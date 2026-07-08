"""
Telegram Client Tracker Bot
----------------------------
Reads client + package data from a Google Sheet and:
  1. Posts a summary (client names + package amount + total) to a Telegram
     group every morning and every night.
  2. Lets you add a new client row by typing in the group:
         add <Client Name> <Package Price>
     e.g.   add John Doe 1500

Setup instructions are in README.md.
"""

import os
import json
import logging
import threading
from datetime import time, datetime
from zoneinfo import ZoneInfo

import gspread
from flask import Flask
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG — fill these in via environment variables (recommended) or edit here
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "PUT_YOUR_GROUP_CHAT_ID_HERE")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1kQWNi0-EQT0gzYl7M4Z-Wu4l2fX88ZW5cJIBwSfjhMk")
WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Kolkata")

# On hosts like Render, you can't commit service_account.json to Git. Instead,
# paste its full contents into a GOOGLE_SERVICE_ACCOUNT_JSON env variable and
# this writes it out to a real file on startup.
if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and not os.path.exists(SERVICE_ACCOUNT_FILE):
    with open(SERVICE_ACCOUNT_FILE, "w") as f:
        f.write(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

MORNING_TIME = time(hour=9, minute=0, tzinfo=ZoneInfo(TIMEZONE))
NIGHT_TIME = time(hour=21, minute=0, tzinfo=ZoneInfo(TIMEZONE))

# Column layout in the sheet (1-indexed, matches your current sheet)
COL_CLIENT = 1   # A - Clients
COL_PACKAGE = 2  # B - Package
COL_EMAIL = 3    # C - Email

PORT = int(os.environ.get("PORT", 8080))

# ---------------------------------------------------------------------------
# Keep-alive web server (for Render + UptimeRobot)
# ---------------------------------------------------------------------------
# Render's free tier sleeps a Web Service after ~15 min with no inbound HTTP
# traffic. The bot's Telegram polling is all outbound, so Render doesn't see
# it as activity. Pinging this endpoint every 5 min with UptimeRobot (or
# similar) keeps the service awake, which also keeps the scheduled
# morning/night jobs firing on time.
keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def health_check():
    return "Bot is alive", 200


def run_keep_alive_server():
    keep_alive_app.run(host="0.0.0.0", port=PORT)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------
def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)
    return sheet.worksheet(WORKSHEET_NAME)


def get_all_data():
    """Returns (headers, data_rows) where data_rows excludes the header row."""
    ws = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        return [], []
    return all_values[0], all_values[1:]


def find_month_column(headers, month_name: str):
    """
    Finds the 1-indexed column number whose header matches month_name.
    Accepts full names ("July") or abbreviations ("Jul"), case-insensitive.
    Returns None if no matching column header exists.
    """
    target = month_name.strip().lower()
    for idx, header in enumerate(headers, start=1):
        h = header.strip().lower()
        if h == target or (h[:3] == target[:3] and len(h) >= 3 and len(target) >= 3):
            return idx
    return None


def current_month_name() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%B")


def get_clients_with_payment(headers, data_rows, month_col_idx):
    """
    Returns list of (name, expected_amount, paid_amount) tuples for the
    given month column. paid_amount is 0.0 if that cell is blank/unrecognized.
    """
    clients = []
    for row in data_rows:
        name = row[COL_CLIENT - 1].strip() if len(row) >= COL_CLIENT else ""
        package_raw = row[COL_PACKAGE - 1].strip() if len(row) >= COL_PACKAGE else ""
        if not name or not package_raw:
            continue
        try:
            expected = float(package_raw.replace(",", ""))
        except ValueError:
            continue

        paid = 0.0
        if month_col_idx and len(row) >= month_col_idx:
            cell_val = row[month_col_idx - 1].strip()
            if cell_val:
                try:
                    paid = float(cell_val.replace(",", ""))
                except ValueError:
                    # allow plain "done"/"paid"/"yes" text in the cell too
                    if cell_val.lower() in ("done", "paid", "yes"):
                        paid = expected
        clients.append((name, expected, paid))
    return clients


def mark_payment(name: str, month_name: str, amount: float = None) -> float:
    """
    Marks a payment for the given client/month. If `amount` is given, that
    amount is ADDED to whatever's already in that month's cell (so partial
    payments accumulate). If `amount` is None, the client's full package
    price is written in (marks the month fully paid). Returns the new total
    paid amount for that client/month. Raises ValueError if the client or
    the month column isn't found.
    """
    ws = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        raise ValueError("Sheet is empty.")
    headers, data_rows = all_values[0], all_values[1:]

    month_col = find_month_column(headers, month_name)
    if not month_col:
        raise ValueError(
            f"No column found for '{month_name}'. Add a '{month_name.title()}' "
            f"column header to the sheet first."
        )

    target = name.strip().lower()
    for idx, row in enumerate(data_rows, start=2):  # row 2 = first data row
        if len(row) >= COL_CLIENT and row[COL_CLIENT - 1].strip().lower() == target:
            package_raw = row[COL_PACKAGE - 1].strip() if len(row) >= COL_PACKAGE else ""
            try:
                package_price = float(package_raw.replace(",", ""))
            except ValueError:
                raise ValueError(f"'{name}' has no valid package amount in column B.")

            if amount is None:
                new_total = package_price
            else:
                existing_raw = row[month_col - 1].strip() if len(row) >= month_col else ""
                try:
                    existing_paid = float(existing_raw.replace(",", ""))
                except ValueError:
                    existing_paid = 0.0
                new_total = existing_paid + amount

            ws.update_cell(idx, month_col, new_total)
            return new_total

    raise ValueError(f"No client found matching: {name}")


