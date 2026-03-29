import anthropic
import requests
import gspread
import json
import re
import base64
import os
import tempfile
from datetime import datetime
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from http.server import BaseHTTPRequestHandler

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
CHAT_ID           = int(os.environ["CHAT_ID"])
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Opportunità master")

TOPIC_MAP = {
    2:  "Campus / Residenze / Open Call",
    12: "Bandi",
    14: "Festival",
    22: "Edu",
    19: "Volontariato",
    9:  "Grant",
}

COLONNE = [
    "timestamp", "topic", "titolo", "descrizione", "categoria",
    "ente", "scadenza", "link", "link_accessibile", "priorità", "fonte", "note"
]

USD_EUR            = 0.92
COSTO_INPUT_PER_M  = 0.80
COSTO_OUTPUT_PER_M = 0.80

def get_clients():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(creds_dict, f)
        tmp_path = f.name
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds       = Credentials.from_service_account_file(tmp_path, scopes=scopes)
    gc          = gspread.authorize(creds)
    sheet       = gc.open(GOOGLE_SHEET_NAME).sheet1
    sheet_costi = gc.open(GOOGLE_SHEET_NAME).worksheet("Costi")
    return client, sheet, sheet_costi

def estrai_link(testo):
    match = re.search(r'https?://\S+', testo or "")
    return match.group(0) if match else None

def pulisci_json(raw):
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

def scrivi_su_sheet(sheet, dati):
    riga = [dati.get(col, "N/D") for col in COLONNE]
    sheet.append_row(riga)

def calcola_e_registra_costo(sheet_costi, response, timestamp):
    input_tokens  = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    costo_usd = (input_tokens * COSTO_INPUT_PER_M + output_tokens * COSTO_OUTPUT_PER_M) / 1_000_000
    costo_eur = round(costo_usd * USD_EUR, 6)
    sheet_costi.append_row([timestamp, costo_eur])

def invia_messaggio_telegram(chat_id, thread_id, testo):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": testo
        }
    )

