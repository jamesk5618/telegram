"""
Telegram Client Tracker Bot
----------------------------
Reads client + package data from a Google Sheet and:
  1. Posts a summary (client names, package amount, paid/pending, total) to
     a Telegram group every morning and every night.
  2. Lets you add a new client through a step-by-step chat flow:
         add <Client Name>
     The bot then asks for package price, posting frequency, platform(s),
     and additional services, and auto-fills today's date as Joining Date.
  3. Lets you edit, remove, and mark payments for existing clients.

Setup instructions are in README.md.
"""

import os
import re
import json
import logging
import threading
import calendar
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
    ConversationHandler,
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
REMINDER_TIME = time(hour=10, minute=0, tzinfo=ZoneInfo(TIMEZONE))

# Column layout in the sheet (1-indexed; A=1, B=2, C=3 ...)
COL_CLIENT = 1   # A - Clients
COL_PACKAGE = 2  # B - Package
COL_EMAIL = 3    # C - Email

# These columns are found dynamically by header name (since month columns
# sit between the fixed columns above and these, and their position can
# shift). Header text must match what's in your sheet's row 1.
HEADER_FREQUENCY = "Frequency"
HEADER_PLATFORM = "Platform"
HEADER_ADDITIONAL = "Additional"
HEADER_JOINING_DATE = "Joining Date"

ALLOWED_PLATFORMS = ["Instagram", "Facebook", "YouTube", "LinkedIn"]
DATE_FORMAT = "%d-%m-%Y"  # used for Joining Date, e.g. 18-09-2026

PORT = int(os.environ.get("PORT", 8080))

# ---------------------------------------------------------------------------
# Keep-alive web server (for Render + UptimeRobot)
# ---------------------------------------------------------------------------
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


def find_column(headers, header_name: str):
    """Finds the 1-indexed column number whose header exactly matches header_name (case-insensitive)."""
    target = header_name.strip().lower()
    for idx, h in enumerate(headers, start=1):
        if h.strip().lower() == target:
            return idx
    return None


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


def set_cell_in_row(row: list, col_idx: int, value):
    """Extends `row` with blanks if needed, then sets row[col_idx-1] = value."""
    while len(row) < col_idx:
        row.append("")
    row[col_idx - 1] = value


def get_client_details(name: str):
    """
    Returns a dict of all known fields for one client (matched by exact,
    case-insensitive name), or None if not found. Includes this month's
    paid/pending figures too.
    """
    headers, data_rows = get_all_data()
    target = name.strip().lower()

    row = None
    for r in data_rows:
        if len(r) >= COL_CLIENT and r[COL_CLIENT - 1].strip().lower() == target:
            row = r
            break
    if row is None:
        return None

    def cell(col_idx):
        return row[col_idx - 1].strip() if col_idx and len(row) >= col_idx else ""

    package_raw = cell(COL_PACKAGE)
    try:
        expected = float(package_raw.replace(",", "")) if package_raw else 0.0
    except ValueError:
        expected = 0.0

    month_name = current_month_name()
    month_col = find_month_column(headers, month_name)
    paid_raw = cell(month_col) if month_col else ""
    try:
        paid = float(paid_raw.replace(",", "")) if paid_raw else 0.0
    except ValueError:
        paid = expected if paid_raw.lower() in ("done", "paid", "yes") else 0.0

    return {
        "name": cell(COL_CLIENT),
        "package": expected,
        "email": cell(COL_EMAIL),
        "frequency": cell(find_column(headers, HEADER_FREQUENCY)),
        "platform": cell(find_column(headers, HEADER_PLATFORM)),
        "additional": cell(find_column(headers, HEADER_ADDITIONAL)),
        "joining_date": cell(find_column(headers, HEADER_JOINING_DATE)),
        "month_name": month_name,
        "paid_this_month": paid,
        "pending_this_month": max(expected - paid, 0.0),
    }


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


