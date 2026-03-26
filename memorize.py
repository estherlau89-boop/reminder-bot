"""Bible verse memorization module with SM-2 spaced repetition."""

import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db
from verses import REVELATION_CHAPTERS, THEMES, VERSES

logger = logging.getLogger(__name__)

# ── Quiz session state (in-memory, lost on restart) ──────────────────────

@dataclass
class QuizSession:
    cards: list[dict]
    current_index: int = 0
    correct: int = 0
    total: int = 0
    hint_used: bool = False
    awaiting_typed: bool = False  # waiting for user to type the verse


active_sessions: dict[int, QuizSession] = {}
# Track last notification per user to avoid spamming
last_notification: dict[int, datetime] = {}


# ── SM-2 Algorithm ───────────────────────────────────────────────────────

def sm2_update(quality: int, ease: float, interval: float, reps: int,
               difficulty: int, streak: int) -> tuple:
    """
    SM-2 spaced repetition update.
    quality: 0-5 (5=perfect, 0=blackout)
    Returns: (new_ease, new_interval_days, new_reps, new_difficulty, new_streak)
    """
    if quality >= 3:  # correct
        new_streak = streak + 1
        if reps == 0:
            new_interval = 0.00347  # ~5 minutes
        elif reps == 1:
            new_interval = 0.0417  # ~1 hour
        elif reps == 2:
            new_interval = 1.0  # 1 day
        else:
            new_interval = interval * ease

        new_ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
        new_reps = reps + 1

        # Advance difficulty after 3 consecutive correct
        new_difficulty = difficulty
        if new_streak >= 3 and difficulty < 4:
            new_difficulty = difficulty + 1
            new_streak = 0  # reset streak for new level
    else:  # incorrect
        new_streak = 0
        new_interval = 0.00347  # back to 5 minutes
        new_ease = max(1.3, ease - 0.2)
        new_reps = 0
        # Drop back one difficulty level
        new_difficulty = max(0, difficulty - 1)

    return new_ease, new_interval, new_reps, new_difficulty, new_streak


# ── Text transformation helpers ──────────────────────────────────────────

def first_letter_hint(text: str) -> str:
    """Convert text to first-letter hints: 'The Lord is' -> 'T__ L___ i_'"""
    words = text.split()
    hints = []
    for word in words:
        # Keep punctuation attached
        if len(word) <= 1:
            hints.append(word)
        else:
            # Find first letter vs punctuation
            letters = [c for c in word if c.isalpha()]
            if letters:
                first = letters[0]
                rest = '_' * (len(word) - 1)
                hints.append(first + rest)
            else:
                hints.append(word)
    return ' '.join(hints)


def fill_in_blank(text: str, blank_ratio: float = 0.3) -> tuple[str, list[str]]:
    """Remove key words (longer words) and return (blanked_text, removed_words)."""
    words = text.split()
    # Pick longer words (5+ chars) as candidates for blanking
    candidates = [(i, w) for i, w in enumerate(words) if len(w) >= 5 and w[0].isalpha()]
    if not candidates:
        candidates = [(i, w) for i, w in enumerate(words) if len(w) >= 3 and w[0].isalpha()]

    n_blanks = max(2, int(len(candidates) * blank_ratio))
    to_blank = random.sample(candidates, min(n_blanks, len(candidates)))
    to_blank.sort(key=lambda x: x[0])

    removed = []
    blanked = list(words)
    for idx, word in to_blank:
        clean_word = re.sub(r'[^\w]', '', word)
        removed.append(clean_word)
        blanked[idx] = '_' * len(word)

    return ' '.join(blanked), removed


