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
