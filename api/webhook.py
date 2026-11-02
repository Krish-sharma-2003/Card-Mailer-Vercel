"""
EkLabs Card Mailer - Vercel (webhook) version

Telegram -> POST /api/webhook -> this function:
  1. checks the secret header (so only Telegram can call it)
  2. saves a row in Supabase (the unique update_id stops duplicate mails)
  3. downloads the card photo and Gemini reads it
  4. sends the greeting mail through Gmail
  5. updates Supabase, replies in Telegram, appends a row to the Google Sheet
"""

import base64
import hmac
import json
import logging
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler

import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cardmailer")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ----------------------------- CONFIG ---------------------------------
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET")
GMAIL_ADDRESS = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")
SENDER_NAME = env("SENDER_NAME", "EkLabs")
GEMINI_API_KEY = env("GEMINI_API_KEY")
# Google retires Gemini models often (gemini-2.5-flash now returns 404).
# GEMINI_MODEL is tried first; if it is gone (404) or out of quota (429),
# the next one in the list is tried automatically.
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
SHEET_URL = env("SHEET_WEBHOOK_URL")
SHEET_SECRET = env("SHEET_SECRET")

# Comma separated chat IDs allowed to use the bot. Empty = everyone.
ALLOWED_CHAT_IDS = {int(x) for x in env("ALLOWED_CHAT_IDS").split(",") if x.strip()}

# Do not mail the same address again within this many hours (0 = always mail).
DUPLICATE_HOURS = int(env("DUPLICATE_HOURS", "24") or 0)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
IST = timezone(timedelta(hours=5, minutes=30))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# ------------------------- EMAIL TEMPLATE -----------------------------
# Change this text later according to business need.
EMAIL_SUBJECT = "Warm Diwali Wishes from {sender}"
EMAIL_BODY = """\
Dear {name},

Wishing you and your family a very Happy Diwali!
May this festival of lights bring happiness, prosperity and success
to you and your business.

It was a pleasure connecting with you.

Warm regards,
{sender}
"""

GEMINI_PROMPT = (
    "This is a photo of a business card. It may be rotated, dark, or have "
    "light text on a dark background. Extract the details and reply with ONLY "
    'JSON in exactly this form: {"name": "", "designation": "", "company": "", '
    '"email": "", "phone": "", "website": "", "address": ""}. '
    "'name' is the person's name, never the company or the job title. "
    "If there are several phone numbers, put them all in 'phone' separated by "
    "commas. Use an empty string for anything that is not on the card."
)


# ---------------------------- TELEGRAM --------------------------------
def tg_send(chat_id: int, text: str, reply_to: int | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        requests.post(f"{TG_API}/sendMessage", json=payload, timeout=15)
    except Exception:
        log.exception("Telegram sendMessage failed")


def tg_typing(chat_id: int) -> None:
    try:
        requests.post(
            f"{TG_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=10,
        )
    except Exception:
        pass


def tg_download(file_id: str) -> bytes:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    path = r.json()["result"]["file_path"]
    f = requests.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=30
    )
    f.raise_for_status()
    return f.content


# ----------------------------- GEMINI ---------------------------------
_working_model = None  # remembered while this serverless instance stays warm


def _gemini_request(model: str, image: bytes, mime: str, low_thinking: bool):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    config = {"responseMimeType": "application/json", "temperature": 0}
    if low_thinking:
        # Keeps replies fast. Not every model accepts it, caller retries without.
        config["thinkingConfig"] = {"thinkingLevel": "low"}
    body = {
        "contents": [
            {
                "parts": [
                    {"text": GEMINI_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(image).decode(),
                        }
                    },
                ]
            }
        ],
        "generationConfig": config,
    }
    return requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=50,
    )


def gemini_extract(image: bytes, mime: str) -> dict:
    global _working_model
    candidates = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    if _working_model in candidates:
        candidates.remove(_working_model)
        candidates.insert(0, _working_model)

    last = None
    for model in candidates:
        r = _gemini_request(model, image, mime, low_thinking=True)
        if r.status_code == 400:  # model may not support thinkingLevel
            r = _gemini_request(model, image, mime, low_thinking=False)
        if r.status_code in (404, 429):  # model retired, or no quota on this one
            log.warning("Gemini model %s returned %s, trying next", model, r.status_code)
            last = r
            continue
        r.raise_for_status()
        _working_model = model
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_card(text)

    last.raise_for_status()  # every model failed: surface the last error
    raise RuntimeError("No Gemini model available")


def _parse_card(text: str) -> dict:
    data = json.loads(text)
    if isinstance(data, list):
        data = data[0] if data else {}

    def clean(key: str) -> str:
        return str(data.get(key) or "").strip()

    found = EMAIL_RE.search(clean("email"))
    return {
        "name": clean("name"),
        "designation": clean("designation"),
        "company": clean("company"),
        "email": found.group(0).lower() if found else "",
        "phone": clean("phone"),
        "website": clean("website"),
        "address": clean("address"),
    }


