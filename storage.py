import sqlite3
from datetime import datetime, timezone

DB_PATH = "decisions.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                channel      TEXT NOT NULL,
                text_excerpt TEXT NOT NULL,
                llm_decision TEXT NOT NULL,
                llm_reason   TEXT NOT NULL
            )
        """)


def log_decision(channel: str, text: str, decision: bool, reason: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, channel, text_excerpt, llm_decision, llm_reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                channel,
                text[:300],
                "YES" if decision else "NO",
                reason,
            ),
        )
