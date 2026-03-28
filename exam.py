"""Exam question testing module for Telegram bot."""

import logging
import random
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db

logger = logging.getLogger(__name__)


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Session state ────────────────────────────────────────────────────────

@dataclass
class ExamSession:
    """Tracks an active test session."""
    set_id: int
    questions: list[dict] = field(default_factory=list)
    current_index: int = 0
    total: int = 0
    correct: int = 0
    answers: list[dict] = field(default_factory=list)  # [{qid, correct, user_answer}]


# user_id -> ExamSession
active_exam_sessions: dict[int, ExamSession] = {}

# user_id -> state for adding questions
# state: {"mode": "awaiting_name"} or {"mode": "awaiting_questions", "set_id": X, "set_name": Y, "count": N}
adding_state: dict[int, dict] = {}


# ── Adding questions ─────────────────────────────────────────────────────

async def exam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /exam — show exam menu."""
    user_id = update.effective_user.id
    sets = db.get_exam_sets(user_id)

    keyboard = [
        [InlineKeyboardButton("➕ Add new question set", callback_data="ex:new")],
    ]

    if sets:
        for s in sets:
            label = f"📝 {s['name']} ({s['question_count']}q)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ex:set:{s['id']}")])

    msg = (
        "📝 <b>Exam Practice</b>\n\n"
        "Add question sets and test yourself!\n\n"
        "<b>Commands:</b>\n"
        "/exam - this menu\n"
        "/addq - add questions to a set\n"
        "/test - start a test\n"
        "/scores - view past scores"
    )

    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def addq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addq — start adding questions."""
    user_id = update.effective_user.id
    args = context.args

    if args:
        # /addq SetName — use existing or create new set
        set_name = " ".join(args).strip()
        set_id = db.create_exam_set(user_id, set_name)
        adding_state[user_id] = {
            "mode": "awaiting_questions",
            "set_id": set_id,
            "set_name": set_name,
            "count": 0,
        }
        await update.message.reply_text(
            f"📝 Adding questions to <b>{html_escape(set_name)}</b>\n\n"
            "Send your questions in this format:\n\n"
            "<code>Q: What is the capital of France?\n"
            "A: Paris</code>\n\n"
            "You can send multiple Q&amp;A pairs in one message!\n"
            "Send /done when finished.",
            parse_mode="HTML",
        )
    else:
        # Ask for set name
        adding_state[user_id] = {"mode": "awaiting_name"}
        await update.message.reply_text(
            "What do you want to name this question set?\n"
            "(e.g. \"Biology Chapter 3\", \"History Quiz\")"
        )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /test — start a test session."""
    user_id = update.effective_user.id
    args = context.args
    sets = db.get_exam_sets(user_id)

    if not sets:
        await update.message.reply_text(
            "You don't have any question sets yet!\n"
            "Use /addq to add questions first."
        )
        return

    # If set name provided, find it
    if args:
        set_name = " ".join(args).strip().lower()
        matching = [s for s in sets if s["name"].lower() == set_name]
        if matching:
            await start_test(update, context, matching[0]["id"])
            return

    # Show set selection
    keyboard = []
    for s in sets:
        if s["question_count"] > 0:
            label = f"📝 {s['name']} ({s['question_count']}q)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"et:start:{s['id']}")])

    if not keyboard:
        await update.message.reply_text(
            "Your question sets are all empty!\n"
            "Use /addq to add questions first."
        )
        return

    await update.message.reply_text(
        "📝 <b>Choose a set to test:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def start_test(update: Update, context: ContextTypes.DEFAULT_TYPE, set_id: int):
    """Start a test session for a given set."""
    user_id = update.effective_user.id
    questions = db.get_exam_questions(set_id)
    exam_set = db.get_exam_set_by_id(set_id)

    if not questions:
        msg = "This set has no questions! Use /addq to add some."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    random.shuffle(questions)

    session = ExamSession(
        set_id=set_id,
        questions=questions,
        total=len(questions),
    )
    active_exam_sessions[user_id] = session

    msg = (
        f"📝 <b>Test: {html_escape(exam_set['name'])}</b>\n"
        f"{len(questions)} questions. Type your answers!\n"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML")
    else:
        await update.message.reply_text(msg, parse_mode="HTML")

    # Send first question
    await send_exam_question(update.effective_chat.id, user_id, context)


async def send_exam_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Send the current exam question."""
    session = active_exam_sessions.get(user_id)
    if not session or session.current_index >= len(session.questions):
        return

    q = session.questions[session.current_index]
    num = session.current_index + 1
    total = session.total

    msg = (
        f"<b>Question {num}/{total}</b>\n\n"
        f"{html_escape(q['question'])}\n\n"
        f"💬 Type your answer:"
    )

    keyboard = [[InlineKeyboardButton("⏭ Skip", callback_data=f"et:skip:{q['id']}")]]

    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def handle_exam_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if user is in an exam session and grade their answer.
    Returns True if handled, False otherwise.
    """
    user_id = update.effective_user.id
    session = active_exam_sessions.get(user_id)

    if not session:
        return False

    if session.current_index >= len(session.questions):
        return False

    q = session.questions[session.current_index]
    user_answer = update.message.text.strip()
    correct_answer = q["answer"].strip()

    # Grade the answer
    is_correct = grade_answer(user_answer, correct_answer)

    db.update_question_stats(q["id"], is_correct)

    session.answers.append({
        "qid": q["id"],
        "correct": is_correct,
        "user_answer": user_answer,
        "correct_answer": correct_answer,
        "question": q["question"],
    })

    if is_correct:
        session.correct += 1
        feedback = f"✅ <b>Correct!</b>"
    else:
        feedback = (
            f"❌ <b>Wrong</b>\n\n"
            f"Your answer: {html_escape(user_answer)}\n"
            f"✅ Correct answer: <b>{html_escape(correct_answer)}</b>"
        )

    await update.message.reply_text(feedback, parse_mode="HTML")

    # Next question or finish
    session.current_index += 1
    if session.current_index < len(session.questions):
        await send_exam_question(update.effective_chat.id, user_id, context)
    else:
        await send_exam_summary(update.effective_chat.id, user_id, context)

    return True


def grade_answer(user_answer: str, correct_answer: str) -> bool:
    """Grade a user's answer against the correct answer. Flexible matching."""
    # Normalize
    def normalize(s):
        s = s.lower().strip()
        s = re.sub(r'[^\w\s]', '', s)  # remove punctuation
        s = re.sub(r'\s+', ' ', s)
        return s

    user_n = normalize(user_answer)
    correct_n = normalize(correct_answer)

    # Exact match
    if user_n == correct_n:
        return True

    # High similarity (>85%)
    ratio = SequenceMatcher(None, user_n, correct_n).ratio()
    if ratio >= 0.85:
        return True

    # Check if correct answer contains multiple acceptable answers (separated by / or ;)
    alternatives = re.split(r'[/;]', correct_answer)
    for alt in alternatives:
        alt_n = normalize(alt)
        if user_n == alt_n or SequenceMatcher(None, user_n, alt_n).ratio() >= 0.85:
            return True

    return False


