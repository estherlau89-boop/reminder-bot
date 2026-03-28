import logging
import os
import re
from datetime import datetime, timezone, timedelta

import dateparser

# Default timezone offset for the user (GMT+7)
USER_TZ_OFFSET = timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "7")))
USER_TZ = timezone(USER_TZ_OFFSET)
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import add_reminder, delete_reminder, get_pending_reminders, get_user_reminders, init_db, init_memorize_db, mark_sent
from database import init_exam_db
import memorize
import exam

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your reminder &amp; Bible memorization bot.\n\n"
        "📝 <b>Reminders</b> — just send me a message like:\n"
        '  "remind me to buy milk in 2 hours"\n'
        '  "call mom tomorrow at 3pm"\n\n'
        "📖 <b>Bible Memorization</b>\n"
        "/memorize - add verses to study\n"
        "/review - review due verses\n"
        "/progress - see your stats\n"
        "/verse Romans 8:37 - look up a verse\n\n"
        "📝 <b>Exam Practice</b>\n"
        "/exam - exam menu\n"
        "/addq - add questions\n"
        "/test - test yourself\n"
        "/scores - view past scores\n\n"
        "📋 <b>Reminders</b>\n"
        "/list - see upcoming reminders\n"
        "/cancel &lt;id&gt; - cancel a reminder",
        message_thread_id=update.message.message_thread_id,
        parse_mode="HTML",
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

    # Check if user is in an exam session or adding questions first
    if await exam.handle_exam_text(update, context):
        return

    # Check if user is in a typing quiz session
    if await memorize.handle_typed_verse(update, context):
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


async def post_init(app: Application):
    """Set bot commands after startup."""
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome message"),
        BotCommand("list", "See upcoming reminders"),
        BotCommand("cancel", "Cancel a reminder"),
        BotCommand("memorize", "Add Bible verses to memorize"),
        BotCommand("review", "Review due verses"),
        BotCommand("quiz", "Start a quiz session"),
        BotCommand("progress", "Check memorization progress"),
        BotCommand("verse", "Look up a verse"),
        BotCommand("exam", "Exam practice menu"),
        BotCommand("addq", "Add exam questions"),
        BotCommand("test", "Start an exam test"),
        BotCommand("scores", "View exam scores"),
        BotCommand("done", "Finish adding questions"),
    ])


def main():
    init_db()
    init_memorize_db()
    init_exam_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Reminder commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CommandHandler("cancel", cancel_reminder))

    # Memorization commands
    app.add_handler(CommandHandler("memorize", memorize.memorize_command))
    app.add_handler(CommandHandler("review", memorize.review_command))
    app.add_handler(CommandHandler("quiz", memorize.review_command))
    app.add_handler(CommandHandler("progress", memorize.progress_command))
    app.add_handler(CommandHandler("verse", memorize.verse_command))

    # Exam commands
    app.add_handler(CommandHandler("exam", exam.exam_command))
    app.add_handler(CommandHandler("addq", exam.addq_command))
    app.add_handler(CommandHandler("test", exam.test_command))
    app.add_handler(CommandHandler("scores", exam.scores_command))
    app.add_handler(CommandHandler("done", exam.done_command))

    # Callback handlers for inline keyboards
    app.add_handler(CallbackQueryHandler(exam.exam_callback_handler, pattern=r"^e[xt]:"))
    app.add_handler(CallbackQueryHandler(memorize.callback_handler))

    # Text message handler (reminders + typed verse answers)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 30 seconds
    app.job_queue.run_repeating(check_reminders, interval=30, first=5)
    # Check for due verse reviews every 60 seconds
    app.job_queue.run_repeating(memorize.check_due_reviews, interval=60, first=15)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