def grade_typed_answer(typed: str, actual: str) -> tuple[int, str]:
    """
    Grade a typed verse against the actual text.
    Returns (quality 0-5, feedback_message).
    """
    # Normalize both
    def normalize(s):
        s = s.lower().strip()
        s = re.sub(r'[^\w\s]', '', s)  # remove punctuation
        s = re.sub(r'\s+', ' ', s)
        return s

    typed_n = normalize(typed)
    actual_n = normalize(actual)

    if typed_n == actual_n:
        return 5, "PERFECT! Word for word!"

    # Calculate similarity
    ratio = SequenceMatcher(None, typed_n, actual_n).ratio()

    # Word-level comparison
    typed_words = typed_n.split()
    actual_words = actual_n.split()

    # Count matching words in order
    matcher = SequenceMatcher(None, typed_words, actual_words)
    matching_words = sum(block.size for block in matcher.get_matching_blocks())
    total_words = len(actual_words)
    word_accuracy = matching_words / total_words if total_words > 0 else 0

    # Find missing/wrong words
    wrong_words = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ('replace', 'delete'):
            wrong_words.extend(actual_words[j1:j2])

    if ratio >= 0.95:
        quality = 5
        feedback = f"Almost perfect! ({int(ratio*100)}% match)"
    elif ratio >= 0.85:
        quality = 4
        feedback = f"Great! {int(word_accuracy*100)}% of words correct"
    elif ratio >= 0.70:
        quality = 3
        feedback = f"Good effort! {int(word_accuracy*100)}% of words correct"
    elif ratio >= 0.50:
        quality = 2
        feedback = f"Getting there! {int(word_accuracy*100)}% of words correct"
    elif ratio >= 0.25:
        quality = 1
        feedback = f"Keep practicing! {int(word_accuracy*100)}% of words correct"
    else:
        quality = 0
        feedback = f"Only {int(word_accuracy*100)}% match — review the verse carefully"

    if wrong_words and len(wrong_words) <= 5:
        feedback += f"\nMissed words: {', '.join(wrong_words[:5])}"

    return quality, feedback


# ── Card presentation ────────────────────────────────────────────────────

