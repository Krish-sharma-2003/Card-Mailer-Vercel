"""
Tell Telegram to send updates to your Vercel app.

Usage:
    python set_webhook.py https://your-project.vercel.app
    python set_webhook.py --delete        (go back to polling / remove webhook)

It asks for the bot token and webhook secret (typing is hidden, nothing is
saved). The secret must be the same as TELEGRAM_WEBHOOK_SECRET on Vercel.
"""
import sys
from getpass import getpass

import requests

if len(sys.argv) < 2:
    sys.exit(__doc__)

token = getpass("Bot token: ").strip()
api = f"https://api.telegram.org/bot{token}"

if sys.argv[1] == "--delete":
    print(requests.post(f"{api}/deleteWebhook", timeout=20).json())
    sys.exit()

secret = getpass("Webhook secret: ").strip()
url = sys.argv[1].rstrip("/") + "/api/webhook"
resp = requests.post(
    f"{api}/setWebhook",
    json={
        "url": url,
        "secret_token": secret,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    },
    timeout=20,
)
print(resp.json())
print(requests.get(f"{api}/getWebhookInfo", timeout=20).json())
