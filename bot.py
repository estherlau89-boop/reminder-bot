import logging
import os
import re
from datetime import datetime, timezone, timedelta

import dateparser

# Default timezone offset for the user (GMT+7)
USER_TZ_OFFSET = timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "7")))
USER_TZ = timezone(USER_TZ_OFFSET)
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


def extract_time_and_task(text: str) -> tuple[str, str]:
    """Split text into (task, time_phrase) by matching common time expressions."""
    # Patterns ordered from most specific to least — match the TIME part
    time_patterns = [
        # "in 2 hours", "in 30 minutes", "in 1 min"
        (r"(?:^|\s)(in\s+\d+\s+(?:minutes?|hours?|days?|weeks?|months?|mins?|hrs?|seconds?|secs?))\s*\.?$", True),
        # "tomorrow at 3pm", "today at 5pm"
        (r"(?:^|\s)((?:today|tomorrow|tonight)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)(?:\s*\.?$|\s)", True),
        # "next monday at 3pm"
        (r"(?:^|\s)((?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)(?:\s*\.?$|\s)", True),
        # "at 5pm", "at 3:30 pm", "at 17:00"
        (r"(?:^|\s)(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)(?:\s*\.?$|\s)", True),
        # "at 17:00" (24h format)
        (r"(?:^|\s)(at\s+\d{1,2}:\d{2})(?:\s*\.?$|\s)", True),
        # "6pm", "3:30pm"  (standalone)
        (r"(?:^|\s)(\d{1,2}(?::\d{2})?\s*(?:am|pm))(?:\s*\.?$|\s)", True),
        # "in 2 hours" in the middle of text
        (r"\s(in\s+\d+\s+(?:minutes?|hours?|days?|weeks?|months?|mins?|hrs?|seconds?|secs?))\s", False),
        # "after 30 minutes"
        (r"(?:^|\s)(after\s+\d+\s+(?:minutes?|hours?|days?|weeks?|months?|mins?|hrs?|seconds?|secs?))(?:\s*\.?$|\s)", True),
    ]

    for pattern, _ in time_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            time_str = match.group(1).strip()
            # Remove the time phrase from text to get the task
            task = text[:match.start(1)] + text[match.end(1):]
            # Clean up
            task = re.sub(r"\s+", " ", task).strip()
            task = re.sub(r"^[\s,.\-]+|[\s,.\-]+$", "", task)
            task = re.sub(r"\s+(at|on|in|by|before|after|for)\s*$", "", task, flags=re.IGNORECASE).strip()
            if task:
                return task, time_str

    return text, ""


def parse_reminder(text: str) -> tuple[str, datetime | None]:
    """Extract a reminder description and a future time from natural language text."""
    # Strip common prefixes
    cleaned = re.sub(r"^(remind\s+me\s+(to\s+)?|reminder\s+(to\s+)?|pls\s+|please\s+)", "", text, flags=re.IGNORECASE).strip()
    # Also strip trailing filler
    cleaned = re.sub(r"\s*\.?\s*$", "", cleaned).strip()

    now_user = datetime.now(USER_TZ).replace(tzinfo=None)
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now_user,
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    # Extract the time phrase from the text
    task, time_phrase = extract_time_and_task(cleaned)
    logger.info("Extracted task='%s', time_phrase='%s' from '%s'", task, time_phrase, cleaned)

    parsed_dt = None

    # Parse the extracted time phrase
    if time_phrase:
        parsed_dt = dateparser.parse(time_phrase, settings=settings)
        logger.info("dateparser('%s') -> %s", time_phrase, parsed_dt)

    # Fallback: try parsing the whole cleaned text
    if parsed_dt is None:
        parsed_dt = dateparser.parse(cleaned, settings=settings)
        logger.info("dateparser fallback('%s') -> %s", cleaned, parsed_dt)
        if parsed_dt is not None:
            task = cleaned

    if parsed_dt is None:
        return cleaned, None

    if not task:
        task = cleaned

    # Parsed time is in user's local timezone, convert to UTC for storage
    parsed_local = parsed_dt.replace(tzinfo=USER_TZ)
    parsed_utc = parsed_local.astimezone(timezone.utc)

    logger.info("Final: task='%s', local=%s, utc=%s", task, parsed_local, parsed_utc)

    return task, parsed_utc


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    try:
        task, remind_at = parse_reminder(text)
    except Exception as e:
        logger.exception("Error parsing reminder: %s", text)
        await update.message.reply_text(
            f"Error parsing your message: {e}",
            message_thread_id=update.message.message_thread_id,
        )
        return

    logger.info("Parsed '%s' -> task='%s', remind_at=%s", text, task, remind_at)

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

    # Show time in user's local timezone
    local_time = remind_at.astimezone(USER_TZ)

    await update.message.reply_text(
        f"Got it! I'll remind you to: {task}\n"
        f"When: {local_time.strftime('%Y-%m-%d %I:%M %p')} (GMT+7)\n"
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