def format_card_message(card: dict, show_answer: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Format a card for display based on its difficulty level."""
    ref = card["verse_ref"]
    text = VERSES.get(ref, "Verse not found")
    level = card["difficulty_level"]
    card_id = card["id"]

    theme_label = ""
    if card["theme"]:
        theme_label = f" | {card['theme'].title()}"

    if show_answer:
        msg = f"📖 *{ref}*{theme_label}\n\n_{text}_"
        keyboard = [[InlineKeyboardButton("▶️ Next", callback_data=f"m:{card_id}:n:0")]]
        return msg, InlineKeyboardMarkup(keyboard)

    if level == 0:
        # Full text — read and self-rate
        msg = (
            f"📖 *{ref}*{theme_label}\n"
            f"📊 Level: Learning\n\n"
            f"_{text}_\n\n"
            f"How well do you know this verse?"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Know it (5)", callback_data=f"m:{card_id}:q:5"),
                InlineKeyboardButton("🔄 Getting there (3)", callback_data=f"m:{card_id}:q:3"),
            ],
            [
                InlineKeyboardButton("📝 Still learning (1)", callback_data=f"m:{card_id}:q:1"),
            ],
        ]

    elif level == 1:
        # First letter hints
        hint = first_letter_hint(text)
        msg = (
            f"📖 *{ref}*{theme_label}\n"
            f"📊 Level: First Letters\n\n"
            f"`{hint}`\n\n"
            f"Can you fill in the words?"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Got it! (5)", callback_data=f"m:{card_id}:q:5"),
                InlineKeyboardButton("🔄 Mostly (3)", callback_data=f"m:{card_id}:q:3"),
            ],
            [
                InlineKeyboardButton("❌ No idea (1)", callback_data=f"m:{card_id}:q:1"),
                InlineKeyboardButton("👁 Show answer", callback_data=f"m:{card_id}:a:0"),
            ],
            [
                InlineKeyboardButton("⌨️ Type it out", callback_data=f"m:{card_id}:t:0"),
            ],
        ]

    elif level == 2:
        # Fill in the blank
        blanked, removed = fill_in_blank(text)
        msg = (
            f"📖 *{ref}*{theme_label}\n"
            f"📊 Level: Fill in the Blanks\n\n"
            f"`{blanked}`\n\n"
            f"Missing words: ||{', '.join(removed)}||\n"
            f"(Tap the spoiler above to peek)"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Got them all (5)", callback_data=f"m:{card_id}:q:5"),
                InlineKeyboardButton("🔄 Most (3)", callback_data=f"m:{card_id}:q:3"),
            ],
            [
                InlineKeyboardButton("❌ Struggled (1)", callback_data=f"m:{card_id}:q:1"),
                InlineKeyboardButton("👁 Show answer", callback_data=f"m:{card_id}:a:0"),
            ],
            [
                InlineKeyboardButton("⌨️ Type it out", callback_data=f"m:{card_id}:t:0"),
            ],
        ]

    else:  # level >= 3
        # Reference only — recite from memory
        msg = (
            f"📖 *{ref}*{theme_label}\n"
            f"📊 Level: {'Mastered ⭐' if level >= 4 else 'From Memory'}\n\n"
            f"Can you recite this verse from memory?\n"
            f"Take a moment, then rate yourself:"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Perfect (5)", callback_data=f"m:{card_id}:q:5"),
                InlineKeyboardButton("🔄 Most of it (4)", callback_data=f"m:{card_id}:q:4"),
            ],
            [
                InlineKeyboardButton("😅 Some (3)", callback_data=f"m:{card_id}:q:3"),
                InlineKeyboardButton("❌ Little (1)", callback_data=f"m:{card_id}:q:1"),
            ],
            [
                InlineKeyboardButton("👁 Show answer", callback_data=f"m:{card_id}:a:0"),
                InlineKeyboardButton("⌨️ Type it out", callback_data=f"m:{card_id}:t:0"),
            ],
        ]

    return msg, InlineKeyboardMarkup(keyboard)


# ── Command handlers ─────────────────────────────────────────────────────

async def memorize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /memorize command — show menu to add verses."""
    args = context.args
    user_id = update.effective_user.id

    if args:
        arg = " ".join(args).lower().strip()

        # Check if it's a verse reference like "Romans 8:37"
        full_text = " ".join(args).strip()
        if full_text in VERSES:
            added = db.add_verse_card(user_id, full_text)
            if added:
                await update.message.reply_text(f"✅ Added *{full_text}* to your memorization list!", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"You're already memorizing *{full_text}*!", parse_mode="Markdown")
            return

        # Check for theme
        if arg in THEMES:
            refs = THEMES[arg]
            count = db.add_bulk_cards(user_id, refs, theme=arg)
            await update.message.reply_text(
                f"✅ Added *{count}* new verses from *{arg.title()}* theme!\n"
                f"({len(refs) - count} already in your list)\n\n"
                f"Use /review to start studying!",
                parse_mode="Markdown",
            )
            return

        # Check for "rev N" or "revelation N"
        rev_match = re.match(r"(?:rev|revelation)\s*(\d+)", arg)
        if rev_match:
            ch = int(rev_match.group(1))
            if ch in REVELATION_CHAPTERS:
                refs = REVELATION_CHAPTERS[ch]
                theme = f"revelation_ch{ch}"
                count = db.add_bulk_cards(user_id, refs, theme=theme)
                await update.message.reply_text(
                    f"✅ Added *{count}* new verses from *Revelation {ch}*!\n"
                    f"({len(refs) - count} already in your list)\n\n"
                    f"Use /review to start studying!",
                    parse_mode="Markdown",
                )
                return

    # Show menu
    keyboard = []

    # Theme buttons
    for theme, refs in THEMES.items():
        label = f"{theme.title()} ({len(refs)} verses)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ma:{theme}")])

    # Revelation chapter rows (4 per row)
    keyboard.append([InlineKeyboardButton("── Revelation ──", callback_data="ma:noop")])
    row = []
    for ch in sorted(REVELATION_CHAPTERS.keys()):
        count = len(REVELATION_CHAPTERS[ch])
        row.append(InlineKeyboardButton(f"Ch {ch}", callback_data=f"mr:{ch}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("📖 All Revelation (404 verses)", callback_data="mr:all")])

    await update.message.reply_text(
        "📚 *Choose what to memorize:*\n\n"
        "Pick a theme or Revelation chapter to add to your study list.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /review or /quiz — start a review session."""
    user_id = update.effective_user.id
    cards = db.get_due_cards(user_id, limit=10)

    if not cards:
        progress = db.get_user_progress(user_id)
        if progress["total"] == 0:
            await update.message.reply_text(
                "You haven't added any verses yet!\n"
                "Use /memorize to get started."
            )
        else:
            await update.message.reply_text(
                f"🎉 No verses due for review right now!\n\n"
                f"📊 You have {progress['total']} verses, {progress['mastered']} mastered.\n"
                f"Check back later or use /memorize to add more."
            )
        return

    session = QuizSession(cards=cards, total=len(cards))
    active_sessions[user_id] = session

    await update.message.reply_text(
        f"📚 *Review Session*\n"
        f"{len(cards)} verses to review. Let's go!\n",
        parse_mode="Markdown",
    )

    # Send first card
    await send_current_card(update.effective_chat.id, user_id, context)


async def send_current_card(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Send the current card in the session."""
    session = active_sessions.get(user_id)
    if not session or session.current_index >= len(session.cards):
        return

    card = session.cards[session.current_index]
    session.hint_used = False
    session.awaiting_typed = False
    msg, keyboard = format_card_message(card)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"({session.current_index + 1}/{session.total}) " + msg,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def send_session_summary(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Send end-of-session summary."""
    session = active_sessions.get(user_id)
    if not session:
        return

    total = session.total
    correct = session.correct
    pct = int(correct / total * 100) if total > 0 else 0

    # Get next due info
    progress = db.get_user_progress(user_id)

    msg = (
        f"🏁 *Review Complete!*\n\n"
        f"Score: {correct}/{total} ({pct}%)\n"
        f"{'🌟 Excellent!' if pct >= 80 else '💪 Keep going!' if pct >= 50 else '📖 More practice needed!'}\n\n"
        f"📊 Total verses: {progress['total']} | Mastered: {progress['mastered']} | Due: {progress['due']}"
    )

    del active_sessions[user_id]
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /progress — show memorization stats."""
    user_id = update.effective_user.id
    overall = db.get_user_progress(user_id)

    if overall["total"] == 0:
        await update.message.reply_text("You haven't started memorizing yet! Use /memorize to begin.")
        return

    lines = ["📊 *Your Memorization Progress*\n"]

    # Theme progress
    for theme in THEMES:
        tp = db.get_theme_progress(user_id, theme)
        if tp["total"] > 0:
            bar_len = 10
            filled = int(tp["mastered"] / tp["total"] * bar_len) if tp["total"] > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"*{theme.title()}*: {tp['mastered']}/{tp['total']} mastered")
            lines.append(f"`[{bar}]` {tp['learning']} learning\n")

    # Revelation progress
    rev_lines = []
    rev_total = 0
    rev_mastered = 0
    for ch in sorted(REVELATION_CHAPTERS.keys()):
        tp = db.get_theme_progress(user_id, f"revelation_ch{ch}")
        if tp["total"] > 0:
            rev_total += tp["total"]
            rev_mastered += tp["mastered"]
            status = "✅" if tp["mastered"] == tp["total"] else f"{tp['mastered']}/{tp['total']}"
            rev_lines.append(f"Ch {ch}: {status}")

    if rev_lines:
        lines.append(f"*Revelation*: {rev_mastered}/{rev_total} mastered")
        lines.append(", ".join(rev_lines) + "\n")

    # Overall
    lines.append(f"*Overall*: {overall['total']} verses | {overall['mastered']} mastered | {overall['due']} due now")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def verse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /verse — look up a verse on demand."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /verse Romans 8:37\n"
            "Or: /verse Revelation 1:1"
        )
        return

    ref = " ".join(context.args).strip()
    text = VERSES.get(ref)
    if text:
        await update.message.reply_text(f"📖 *{ref}*\n\n_{text}_", parse_mode="Markdown")
    else:
        # Try fuzzy match
        matches = [r for r in VERSES if ref.lower() in r.lower()]
        if matches:
            suggestions = "\n".join(f"• {m}" for m in matches[:5])
            await update.message.reply_text(f"Verse not found. Did you mean:\n{suggestions}")
        else:
            await update.message.reply_text(f"Verse *{ref}* not found in the database.", parse_mode="Markdown")


# ── Callback handler ─────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard callbacks for memorization."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # /memorize menu callbacks
    if data.startswith("ma:"):
        theme = data[3:]
        if theme == "noop":
            return
        refs = THEMES.get(theme, [])
        if refs:
            count = db.add_bulk_cards(user_id, refs, theme=theme)
            await query.edit_message_text(
                f"✅ Added *{count}* new verses from *{theme.title()}*!\n"
                f"({len(refs) - count} already in your list)\n\n"
                f"Use /review to start studying!",
                parse_mode="Markdown",
            )
        return

    if data.startswith("mr:"):
        ch_str = data[3:]
        if ch_str == "all":
            total_added = 0
            for ch in sorted(REVELATION_CHAPTERS.keys()):
                refs = REVELATION_CHAPTERS[ch]
                total_added += db.add_bulk_cards(user_id, refs, theme=f"revelation_ch{ch}")
            await query.edit_message_text(
                f"✅ Added *{total_added}* new Revelation verses!\n\n"
                f"Use /review to start studying!",
                parse_mode="Markdown",
            )
        else:
            ch = int(ch_str)
            refs = REVELATION_CHAPTERS.get(ch, [])
            theme = f"revelation_ch{ch}"
            count = db.add_bulk_cards(user_id, refs, theme=theme)
            await query.edit_message_text(
                f"✅ Added *{count}* new verses from *Revelation {ch}*!\n"
                f"({len(refs) - count} already in your list)\n\n"
                f"Use /review to start studying!",
                parse_mode="Markdown",
            )
        return

    # Quiz callbacks: m:{card_id}:{action}:{value}
    if data.startswith("m:"):
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, card_id_str, action, value = parts
        card_id = int(card_id_str)
        card = db.get_card_by_id(card_id)
        if not card:
            return

        session = active_sessions.get(user_id)

        if action == "q":
            # Quality rating
            quality = int(value)
            if session and session.hint_used:
                quality = min(quality, 3)

            # Apply SM-2
            new_ease, new_interval, new_reps, new_diff, new_streak = sm2_update(
                quality, card["ease_factor"], card["interval_days"],
                card["repetitions"], card["difficulty_level"], card["streak"],
            )
            next_review = (datetime.now(timezone.utc) + timedelta(days=new_interval)).isoformat()

            db.update_card_sm2(card_id, new_ease, new_interval, new_reps, next_review, new_diff, new_streak)
            db.log_review(user_id, card["verse_ref"], quality, card["difficulty_level"])

            if session:
                if quality >= 3:
                    session.correct += 1

                # Show brief feedback
                interval_str = format_interval(new_interval)
                diff_change = ""
                if new_diff > card["difficulty_level"]:
                    diff_change = " ⬆️ Level up!"
                elif new_diff < card["difficulty_level"]:
                    diff_change = " ⬇️ Dropped a level"

                feedback = f"{'✅' if quality >= 3 else '❌'} Next review: {interval_str}{diff_change}"
                await query.edit_message_text(feedback)

                # Next card
                session.current_index += 1
                if session.current_index < len(session.cards):
                    await send_current_card(update.effective_chat.id, user_id, context)
                else:
                    await send_session_summary(update.effective_chat.id, user_id, context)
            else:
                interval_str = format_interval(new_interval)
                await query.edit_message_text(f"{'✅' if quality >= 3 else '❌'} Reviewed! Next: {interval_str}")

        elif action == "a":
            # Show answer
            if session:
                session.hint_used = True
            msg, keyboard = format_card_message(card, show_answer=True)
            await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")

        elif action == "t":
            # Type it out mode
            if session:
                session.awaiting_typed = True
                session.hint_used = False
            ref = card["verse_ref"]
            await query.edit_message_text(
                f"⌨️ *Type out {ref}* from memory:\n\n"
                f"(Just type the verse text and send it as a message)",
                parse_mode="Markdown",
            )

        elif action == "n":
            # Next card (after show answer)
            if session:
                session.current_index += 1
                if session.current_index < len(session.cards):
                    await send_current_card(update.effective_chat.id, user_id, context)
                else:
                    await send_session_summary(update.effective_chat.id, user_id, context)


async def handle_typed_verse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if the user is in a typing quiz and grade their response.
    Returns True if the message was handled, False otherwise.
    """
    user_id = update.effective_user.id
    session = active_sessions.get(user_id)

    if not session or not session.awaiting_typed:
        return False

    if session.current_index >= len(session.cards):
        return False

    card = session.cards[session.current_index]
    actual_text = VERSES.get(card["verse_ref"], "")
    typed_text = update.message.text.strip()

    # Grade the typed answer
    quality, feedback = grade_typed_answer(typed_text, actual_text)
    session.awaiting_typed = False

    # Apply SM-2
    new_ease, new_interval, new_reps, new_diff, new_streak = sm2_update(
        quality, card["ease_factor"], card["interval_days"],
        card["repetitions"], card["difficulty_level"], card["streak"],
    )
    next_review = (datetime.now(timezone.utc) + timedelta(days=new_interval)).isoformat()

    db.update_card_sm2(card["id"], new_ease, new_interval, new_reps, next_review, new_diff, new_streak)
    db.log_review(user_id, card["verse_ref"], quality, card["difficulty_level"])

    if quality >= 3:
        session.correct += 1

    interval_str = format_interval(new_interval)
    diff_change = ""
    if new_diff > card["difficulty_level"]:
        diff_change = "\n⬆️ Level up!"
    elif new_diff < card["difficulty_level"]:
        diff_change = "\n⬇️ Dropped a level"

    grade_emoji = ["💀", "😰", "😐", "🙂", "😄", "🌟"][quality]

    msg = (
        f"{grade_emoji} *{card['verse_ref']}*\n\n"
        f"{feedback}\n"
        f"Next review: {interval_str}{diff_change}\n\n"
        f"*Actual verse:*\n_{actual_text}_"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")

    # Next card
    session.current_index += 1
    if session.current_index < len(session.cards):
        await send_current_card(update.effective_chat.id, user_id, context)
    else:
        await send_session_summary(update.effective_chat.id, user_id, context)

    return True


# ── Review notification job ──────────────────────────────────────────────

async def check_due_reviews(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job to notify users about due reviews."""
    # Get all distinct user_ids that have due cards
    conn = db.get_connection()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT user_id, COUNT(*) as due_count FROM verse_cards WHERE next_review <= ? GROUP BY user_id",
        (now,),
    ).fetchall()
    conn.close()

    for row in rows:
        user_id = row["user_id"]
        due_count = row["due_count"]

        if due_count == 0:
            continue

        # Don't notify if we already did recently (30 min cooldown)
        last = last_notification.get(user_id)
        if last and (datetime.now(timezone.utc) - last).total_seconds() < 1800:
            continue

        # Don't notify if user has an active session
        if user_id in active_sessions:
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📚 You have *{due_count}* verses due for review!\n"
                     f"Use /review to start your session.",
                parse_mode="Markdown",
            )
            last_notification[user_id] = datetime.now(timezone.utc)
        except Exception as e:
            logger.warning("Failed to notify user %s: %s", user_id, e)


# ── Helpers ──────────────────────────────────────────────────────────────

def format_interval(days: float) -> str:
    """Format interval in days to human-readable string."""
    if days < 0.007:  # < ~10 min
        minutes = int(days * 24 * 60)
        return f"{max(1, minutes)} min"
    elif days < 0.042:  # < 1 hour
        minutes = int(days * 24 * 60)
        return f"{minutes} min"
    elif days < 1:
        hours = round(days * 24, 1)
        return f"{hours} hr{'s' if hours != 1 else ''}"
    elif days < 7:
        d = round(days, 1)
        return f"{d} day{'s' if d != 1 else ''}"
    elif days < 30:
        weeks = round(days / 7, 1)
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    else:
        months = round(days / 30, 1)
        return f"{months} month{'s' if months != 1 else ''}"