def add_client_full(name, price, frequency, platform, additional, joining_date):
    """
    Appends a new client row, filling in Frequency/Platform/Additional/
    Joining Date wherever those headers are found in row 1 (regardless of
    their column position), plus month columns left blank.
    """
    ws = get_worksheet()
    headers = ws.row_values(1)
    row = [""] * max(len(headers), COL_PACKAGE)

    set_cell_in_row(row, COL_CLIENT, name)
    set_cell_in_row(row, COL_PACKAGE, price)

    freq_col = find_column(headers, HEADER_FREQUENCY)
    if freq_col:
        set_cell_in_row(row, freq_col, frequency)

    platform_col = find_column(headers, HEADER_PLATFORM)
    if platform_col:
        set_cell_in_row(row, platform_col, platform)

    additional_col = find_column(headers, HEADER_ADDITIONAL)
    if additional_col:
        set_cell_in_row(row, additional_col, additional)

    joining_col = find_column(headers, HEADER_JOINING_DATE)
    if joining_col:
        set_cell_in_row(row, joining_col, joining_date)

    ws.append_row(row, value_input_option="USER_ENTERED")


def edit_client_price(name: str, new_price: float) -> bool:
    """Updates a client's package price (column B). Returns True if found and updated."""
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


def set_joining_date(name: str, date_str: str) -> bool:
    """Backfills the Joining Date for an existing client. Returns True if found."""
    ws = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        raise ValueError("Sheet is empty.")
    headers, data_rows = all_values[0], all_values[1:]

    joining_col = find_column(headers, HEADER_JOINING_DATE)
    if not joining_col:
        raise ValueError(f"No '{HEADER_JOINING_DATE}' column found in the sheet.")

    target = name.strip().lower()
    for idx, row in enumerate(data_rows, start=2):
        if len(row) >= COL_CLIENT and row[COL_CLIENT - 1].strip().lower() == target:
            ws.update_cell(idx, joining_col, date_str)
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
# Scheduled + on-demand summary handlers
# ---------------------------------------------------------------------------
async def send_morning_update(context: ContextTypes.DEFAULT_TYPE):
    msg = build_summary_message("☀️ Good morning! Here's today's client summary:")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)


async def send_night_update(context: ContextTypes.DEFAULT_TYPE):
    msg = build_summary_message("🌙 Good night! Here's today's client summary:")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)


async def send_due_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs once a day. For each client, treats their Joining Date's day-of-month
    as their recurring billing day (e.g. joined on the 15th -> billed on the
    15th every month; clamped to the last day of shorter months). If today
    is that day and their current-month payment isn't fully paid, they're
    included in a reminder message posted to the group.
    """
    headers, data_rows = get_all_data()
    month_name = current_month_name()
    month_col = find_month_column(headers, month_name)
    joining_col = find_column(headers, HEADER_JOINING_DATE)

    if not joining_col:
        return  # no Joining Date column set up yet, nothing to check

    today = datetime.now(ZoneInfo(TIMEZONE))
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    due_today = []
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
        if month_col and len(row) >= month_col:
            cell_val = row[month_col - 1].strip()
            if cell_val:
                try:
                    paid = float(cell_val.replace(",", ""))
                except ValueError:
                    if cell_val.lower() in ("done", "paid", "yes"):
                        paid = expected
        pending = expected - paid
        if pending <= 0:
            continue  # already fully paid, nothing to remind about

        joining_raw = row[joining_col - 1].strip() if len(row) >= joining_col else ""
        if not joining_raw:
            continue
        try:
            joining_date = datetime.strptime(joining_raw, DATE_FORMAT)
        except ValueError:
            continue

        billing_day = min(joining_date.day, days_in_month)
        if today.day == billing_day:
            due_today.append((name, pending))

    if not due_today:
        return

    lines = ["🔔 Payment due today:", ""]
    for name, pending in due_today:
        lines.append(f"• {name} — ₹{pending:,.0f} pending")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: /summary"""
    msg = build_summary_message("📊 Client summary (on request):")
    await update.message.reply_text(msg)