# ----------------------------- SUPABASE -------------------------------
def sb_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def sb_insert(row: dict) -> dict | None:
    """Insert a row. Returns None if update_id already exists (duplicate)."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/cards",
        headers={**sb_headers(), "Prefer": "return=representation"},
        json=row,
        timeout=15,
    )
    if r.status_code == 409:
        return None
    r.raise_for_status()
    return r.json()[0]


def sb_update(row_id: int, fields: dict) -> None:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/cards",
        params={"id": f"eq.{row_id}"},
        headers=sb_headers(),
        json=fields,
        timeout=15,
    )
    r.raise_for_status()


def recently_mailed(email: str) -> bool:
    if DUPLICATE_HOURS <= 0:
        return False
    since = (datetime.now(timezone.utc) - timedelta(hours=DUPLICATE_HOURS)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/cards",
        headers=sb_headers(),
        params={
            "select": "id",
            "email": f"eq.{email}",
            "status": "eq.mail_sent",
            "created_at": f"gte.{since}",
            "limit": "1",
        },
        timeout=15,
    )
    r.raise_for_status()
    return bool(r.json())


# --------------------------- GOOGLE SHEET -----------------------------
def sheet_append(row: list) -> bool:
    if not SHEET_URL:
        return False
    try:
        r = requests.post(
            SHEET_URL, json={"secret": SHEET_SECRET, "row": row}, timeout=25
        )
        return r.ok and r.text.strip() == "ok"
    except Exception:
        log.exception("Sheet append failed")
        return False


# ------------------------------ EMAIL ---------------------------------
def send_mail(to_email: str, name: str) -> None:
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{GMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg["Subject"] = EMAIL_SUBJECT.format(sender=SENDER_NAME)
    msg.set_content(EMAIL_BODY.format(name=name or "Sir/Madam", sender=SENDER_NAME))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ----------------------------- MAIN LOGIC -----------------------------
def pick_image(msg: dict) -> tuple[str, str]:
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], "image/jpeg"  # largest size
    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        return doc["file_id"], doc["mime_type"]
    return "", ""


STATUS_LABEL = {
    "mail_sent": "Sent",
    "no_email_found": "No email found",
    "duplicate_skipped": "Skipped (already mailed)",
    "error": "Error",
}


def handle_card(update: dict, msg: dict, chat_id: int, file_id: str, mime: str) -> None:
    sender = msg.get("from") or {}
    added_by = sender.get("username") or sender.get("first_name") or ""

    # Saving the row first makes update_id the lock: a Telegram retry of the
    # same update hits the unique constraint and is ignored (no double mail).
    row = sb_insert(
        {"update_id": update["update_id"], "chat_id": chat_id, "added_by": added_by}
    )
    if row is None:
        log.info("Duplicate update %s ignored", update["update_id"])
        return

    info = {k: "" for k in
            ("name", "designation", "company", "email", "phone", "website", "address")}
    status, error, reply = "error", "", ""
    mailed_at = None

    try:
        tg_typing(chat_id)
        image = tg_download(file_id)
        info = gemini_extract(image, mime)

        if not info["email"]:
            status = "no_email_found"
            reply = ("Could not find an email on this card. "
                     "Try a clearer, well-lit, straight photo.")
        elif recently_mailed(info["email"]):
            status = "duplicate_skipped"
            reply = (f"Already mailed {info['email']} in the last "
                     f"{DUPLICATE_HOURS} hours, so I skipped it.")
        else:
            send_mail(info["email"], info["name"])
            status = "mail_sent"
            mailed_at = datetime.now(timezone.utc).isoformat()
            reply = (f"Mail sent to {info['name'] or 'contact'} <{info['email']}>"
                     + (f"\nPhone: {info['phone']}" if info["phone"] else ""))
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        error = f"HTTP {code} from {e.request.url.split('?')[0] if e.request else ''}"
        if "generativelanguage" in error and code == 429:
            reply = "Gemini daily limit reached. Please try again later."
        else:
            reply = f"Something went wrong ({error})."
        log.exception("HTTP error")
    except smtplib.SMTPAuthenticationError:
        error = "Gmail login failed"
        reply = "Gmail login failed. Check the App Password."
        log.exception("SMTP auth error")
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:300]
        reply = f"Something went wrong ({type(e).__name__})."
        log.exception("Card processing failed")

    try:
        sb_update(row["id"], {**info, "status": status, "error": error or None,
                              "mail_sent_at": mailed_at})
    except Exception:
        log.exception("Supabase update failed")

    tg_send(chat_id, reply, reply_to=msg.get("message_id"))

    synced = sheet_append([
        datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
        info["name"], info["designation"], info["company"], info["email"],
        info["phone"], info["website"], info["address"],
        STATUS_LABEL.get(status, status), added_by,
    ])
    if synced:
        try:
            sb_update(row["id"], {"sheet_synced": True})
        except Exception:
            log.exception("Could not mark sheet_synced")


def handle_update(update: dict) -> None:
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/start":
            tg_send(chat_id, "Send me a photo of a business card and "
                             "I'll mail the person a greeting.")
        elif cmd == "/id":
            tg_send(chat_id, f"Chat ID: {chat_id}")
        return

    file_id, mime = pick_image(msg)
    if not file_id:
        return
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return
    handle_card(update, msg, chat_id, file_id, mime)


# --------------------------- VERCEL HANDLER ---------------------------
class handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, body: str = "ok") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # quick health check in the browser
        self._respond(200, "EkLabs Card Mailer webhook is live")

    def do_POST(self):
        got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not WEBHOOK_SECRET or not hmac.compare_digest(
            got.encode(), WEBHOOK_SECRET.encode()
        ):
            return self._respond(403, "forbidden")
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length) or b"{}")
            handle_update(update)
        except Exception:
            log.exception("Update failed")
        # Always 200, otherwise Telegram keeps retrying the same update.
        self._respond(200)

    def log_message(self, *args):  # keep logs free of request lines
        pass