async def send_exam_summary(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Send test results summary."""
    session = active_exam_sessions.get(user_id)
    if not session:
        return

    total = session.total
    correct = session.correct
    pct = int(correct / total * 100) if total > 0 else 0

    # Save result
    db.save_exam_result(user_id, session.set_id, total, correct)

    # Grade
    if pct >= 90:
        grade = "A+ 🌟"
    elif pct >= 80:
        grade = "A 🎉"
    elif pct >= 70:
        grade = "B 👍"
    elif pct >= 60:
        grade = "C 🙂"
    elif pct >= 50:
        grade = "D 😅"
    else:
        grade = "F 📖"

    msg = (
        f"🏁 <b>Test Complete!</b>\n\n"
        f"Score: <b>{correct}/{total}</b> ({pct}%)\n"
        f"Grade: <b>{grade}</b>\n"
    )

    # Show wrong answers for review
    wrong = [a for a in session.answers if not a["correct"]]
    if wrong:
        msg += f"\n❌ <b>Questions you got wrong ({len(wrong)}):</b>\n\n"
        for i, a in enumerate(wrong, 1):
            msg += (
                f"{i}. {html_escape(a['question'])}\n"
                f"   Your answer: {html_escape(a['user_answer'])}\n"
                f"   ✅ Correct: <b>{html_escape(a['correct_answer'])}</b>\n\n"
            )
    else:
        msg += "\n🌟 Perfect score! You got everything right!"

    # Truncate if too long for Telegram (4096 char limit)
    if len(msg) > 4000:
        msg = msg[:3950] + "\n\n... (truncated)"

    del active_exam_sessions[user_id]
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")


async def scores_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scores — show past test results."""
    user_id = update.effective_user.id
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT r.*, s.name as set_name
           FROM exam_results r JOIN exam_sets s ON r.set_id = s.id
           WHERE r.user_id=? ORDER BY r.taken_at DESC LIMIT 20""",
        (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "No test results yet! Use /test to take a test."
        )
        return

    lines = ["📊 <b>Your Test Scores</b>\n"]
    for r in rows:
        pct = int(r["correct"] / r["total"] * 100) if r["total"] > 0 else 0
        emoji = "🌟" if pct >= 80 else "👍" if pct >= 60 else "📖"
        lines.append(
            f"{emoji} <b>{html_escape(r['set_name'])}</b>: "
            f"{r['correct']}/{r['total']} ({pct}%) — {r['taken_at'][:10]}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /done — finish adding questions."""
    user_id = update.effective_user.id
    state = adding_state.pop(user_id, None)

    if not state or state["mode"] != "awaiting_questions":
        await update.message.reply_text("Nothing to finish! Use /addq to start adding questions.")
        return

    await update.message.reply_text(
        f"✅ Done! Added <b>{state['count']}</b> questions to <b>{html_escape(state['set_name'])}</b>.\n\n"
        f"Use /test to start testing yourself!",
        parse_mode="HTML",
    )


# ── Text message handler ─────────────────────────────────────────────────

async def handle_exam_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle text messages for exam features.
    Returns True if handled, False otherwise.
    """
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Check if in test session first
    if user_id in active_exam_sessions:
        return await handle_exam_answer(update, context)

    # Check if adding questions
    state = adding_state.get(user_id)
    if not state:
        return False

    if state["mode"] == "awaiting_name":
        # User is providing set name
        set_name = text.strip()
        if not set_name:
            await update.message.reply_text("Please provide a name for the question set.")
            return True

        set_id = db.create_exam_set(user_id, set_name)
        adding_state[user_id] = {
            "mode": "awaiting_questions",
            "set_id": set_id,
            "set_name": set_name,
            "count": 0,
        }
        await update.message.reply_text(
            f"📝 Created set <b>{html_escape(set_name)}</b>\n\n"
            "Now send your questions in this format:\n\n"
            "<code>Q: What is the capital of France?\n"
            "A: Paris</code>\n\n"
            "You can send multiple Q&amp;A pairs in one message!\n"
            "Send /done when finished.",
            parse_mode="HTML",
        )
        return True

    elif state["mode"] == "awaiting_questions":
        # Parse Q&A pairs from the message
        set_id = state["set_id"]
        pairs = parse_qa_pairs(text)

        if not pairs:
            await update.message.reply_text(
                "I couldn't find any Q&A pairs. Use this format:\n\n"
                "<code>Q: Your question here?\n"
                "A: The answer</code>\n\n"
                "Or number them:\n"
                "<code>1. Question?\n"
                "Answer\n\n"
                "2. Question?\n"
                "Answer</code>\n\n"
                "Send /done when finished.",
                parse_mode="HTML",
            )
            return True

        for q, a in pairs:
            db.add_exam_question(set_id, q, a)
            state["count"] += 1

        await update.message.reply_text(
            f"✅ Added {len(pairs)} question(s)! (Total: {state['count']})\n"
            f"Send more or /done to finish."
        )
        return True

    return False


def parse_qa_pairs(text: str) -> list[tuple[str, str]]:
    """Parse Q&A pairs from text. Supports multiple formats."""
    pairs = []

    # Format 1: Q: ... A: ... (can have multiple)
    qa_pattern = re.findall(
        r'(?:^|\n)\s*Q[:\.]?\s*(.+?)(?:\n)\s*A[:\.]?\s*(.+?)(?=\n\s*Q[:\.]?\s|\Z)',
        text, re.IGNORECASE | re.DOTALL
    )
    if qa_pattern:
        for q, a in qa_pattern:
            q = q.strip()
            a = a.strip()
            if q and a:
                pairs.append((q, a))
        return pairs

    # Format 2: Numbered questions with answer on next line
    # 1. Question?
    # Answer
    numbered = re.findall(
        r'(?:^|\n)\s*\d+[.)]\s*(.+?)(?:\n)\s*([^\d\n].+?)(?=\n\s*\d+[.)]\s|\Z)',
        text, re.DOTALL
    )
    if numbered:
        for q, a in numbered:
            q = q.strip()
            a = a.strip()
            if q and a:
                pairs.append((q, a))
        return pairs

    # Format 3: Simple two-line format (question\nanswer)
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if len(lines) == 2:
        pairs.append((lines[0], lines[1]))
        return pairs

    # Format 4: Lines separated by blank lines, each group is Q then A
    groups = re.split(r'\n\s*\n', text.strip())
    for group in groups:
        group_lines = [l.strip() for l in group.strip().split('\n') if l.strip()]
        if len(group_lines) >= 2:
            q = group_lines[0]
            a = ' '.join(group_lines[1:])
            # Remove leading Q:/A: if present
            q = re.sub(r'^Q[:\.]?\s*', '', q, flags=re.IGNORECASE).strip()
            a = re.sub(r'^A[:\.]?\s*', '', a, flags=re.IGNORECASE).strip()
            if q and a:
                pairs.append((q, a))

    return pairs


# ── Callback handler ─────────────────────────────────────────────────────

async def exam_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks for exam features."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "ex:new":
        adding_state[user_id] = {"mode": "awaiting_name"}
        await query.edit_message_text(
            "What do you want to name this question set?\n"
            "(e.g. \"Biology Chapter 3\", \"History Quiz\")"
        )
        return

    if data.startswith("ex:set:"):
        set_id = int(data.split(":")[2])
        exam_set = db.get_exam_set_by_id(set_id)
        questions = db.get_exam_questions(set_id)

        if not exam_set:
            await query.edit_message_text("Set not found.")
            return

        msg = f"📝 <b>{html_escape(exam_set['name'])}</b>\n{len(questions)} questions\n"

        # Show first few questions as preview
        for i, q in enumerate(questions[:5], 1):
            msg += f"\n{i}. {html_escape(q['question'][:60])}"
        if len(questions) > 5:
            msg += f"\n... and {len(questions) - 5} more"

        keyboard = [
            [InlineKeyboardButton("🎯 Test me!", callback_data=f"et:start:{set_id}")],
            [InlineKeyboardButton("➕ Add more questions", callback_data=f"et:addmore:{set_id}")],
            [InlineKeyboardButton("🗑 Delete set", callback_data=f"et:del:{set_id}")],
            [InlineKeyboardButton("◀ Back", callback_data="et:back")],
        ]

        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    if data.startswith("et:start:"):
        set_id = int(data.split(":")[2])
        await start_test(update, context, set_id)
        return

    if data.startswith("et:addmore:"):
        set_id = int(data.split(":")[2])
        exam_set = db.get_exam_set_by_id(set_id)
        if exam_set:
            adding_state[user_id] = {
                "mode": "awaiting_questions",
                "set_id": set_id,
                "set_name": exam_set["name"],
                "count": 0,
            }
            await query.edit_message_text(
                f"📝 Adding questions to <b>{html_escape(exam_set['name'])}</b>\n\n"
                "Send your questions in this format:\n\n"
                "<code>Q: Your question?\n"
                "A: The answer</code>\n\n"
                "Send /done when finished.",
                parse_mode="HTML",
            )
        return

    if data.startswith("et:del:"):
        set_id = int(data.split(":")[2])
        exam_set = db.get_exam_set_by_id(set_id)
        if exam_set:
            keyboard = [
                [InlineKeyboardButton("⚠️ Yes, delete it", callback_data=f"et:confirm_del:{set_id}")],
                [InlineKeyboardButton("◀ Cancel", callback_data=f"ex:set:{set_id}")],
            ]
            await query.edit_message_text(
                f"Are you sure you want to delete <b>{html_escape(exam_set['name'])}</b> and all its questions?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        return

    if data.startswith("et:confirm_del:"):
        set_id = int(data.split(":")[2])
        exam_set = db.get_exam_set_by_id(set_id)
        name = exam_set["name"] if exam_set else "Unknown"
        db.delete_exam_set(set_id)
        await query.edit_message_text(f"🗑 Deleted <b>{html_escape(name)}</b>.", parse_mode="HTML")
        return

    if data == "et:back":
        # Rebuild exam menu
        sets = db.get_exam_sets(user_id)
        keyboard = [
            [InlineKeyboardButton("➕ Add new question set", callback_data="ex:new")],
        ]
        for s in sets:
            label = f"📝 {s['name']} ({s['question_count']}q)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ex:set:{s['id']}")])

        await query.edit_message_text(
            "📝 <b>Exam Practice</b>\n\nChoose a set or create a new one.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    if data.startswith("et:skip:"):
        qid = int(data.split(":")[2])
        session = active_exam_sessions.get(user_id)
        if session and session.current_index < len(session.questions):
            q = session.questions[session.current_index]
            session.answers.append({
                "qid": q["id"],
                "correct": False,
                "user_answer": "(skipped)",
                "correct_answer": q["answer"],
                "question": q["question"],
            })
            db.update_question_stats(q["id"], False)

            await query.edit_message_text(
                f"⏭ Skipped\n✅ Answer: <b>{html_escape(q['answer'])}</b>",
                parse_mode="HTML",
            )

            session.current_index += 1
            if session.current_index < len(session.questions):
                await send_exam_question(update.effective_chat.id, user_id, context)
            else:
                await send_exam_summary(update.effective_chat.id, user_id, context)
        return
