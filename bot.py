import logging
import os
import re
from datetime import datetime, timezone

import dateparser
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import add_reminder, delete_reminder, get_pending_reminders, get_user_reminders, init_db, mark_sent

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your reminder bot.\n\n"
        "Just send me a message like:\n"
        '  "remind me to buy milk in 2 hours"\n'
        '  "call mom tomorrow at 3pm"\n'
        '  "meeting in 30 minutes"\n\n'
        "Commands:\n"
        "/list - see your upcoming reminders\n"
        "/cancel <id> - cancel a reminder",
        message_thread_id=update.message.message_thread_id,
    )


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reminders = get_user_reminders(chat_id)

    if not reminders:
        await update.message.reply_text(
            "You have no upcoming reminders.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    lines = []
    for r in reminders:
        remind_at = datetime.fromisoformat(r["remind_at"])
        lines.append(f"[{r['id']}] {r['text']} — {remind_at.strftime('%Y-%m-%d %H:%M UTC')}")

    await update.message.reply_text(
        "Your upcoming reminders:\n\n" + "\n".join(lines),
        message_thread_id=update.message.message_thread_id,
    )


async def cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /cancel <id>\nUse /list to see your reminder IDs.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "Invalid ID. Use /list to see your reminder IDs.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    if delete_reminder(reminder_id):
        await update.message.reply_text(
            f"Reminder [{reminder_id}] cancelled.",
            message_thread_id=update.message.message_thread_id,
        )
    else:
        await update.message.reply_text(
            f"Reminder [{reminder_id}] not found or already sent.",
            message_thread_id=update.message.message_thread_id,
        )


def parse_reminder(text: str) -> tuple[str, datetime | None]:
    """Extract a reminder description and a future time from natural language text."""
    # Strip common prefixes
    cleaned = re.sub(r"^(remind\s+me\s+(to\s+)?|reminder\s+(to\s+)?)", "", text, flags=re.IGNORECASE).strip()

    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(),
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    # First try dateparser.search to find dates within longer text
    search_results = dateparser.search.search_dates(text, settings=settings)

    parsed_dt = None
    matched_text = ""

    if search_results:
        # Use the last date found (usually the time part at the end)
        matched_text, parsed_dt = search_results[-1]
    else:
        # Fallback: try parsing the whole string
        parsed_dt = dateparser.parse(text, settings=settings)

    if parsed_dt is None:
        return cleaned, None

    # Remove the matched time text from the cleaned string to get the task
    task = cleaned
    if matched_text:
        # Remove the matched date/time phrase from the task
        task = cleaned.replace(matched_text, "").strip()
        # Clean up leftover prepositions and whitespace
        task = re.sub(r"\s+(at|on|in|by|before|after|for)\s*$", "", task, flags=re.IGNORECASE).strip()
        task = re.sub(r"^\s*(at|on|in|by|before|after|for)\s+", "", task, flags=re.IGNORECASE).strip()

    if not task:
        task = cleaned

    # Convert to UTC for storage
    parsed_utc = parsed_dt.astimezone(timezone.utc) if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)

    return task, parsed_utc


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    task, remind_at = parse_reminder(text)

    if remind_at is None:
        await update.message.reply_text(
            "I couldn't figure out when to remind you. Try something like:\n"
            '"remind me to buy milk in 2 hours"\n'
            '"call mom tomorrow at 3pm"',
            message_thread_id=update.message.message_thread_id,
        )
        return

    if remind_at <= datetime.now(timezone.utc):
        await update.message.reply_text(
            "That time seems to be in the past. Please specify a future time.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    reminder_id = add_reminder(chat_id, thread_id, task, remind_at)

    await update.message.reply_text(
        f"Got it! I'll remind you to: {task}\n"
        f"When: {remind_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"ID: [{reminder_id}]",
        message_thread_id=thread_id,
    )


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job that checks for due reminders and sends them."""
    pending = get_pending_reminders()
    for r in pending:
        try:
            kwargs = {"chat_id": r["chat_id"], "text": f"🔔 Reminder: {r['text']}"}
            if r["message_thread_id"]:
                kwargs["message_thread_id"] = r["message_thread_id"]
            await context.bot.send_message(**kwargs)
            mark_sent(r["id"])
            logger.info("Sent reminder %d to chat %d", r["id"], r["chat_id"])
        except Exception:
            logger.exception("Failed to send reminder %d", r["id"])


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CommandHandler("cancel", cancel_reminder))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 30 seconds
    app.job_queue.run_repeating(check_reminders, interval=30, first=5)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
