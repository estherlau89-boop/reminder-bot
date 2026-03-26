import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "reminders.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def add_reminder(chat_id: int, message_thread_id: int | None, text: str, remind_at: datetime) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders (chat_id, message_thread_id, text, remind_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, message_thread_id, text, remind_at.isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    reminder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return reminder_id


def get_pending_reminders() -> list[dict]:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE sent = 0 AND remind_at <= ?", (now,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_user_reminders(chat_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE chat_id = ? AND sent = 0 ORDER BY remind_at ASC",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_reminder(reminder_id: int) -> bool:
    conn = get_connection()
    cursor = conn.execute("DELETE FROM reminders WHERE id = ? AND sent = 0", (reminder_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def mark_sent(reminder_id: int):
    conn = get_connection()
    conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


# --------------- Memorization tables ---------------

def init_memorize_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verse_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            verse_ref TEXT NOT NULL,
            theme TEXT,
            ease_factor REAL NOT NULL DEFAULT 2.5,
            interval_days REAL NOT NULL DEFAULT 0,
            repetitions INTEGER NOT NULL DEFAULT 0,
            next_review TEXT NOT NULL,
            last_review TEXT,
            difficulty_level INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, verse_ref)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            verse_ref TEXT NOT NULL,
            quality INTEGER NOT NULL,
            difficulty_level INTEGER NOT NULL,
            reviewed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def add_verse_card(user_id: int, verse_ref: str, theme: str | None = None) -> bool:
    """Add a verse card. Returns True if newly added, False if already exists."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO verse_cards (user_id, verse_ref, theme, next_review) VALUES (?, ?, ?, ?)",
            (user_id, verse_ref, theme, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def add_bulk_cards(user_id: int, verse_refs: list[str], theme: str | None = None) -> int:
    """Add multiple verse cards. Returns count of newly added."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for ref in verse_refs:
        try:
            conn.execute(
                "INSERT INTO verse_cards (user_id, verse_ref, theme, next_review) VALUES (?, ?, ?, ?)",
                (user_id, ref, theme, now),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return added


def get_due_cards(user_id: int, limit: int = 10) -> list[dict]:
    """Get cards due for review, interleaved across themes."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT * FROM verse_cards
           WHERE user_id = ? AND next_review <= ?
           ORDER BY next_review ASC
           LIMIT ?""",
        (user_id, now, limit * 3),  # fetch extra for interleaving
    ).fetchall()
    conn.close()

    cards = [dict(r) for r in rows]
    if not cards:
        return []

    # Interleave: group by theme, round-robin
    from collections import defaultdict
    by_theme = defaultdict(list)
    for c in cards:
        by_theme[c["theme"] or "none"].append(c)

    result = []
    theme_keys = list(by_theme.keys())
    idx = 0
    while len(result) < limit:
        added_this_round = False
        for key in theme_keys:
            if by_theme[key] and len(result) < limit:
                result.append(by_theme[key].pop(0))
                added_this_round = True
        if not added_this_round:
            break

    return result


def update_card_sm2(card_id: int, ease_factor: float, interval_days: float,
                    repetitions: int, next_review: str, difficulty_level: int, streak: int):
    conn = get_connection()
    conn.execute(
        """UPDATE verse_cards
           SET ease_factor=?, interval_days=?, repetitions=?, next_review=?,
               last_review=?, difficulty_level=?, streak=?
           WHERE id=?""",
        (ease_factor, interval_days, repetitions, next_review,
         datetime.now(timezone.utc).isoformat(), difficulty_level, streak, card_id),
    )
    conn.commit()
    conn.close()


def log_review(user_id: int, verse_ref: str, quality: int, difficulty_level: int):
    conn = get_connection()
    conn.execute(
        "INSERT INTO review_log (user_id, verse_ref, quality, difficulty_level) VALUES (?, ?, ?, ?)",
        (user_id, verse_ref, quality, difficulty_level),
    )
    conn.commit()
    conn.close()


def get_user_progress(user_id: int) -> dict:
    """Get aggregate memorization stats."""
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    mastered = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=? AND difficulty_level>=3 AND streak>=3",
        (user_id,),
    ).fetchone()[0]
    due = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=? AND next_review<=?",
        (user_id, datetime.now(timezone.utc).isoformat()),
    ).fetchone()[0]
    conn.close()
    return {"total": total, "mastered": mastered, "due": due}


def get_theme_progress(user_id: int, theme: str) -> dict:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=? AND theme=?", (user_id, theme)
    ).fetchone()[0]
    mastered = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=? AND theme=? AND difficulty_level>=3 AND streak>=3",
        (user_id, theme),
    ).fetchone()[0]
    learning = conn.execute(
        "SELECT COUNT(*) FROM verse_cards WHERE user_id=? AND theme=? AND repetitions>0",
        (user_id, theme),
    ).fetchone()[0]
    conn.close()
    return {"total": total, "mastered": mastered, "learning": learning}


def get_card_by_id(card_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM verse_cards WHERE id=?", (card_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_user_cards(user_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM verse_cards WHERE user_id=? ORDER BY theme, verse_ref", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