async def total_client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: /total or the phrase 'total client'"""
    msg = build_summary_message("📊 Total client list:")
    await update.message.reply_text(msg)


async def details_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles:
        details John Doe
        /details John Doe
    Shows everything known about one client.
    """
    text = update.message.text.strip()

    if text.lower().startswith("/details"):
        text = text[8:].strip()
    elif text.lower().startswith("details"):
        text = text[7:].strip()

    name = text.strip()
    if not name:
        await update.message.reply_text(
            "Please use the format:\ndetails Client Name\n(e.g. details John Doe)"
        )
        return

    try:
        info = get_client_details(name)
    except Exception as e:
        logger.exception("Failed to fetch client details")
        await update.message.reply_text(f"⚠️ Failed to read the sheet: {e}")
        return

    if not info:
        await update.message.reply_text(f"⚠️ No client found matching: {name}")
        return

    if info["pending_this_month"] <= 0:
        payment_line = f"✅ Paid in full for {info['month_name']} (₹{info['paid_this_month']:,.0f})"
    elif info["paid_this_month"] > 0:
        payment_line = (
            f"⏳ ₹{info['paid_this_month']:,.0f} paid, "
            f"₹{info['pending_this_month']:,.0f} pending for {info['month_name']}"
        )
    else:
        payment_line = f"⏳ ₹{info['pending_this_month']:,.0f} pending for {info['month_name']}"

    lines = [
        f"👤 {info['name']}",
        f"💰 Package: ₹{info['package']:,.0f}",
        payment_line,
    ]
    if info["email"]:
        lines.append(f"📧 Email: {info['email']}")
    if info["frequency"]:
        lines.append(f"📅 Frequency: {info['frequency']}")
    if info["platform"]:
        lines.append(f"📱 Platform: {info['platform']}")
    if info["additional"]:
        lines.append(f"➕ Additional: {info['additional']}")
    if info["joining_date"]:
        lines.append(f"🗓️ Joining Date: {info['joining_date']}")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# "add" conversation flow: add ClientName -> price -> frequency -> platform
# -> additional -> saved with today's date as Joining Date
# ---------------------------------------------------------------------------
ASK_PRICE, ASK_FREQUENCY, ASK_PLATFORM, ASK_ADDITIONAL = range(4)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower().startswith("/add"):
        name = text[4:].strip()
    elif text.lower().startswith("add"):
        name = text[3:].strip()
    else:
        name = text.strip()

    if not name:
        await update.message.reply_text(
            "Please include the client's name, e.g.:\nadd John Doe"
        )
        return ConversationHandler.END

    context.user_data["new_client"] = {"name": name}
    await update.message.reply_text(
        f"Adding new client: {name}\n\nWhat's the package price? (just the number, e.g. 1500)"
    )
    return ASK_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        price = float(text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("Please send just the numeric price, e.g. 1500")
        return ASK_PRICE

    context.user_data["new_client"]["price"] = price
    await update.message.reply_text(
        "Got it. What's the posting frequency this month?\n"
        "Use P for posts and R for reels — e.g. 8P 4R"
    )
    return ASK_FREQUENCY


async def add_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not re.search(r"\d+\s*[PRpr]", text):
        await update.message.reply_text(
            "Please include counts with P (posts) and/or R (reels) — e.g. 8P 4R"
        )
        return ASK_FREQUENCY

    context.user_data["new_client"]["frequency"] = text
    await update.message.reply_text(
        "Which platform(s)? Reply with any of these, comma-separated:\n"
        f"{', '.join(ALLOWED_PLATFORMS)}"
    )
    return ASK_PLATFORM


async def add_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tokens = [t.strip() for t in text.split(",") if t.strip()]

    matched = []
    unmatched = []
    for t in tokens:
        hit = next((p for p in ALLOWED_PLATFORMS if p.lower() == t.lower()), None)
        if hit:
            matched.append(hit)
        else:
            unmatched.append(t)

    if not matched:
        await update.message.reply_text(
            f"Please choose from: {', '.join(ALLOWED_PLATFORMS)} (comma-separated)"
        )
        return ASK_PLATFORM

    if unmatched:
        await update.message.reply_text(
            f"Note: ignored unrecognized platform(s): {', '.join(unmatched)}"
        )

    context.user_data["new_client"]["platform"] = ", ".join(matched)
    await update.message.reply_text(
        "Any additional services? e.g. Website, SEO, GMB — comma-separated,\n"
        "or type your own (e.g. 'Content Writing'), or send 'None'"
    )
    return ASK_ADDITIONAL


async def add_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    additional = "" if text.lower() == "none" else text

    data = context.user_data.pop("new_client", None)
    if not data:
        await update.message.reply_text(
            "Something went wrong — please start again with: add Client Name"
        )
        return ConversationHandler.END

    joining_date = datetime.now(ZoneInfo(TIMEZONE)).strftime(DATE_FORMAT)

    try:
        add_client_full(
            name=data["name"],
            price=data["price"],
            frequency=data["frequency"],
            platform=data["platform"],
            additional=additional,
            joining_date=joining_date,
        )
    except Exception as e:
        logger.exception("Failed to add client")
        await update.message.reply_text(f"⚠️ Failed to add client to the sheet: {e}")
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Client added!\n\n"
        f"👤 Name: {data['name']}\n"
        f"💰 Package: ₹{data['price']:,.0f}\n"
        f"📅 Frequency: {data['frequency']}\n"
        f"📱 Platform: {data['platform']}\n"
        f"➕ Additional: {additional or 'None'}\n"
        f"🗓️ Joining Date: {joining_date}"
    )
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_client", None)
    await update.message.reply_text("Cancelled adding client.")
    return ConversationHandler.END