def analizza_messaggio(client, testo, topic, link=None):
    contenuto_link   = None
    link_accessibile = "assente"
    if link:
        try:
            r = requests.get(link, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                contenuto_link   = soup.get_text(separator=" ", strip=True)[:5000]
                link_accessibile = "sì"
            else:
                link_accessibile = "no"
        except Exception:
            link_accessibile = "no"
    sezioni = [f'Messaggio dal topic Telegram "{topic}":\n"""{testo}"""']
    if contenuto_link:
        sezioni.append(f'Testo estratto dalla pagina ({link}):\n"""{contenuto_link}"""')
    elif link:
        sezioni.append(f'Link presente ({link}) ma non accessibile.')
    prompt = f"""Sei un assistente esperto in opportunità culturali, bandi, residenze e finanziamenti.
Analizza le informazioni seguenti e compila una scheda strutturata.

ISTRUZIONI IMPORTANTI:
- Per la descrizione: scrivi sempre una panoramica utile dell'opportunità, anche se le info sono parziali. "N/D" è l'ultimo resort assoluto.
- Per il campo link: usa SEMPRE e SOLO il link originale fornito nel messaggio Telegram, senza modificarlo o sostituirlo con altri link trovati nella pagina (es. "link in bio").
- Per ente e scadenza: cerca attentamente nel testo estratto dalla pagina prima di scrivere "N/D".
- Sii sintetico ma informativo.

Rispondi SOLO con un oggetto JSON valido, senza markdown, senza backtick, senza spiegazioni.
Campi:
- titolo: stringa breve che riassume l'opportunità
- descrizione: panoramica dell'opportunità, cosa offre, a chi è rivolta, max 3 righe
- categoria: uno tra [Bando, Residenza, Festival, Formazione, Volontariato, Grant, Altro]
- ente: organizzazione o ente promotore
- scadenza: data scadenza in formato GG/MM/AAAA (se non presente, "N/D")
- link: url se presente (se non presente, "N/D")
- link_accessibile: "{link_accessibile}"
- priorità: uno tra [Alta, Media, Bassa] in base all'urgenza percepita
- fonte: cosa hai usato, es. "testo", "link", "testo+link"
- note: info aggiuntive utili, max 2 righe (se nessuna, "N/D")

{"  ".join(sezioni)}
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    risultato = json.loads(pulisci_json(response.content[0].text.strip()))
    risultato["timestamp"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    risultato["topic"]     = topic
    return risultato, response

def analizza_immagine(client, file_id, topic, caption=None):
    r         = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}")
    file_path = r.json()["result"]["file_path"]
    img_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}").content
    img_b64   = base64.standard_b64encode(img_bytes).decode("utf-8")
    prompt = f"""Sei un assistente esperto in opportunità culturali, bandi, residenze e finanziamenti.
Analizza l'immagine allegata e compila una scheda strutturata.

ISTRUZIONI IMPORTANTI:
- Per la descrizione: scrivi sempre una panoramica utile dell'opportunità, anche se le info sono parziali. "N/D" è l'ultimo resort assoluto.
- Per il campo link: usa SEMPRE e SOLO il link originale fornito nella caption del messaggio Telegram, senza modificarlo o sostituirlo con altri link trovati nell'immagine (es. "link in bio").
- Per ente e scadenza: cerca attentamente nel testo visibile nell'immagine.
- Sii sintetico ma informativo.

{"Caption del messaggio: " + caption if caption else "Nessuna caption."}

Rispondi SOLO con un oggetto JSON valido, senza markdown, senza backtick, senza spiegazioni.
Campi:
- titolo: stringa breve che riassume l'opportunità
- descrizione: panoramica dell'opportunità, cosa offre, a chi è rivolta, max 3 righe
- categoria: uno tra [Bando, Residenza, Festival, Formazione, Volontariato, Grant, Altro]
- ente: organizzazione o ente promotore
- scadenza: data scadenza in formato GG/MM/AAAA (se non presente, "N/D")
- link: url se presente nella caption (se non presente, "N/D")
- link_accessibile: "assente"
- priorità: uno tra [Alta, Media, Bassa] in base all'urgenza percepita
- fonte: "immagine" oppure "immagine+testo" se c'è caption
- note: info aggiuntive utili, max 2 righe (se nessuna, "N/D")
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    risultato = json.loads(pulisci_json(response.content[0].text.strip()))
    risultato["timestamp"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    risultato["topic"]     = topic
    return risultato, response

class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers["Content-Length"])
        body           = self.rfile.read(content_length)
        update         = json.loads(body)
        msg            = update.get("message", {})

        try:
            client, sheet, sheet_costi = get_clients()

            if msg.get("chat", {}).get("id") != CHAT_ID:
                self._risposta(200)
                return

            thread_id  = msg.get("message_thread_id")
            testo      = msg.get("text") or msg.get("caption", "")
            topic_name = TOPIC_MAP.get(thread_id, f"Topic sconosciuto ({thread_id})")

            if "photo" in msg:
                file_id         = msg["photo"][-1]["file_id"]
                risultato, resp = analizza_immagine(client, file_id, topic_name, caption=testo or None)
            else:
                link            = estrai_link(testo)
                risultato, resp = analizza_messaggio(client, testo, topic_name, link)

            scrivi_su_sheet(sheet, risultato)
            calcola_e_registra_costo(sheet_costi, resp, risultato["timestamp"])
            invia_messaggio_telegram(CHAT_ID, thread_id, "✅ Inserimento effettuato")

        except Exception as e:
            print(f"Errore: {e}")
            thread_id = msg.get("message_thread_id") if msg else None
            if thread_id:
                invia_messaggio_telegram(CHAT_ID, thread_id, "❌ Inserimento interrotto")

        self._risposta(200)

    def do_GET(self):
        self._risposta(200, "OK")

    def _risposta(self, status, body=""):
        self.send_response(status)
        self.end_headers()
        if body:
            self.wfile.write(body.encode())
