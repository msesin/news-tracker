import hashlib
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_posts (
                hash      TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL
            )
        """)
        # Posts the classifier never actually got to judge. Retries buy a post
        # about ninety seconds; an outage lasting an hour outlives that and
        # every post arriving inside it would otherwise be logged NO unreviewed
        # and lost. Parking them here turns "you may be missing updates" into
        # "updates arrived late". Living in SQLite rather than memory is what
        # makes them survive the restart that clears every in-process flag.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                channel   TEXT NOT NULL,
                text      TEXT NOT NULL,
                attempts  INTEGER NOT NULL DEFAULT 0
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


def _hash(text: str) -> str:
    return hashlib.sha256(text[:150].encode()).hexdigest()


def is_duplicate(text: str) -> bool:
    h = _hash(text)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_posts WHERE hash = ? "
            "AND timestamp > datetime('now', '-24 hours')",
            (h,),
        ).fetchone()
    return row is not None


def mark_seen(text: str):
    h = _hash(text)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seen_posts (hash, timestamp) VALUES (?, datetime('now'))",
            (h,),
        )


# Give up on a parked post after this many drain attempts, so one permanently
# malformed post cannot be retried forever on every recovery.
MAX_PARK_ATTEMPTS = 5


def park_post(channel: str, text: str) -> None:
    """Hold a post the classifier could not judge, for a retry after recovery."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO pending_posts (timestamp, channel, text) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), channel, text),
        )


def pending_posts() -> list[tuple[int, str, str, int]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, channel, text, attempts FROM pending_posts ORDER BY id"
        ).fetchall()


def pending_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM pending_posts").fetchone()[0]


def unpark(post_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM pending_posts WHERE id = ?", (post_id,))


def note_park_attempt(post_id: int) -> int:
    """Count one drain attempt; returns the new total."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE pending_posts SET attempts = attempts + 1 WHERE id = ?", (post_id,)
        )
        row = conn.execute(
            "SELECT attempts FROM pending_posts WHERE id = ?", (post_id,)
        ).fetchone()
    return row[0] if row else MAX_PARK_ATTEMPTS