def add_client(name: str, package: float):
    ws = get_worksheet()
    ws.append_row([name, package], value_input_option="USER_ENTERED")


def edit_client_price(name: str, new_price: float) -> bool:
    """
    Updates a client's package price (column B). Returns True if found and
    updated, False if no matching client.
    """
    ws = get_worksheet()
    rows = ws.get_all_values()
    target = name.strip().lower()

    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= COL_CLIENT and row[COL_CLIENT - 1].strip().lower() == target:
            ws.update_cell(idx, COL_PACKAGE, new_price)
            return True
    return False


def remove_client(name: str) -> bool:
    """
    Finds a client row by name (case-insensitive, exact match) and deletes
    that row from the sheet. Returns True if a row was found and removed,
    False otherwise. If multiple rows match, removes the first match.
    """
    ws = get_worksheet()
    rows = ws.get_all_values()
    target = name.strip().lower()

    for idx, row in enumerate(rows[1:], start=2):  # start=2: row 1 is header
        if len(row) >= COL_CLIENT and row[COL_CLIENT - 1].strip().lower() == target:
            ws.delete_rows(idx)
            return True
    return False


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def build_summary_message(greeting: str) -> str:
    headers, data_rows = get_all_data()
    month_name = current_month_name()
    month_col = find_month_column(headers, month_name)
    clients = get_clients_with_payment(headers, data_rows, month_col)

    if not clients:
        return f"{greeting}\n\nNo client data found in the sheet."

    lines = [greeting, "", f"📋 Client List — {month_name}:"]
    total_expected = 0.0
    total_collected = 0.0
    for name, expected, paid in clients:
        remaining = expected - paid
        if remaining <= 0:
            status = f"✅ Paid ₹{expected:,.0f}"
        elif paid > 0:
            status = f"⏳ ₹{paid:,.0f} paid, ₹{remaining:,.0f} pending"
        else:
            status = f"⏳ ₹{remaining:,.0f} pending"
        lines.append(f"• {name} — {status}")
        total_expected += expected
        total_collected += paid

    lines.append("")
    lines.append(f"💰 Expected this month: ₹{total_expected:,.0f}")
    lines.append(f"✅ Collected this month: ₹{total_collected:,.0f}")
    lines.append(f"⏳ Pending: ₹{total_expected - total_collected:,.0f}")
    lines.append(f"👥 Total Clients: {len(clients)}")

    if not month_col:
        lines.append("")
        lines.append(
            f"⚠️ No '{month_name}' column found in the sheet — payments can't "
            f"be tracked until it's added."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
async def send_morning_update(context: ContextTypes.DEFAULT_TYPE):
    msg = build_summary_message("☀️ Good morning! Here's today's client summary:")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)


async def send_night_update(context: ContextTypes.DEFAULT_TYPE):
    msg = build_summary_message("🌙 Good night! Here's today's client summary:")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: /summary"""
    msg = build_summary_message("📊 Client summary (on request):")
    await update.message.reply_text(msg)


async def total_client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual trigger: /total or the phrase "total client"
    Same output as the scheduled morning/night summary.
    """
    msg = build_summary_message("📊 Total client list:")
    await update.message.reply_text(msg)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages like:
        add John Doe 1500
        /add John Doe 1500
    Last word must be the numeric package price; everything before it is the
    client name.
    """
    text = update.message.text.strip()

    # Strip leading "/add" or "add"
    if text.lower().startswith("/add"):
        text = text[4:].strip()
    elif text.lower().startswith("add"):
        text = text[3:].strip()

    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "Please use the format:\nadd Client Name Price\n(e.g. add John Doe 1500)"
        )
        return

    price_str = parts[-1]
    name = " ".join(parts[:-1])

    try:
        price = float(price_str.replace(",", ""))
    except ValueError:
        await update.message.reply_text(
            "Couldn't read the price. Please use:\nadd Client Name Price\n(e.g. add John Doe 1500)"
        )
        return

    try:
        add_client(name, price)
    except Exception as e:
        logger.exception("Failed to add client to sheet")
        await update.message.reply_text(f"⚠️ Failed to add entry to the sheet: {e}")
        return

    await update.message.reply_text(f"✅ Added: {name} — ₹{price:,.0f}")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages like:
        remove John Doe
        /remove John Doe
    Removes that client's row from the sheet (case-insensitive exact match
    on the whole name).
    """
    text = update.message.text.strip()

    if text.lower().startswith("/remove"):
        text = text[7:].strip()
    elif text.lower().startswith("remove"):
        text = text[6:].strip()

    name = text.strip()
    if not name:
        await update.message.reply_text(
            "Please use the format:\nremove Client Name\n(e.g. remove John Doe)"
        )
        return

    try:
        removed = remove_client(name)
    except Exception as e:
        logger.exception("Failed to remove client from sheet")
        await update.message.reply_text(f"⚠️ Failed to remove entry from the sheet: {e}")
        return

    if removed:
        await update.message.reply_text(f"🗑️ Removed: {name}")
    else:
        await update.message.reply_text(f"⚠️ No client found matching: {name}")


