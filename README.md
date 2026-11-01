# EkLabs Card Mailer (Vercel + Supabase + Google Sheet)

Send a business card photo to the Telegram bot. It reads the card with Gemini,
mails the person a greeting from your Gmail, saves the record in Supabase and
adds a row to a Google Sheet for follow-up.

```
Telegram --webhook--> Vercel (api/webhook.py) --> Gemini (reads card)
                                  |--> Gmail SMTP (sends greeting)
                                  |--> Supabase (record + duplicate protection)
                                  '--> Google Sheet via Apps Script (follow-up list)
```

## Setup (about 30 minutes, one time)

### 1. Supabase
1. Create a project at supabase.com.
2. SQL Editor -> New query -> paste `supabase_schema.sql` -> Run.
3. Settings -> API: copy the **Project URL** and the **service_role** key.
   The service_role key is a secret. It only goes into Vercel, never into a browser or GitHub.

### 2. Google Sheet
1. Create a new Google Sheet (name it e.g. "Card Mailer Leads").
2. Extensions -> Apps Script -> delete the sample code -> paste `google_apps_script.js`.
3. Change `SECRET` to a random string (remember it, it becomes `SHEET_SECRET`).
4. Deploy -> New deployment -> type **Web app** -> Execute as **Me** -> Who has access **Anyone** -> Deploy.
   Allow the permissions it asks for. Copy the **Web app URL** (ends with `/exec`).
5. The "Cards" tab and its headers are created automatically on the first card.

### 3. Gemini key
Make a **new** key at aistudio.google.com (do not reuse the STOCKFLOW key).

### 4. Gmail App Password
Google Account -> Security -> 2-Step Verification -> App passwords.

### 5. Vercel
1. Put this folder in a GitHub repo (`.env` is ignored, never commit secrets) and import it in Vercel.
   Or run `vercel` from this folder if you use the CLI.
2. Project -> Settings -> Environment Variables: add everything from `.env.example`.
   `TELEGRAM_WEBHOOK_SECRET` is any long random string you make up.
3. Deploy. Open `https://your-project.vercel.app/api/webhook` in the browser.
   You should see: `EkLabs Card Mailer webhook is live`.

### 6. Connect Telegram to Vercel
Stop the old local `bot.py` first (two things cannot read the same bot at once), then:

```
pip install requests
python set_webhook.py https://your-project.vercel.app
```

Enter the bot token and the same webhook secret when asked. You should see `"ok": true`.

### 7. Test
1. Send `/start`, then `/id` to the bot. Put the chat IDs (yours and Ma'am's) in
   `ALLOWED_CHAT_IDS` (comma separated) and redeploy, so strangers cannot trigger mails.
2. Send a card photo. Check: Telegram reply, the mail, the Supabase `cards` table, the Google Sheet.

For use in a group: add the bot to the group, and in BotFather run `/setprivacy` -> Disable.

## Good to know
- **Follow-up**: the Sheet has `Follow-up Status` (dropdown), `Follow-up Date` and `Notes`
  for the team. Download as Excel any time: File -> Download -> .xlsx.
- **Duplicate mails**: the same email is not mailed twice within `DUPLICATE_HOURS` (default 24).
  Set it to `0` while testing with the same card.
- **If the Sheet is down**, the mail still goes out. Rows with `sheet_synced = false` in
  Supabase show what is missing from the Sheet.
- **Change the mail text** in `EMAIL_SUBJECT` / `EMAIL_BODY` at the top of `api/webhook.py`.
- **Go back to polling** (local `bot.py`): `python set_webhook.py --delete`.

## Troubleshooting
- `/api/webhook` asks for a login or returns 401: Vercel Deployment Protection is on.
  Use the production domain, or turn protection off for Production (Settings -> Deployment Protection).
- No reply from the bot: `getWebhookInfo` (printed by `set_webhook.py`) shows `last_error_message`.
  Also check Vercel -> Logs.
- "Gmail login failed": the App Password is wrong, or 2-Step Verification is off.
- "Gemini daily limit reached": free key limit. Try tomorrow, or use a paid key.
- Gmail SMTP from Vercel is not verified by us. If the mail step fails with a network error in
  the logs, switch the sending to an email API (Brevo / Resend).
