# Telegram Client Tracker Bot

Posts your client list + package totals to a Telegram group every morning
and night, and lets anyone in the group add a new client by typing:

```
add Client Name 1500
```

## 1. Create the Telegram bot

1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts, and copy the **bot token** it gives you.
3. Add the bot to your Telegram group.
4. Get the group's chat ID:
   - Add **@RawDataBot** (or @getidsbot) to the group temporarily, it will
     show you the group's chat ID (a negative number like `-1001234567890`).
   - Remove that helper bot afterward if you like.

## 2. Give the bot access to your Google Sheet

1. Go to https://console.cloud.google.com/ and create a project (or use an
   existing one).
2. Enable the **Google Sheets API** for that project.
3. Create a **Service Account** (APIs & Services → Credentials → Create
   Credentials → Service Account).
4. Create a key for it (JSON) and download it. Rename it
   `service_account.json` and place it in this folder.
5. Open your Google Sheet, click **Share**, and share it with the service
   account's email address (it looks like
   `something@your-project.iam.gserviceaccount.com`) — give it **Editor**
   access so it can add rows.

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Configure

Set these environment variables (or edit the top of `bot.py` directly):

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-token"
export TELEGRAM_GROUP_CHAT_ID="-1001234567890"
export GOOGLE_SHEET_ID="1kQWNi0-EQT0gzYl7M4Z-Wu4l2fX88ZW5cJIBwSfjhMk"
export GOOGLE_WORKSHEET_NAME="Sheet1"
export GOOGLE_SERVICE_ACCOUNT_FILE="service_account.json"
export BOT_TIMEZONE="Asia/Kolkata"
```

(`GOOGLE_SHEET_ID` is already defaulted to the sheet you linked, so you can
skip that one unless you change sheets.)

## 5. Run

```bash
python bot.py
```

Leave it running (e.g. on a small VPS, or with `pm2`/`systemd`/`screen`) so
it keeps posting daily and listening for `add` messages.

## How it works

- **Morning/night updates** — by default at 9:00 AM and 9:00 PM
  (`Asia/Kolkata`), the bot reads column A (Clients) and column B (Package)
  from the sheet, and posts each client with their package amount plus a
  grand total to the group. Change `MORNING_TIME` / `NIGHT_TIME` in
  `bot.py` to adjust.
- **`/summary`** — anyone can request the summary on demand.
- **`add Client Name 1500`** or **`/add Client Name 1500`** — appends a new
  row to the sheet with that name and package price. The last word in the
  message must be the numeric price; everything before it is treated as the
  client name.

## Notes / things you may want to tweak

- Currency symbol defaults to ₹ (rupee) — change in `build_summary_message`
  if needed.
- The bot currently only fills columns A (Clients) and B (Package) when
  adding — your sheet has more columns (Email, SM Work, etc.) that are left
  blank on new rows. Let me know if you want `add` to also capture email or
  other fields.
- If you want the summary to only include *active* clients (e.g. skip rows
  marked "Completed" in the Status column), that logic can be added easily.