async def payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages like:
        John Doe July done              -> marks full package price as paid
        John Doe July done 2000         -> adds a partial payment of 2000
    """
    text = update.message.text.strip()
    words = text.split()

    if len(words) < 3:
        await update.message.reply_text(
            "Please use the format:\nClient Name Month done\nor\nClient Name Month done 2000\n"
            "(e.g. John Doe July done  /  John Doe July done 2000)"
        )
        return

    partial_amount = None
    if words[-1].lower() != "done":
        # last word might be a partial amount, second-last should be "done"
        if len(words) >= 4 and words[-2].lower() == "done":
            try:
                partial_amount = float(words[-1].replace(",", ""))
            except ValueError:
                await update.message.reply_text(
                    "Couldn't read the payment amount. Please use:\n"
                    "Client Name Month done 2000"
                )
                return
            month_name = words[-3]
            name = " ".join(words[:-3])
        else:
            await update.message.reply_text(
                "Please use the format:\nClient Name Month done\nor\nClient Name Month done 2000"
            )
            return
    else:
        month_name = words[-2]
        name = " ".join(words[:-2])

    if not name:
        await update.message.reply_text(
            "Please use the format:\nClient Name Month done\n(e.g. John Doe July done)"
        )
        return

    try:
        new_total = mark_payment(name, month_name, partial_amount)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except Exception as e:
        logger.exception("Failed to mark payment")
        await update.message.reply_text(f"⚠️ Failed to update the sheet: {e}")
        return

    if partial_amount is not None:
        await update.message.reply_text(
            f"✅ Added ₹{partial_amount:,.0f} for {name}'s {month_name.title()} payment "
            f"— total paid so far: ₹{new_total:,.0f}"
        )
    else:
        await update.message.reply_text(
            f"✅ Marked {name}'s {month_name.title()} payment as done — ₹{new_total:,.0f}"
        )


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages like:
        edit John Doe 6000
        /edit John Doe 6000
    Updates that client's package price.
    """
    text = update.message.text.strip()

    if text.lower().startswith("/edit"):
        text = text[5:].strip()
    elif text.lower().startswith("edit"):
        text = text[4:].strip()

    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "Please use the format:\nedit Client Name NewPrice\n(e.g. edit John Doe 6000)"
        )
        return

    price_str = parts[-1]
    name = " ".join(parts[:-1])

    try:
        new_price = float(price_str.replace(",", ""))
    except ValueError:
        await update.message.reply_text(
            "Couldn't read the price. Please use:\nedit Client Name NewPrice\n(e.g. edit John Doe 6000)"
        )
        return

    try:
        found = edit_client_price(name, new_price)
    except Exception as e:
        logger.exception("Failed to edit client price")
        await update.message.reply_text(f"⚠️ Failed to update the sheet: {e}")
        return

    if found:
        await update.message.reply_text(f"✏️ Updated {name}'s package price to ₹{new_price:,.0f}")
    else:
        await update.message.reply_text(f"⚠️ No client found matching: {name}")


async def plain_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Single dispatcher for plain (non-slash) text messages in the group.
    Routes to the right handler based on the message's starting words.
    """
    text = (update.message.text or "").strip().lower()

    if text.startswith("add "):
        await add_command(update, context)
    elif text.startswith("remove "):
        await remove_command(update, context)
    elif text.startswith("edit "):
        await edit_command(update, context)
    elif text in ("total client", "total clients"):
        await total_client_command(update, context)
    elif text.endswith(" done") or " done " in text:
        await payment_command(update, context)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if BOT_TOKEN.startswith("PUT_YOUR"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN before running the bot.")
    if GROUP_CHAT_ID.startswith("PUT_YOUR"):
        raise SystemExit("Set TELEGRAM_GROUP_CHAT_ID before running the bot.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("total", total_client_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_dispatcher))

    app.job_queue.run_daily(send_morning_update, time=MORNING_TIME, name="morning_update")
    app.job_queue.run_daily(send_night_update, time=NIGHT_TIME, name="night_update")

    # Start the keep-alive web server in the background so Render sees
    # inbound traffic (from UptimeRobot) and doesn't put this service to sleep.
    threading.Thread(target=run_keep_alive_server, daemon=True).start()

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