add_conversation = ConversationHandler(
    entry_points=[
        CommandHandler("add", add_start),
        MessageHandler(filters.Regex(r"(?i)^add\s+.+") & ~filters.COMMAND, add_start),
    ],
    states={
        ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
        ASK_FREQUENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_frequency)],
        ASK_PLATFORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_platform)],
        ASK_ADDITIONAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_additional)],
    },
    fallbacks=[CommandHandler("cancel", add_cancel)],
)


# ---------------------------------------------------------------------------
# Other command handlers
# ---------------------------------------------------------------------------
async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles:
        remove John Doe
        /remove John Doe
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
    Handles:
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
    Handles:
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


async def setjoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Backfills a Joining Date for a client added before the bot existed.
    Handles:
        setjoin John Doe 15-06-2026
        /setjoin John Doe 15-06-2026
    Date format: DD-MM-YYYY
    """
    text = update.message.text.strip()

    if text.lower().startswith("/setjoin"):
        text = text[8:].strip()
    elif text.lower().startswith("setjoin"):
        text = text[7:].strip()

    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "Please use the format:\nsetjoin Client Name DD-MM-YYYY\n"
            "(e.g. setjoin John Doe 15-06-2026)"
        )
        return

    date_str = parts[-1]
    name = " ".join(parts[:-1])

    try:
        datetime.strptime(date_str, DATE_FORMAT)
    except ValueError:
        await update.message.reply_text(
            f"Please give the date as DD-MM-YYYY, e.g. 15-06-2026"
        )
        return

    try:
        found = set_joining_date(name, date_str)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except Exception as e:
        logger.exception("Failed to set joining date")
        await update.message.reply_text(f"⚠️ Failed to update the sheet: {e}")
        return

    if found:
        await update.message.reply_text(f"🗓️ Set {name}'s joining date to {date_str}")
    else:
        await update.message.reply_text(f"⚠️ No client found matching: {name}")


async def plain_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Single dispatcher for plain (non-slash) text messages in the group that
    aren't caught by the add-client conversation above.
    """
    text = (update.message.text or "").strip().lower()

    if text.startswith("remove "):
        await remove_command(update, context)
    elif text.startswith("edit "):
        await edit_command(update, context)
    elif text.startswith("setjoin "):
        await setjoin_command(update, context)
    elif text.startswith("details "):
        await details_command(update, context)
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
    app.add_handler(CommandHandler("total", total_client_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("setjoin", setjoin_command))
    app.add_handler(CommandHandler("details", details_command))

    # Conversation handler must be added before the generic dispatcher so
    # "add ..." messages get captured by the step-by-step flow.
    app.add_handler(add_conversation)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_dispatcher))

    app.job_queue.run_daily(send_morning_update, time=MORNING_TIME, name="morning_update")
    app.job_queue.run_daily(send_night_update, time=NIGHT_TIME, name="night_update")
    app.job_queue.run_daily(send_due_reminders, time=REMINDER_TIME, name="due_reminders")

    # Start the keep-alive web server in the background so Render sees
    # inbound traffic (from UptimeRobot) and doesn't put this service to sleep.
    threading.Thread(target=run_keep_alive_server, daemon=True).start()

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
