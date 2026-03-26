import os
import libsql_experimental as libsql
from datetime import datetime

TURSO_URL = os.getenv("TURSO_DB_URL", "")
TURSO_TOKEN = os.getenv("TURSO_DB_TOKEN", "")


def get_conn():
    """Создаёт соединение с Turso."""
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)


async def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            downloads   INTEGER DEFAULT 0,
            is_subscribed INTEGER DEFAULT 0,
            joined_at   TEXT DEFAULT (datetime('now')),
            last_active TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS downloads_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            url         TEXT,
            status      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS stats (
            date        TEXT PRIMARY KEY,
            total_downloads INTEGER DEFAULT 0,
            new_users   INTEGER DEFAULT 0
        );
    """)
    conn.commit()


def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row


def upsert_user(user_id: int, username: str, first_name: str):
    conn = get_conn()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_active = datetime('now')
    """, (user_id, username, first_name))
    conn.commit()


def increment_downloads(user_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET downloads = downloads + 1, last_active = datetime('now') WHERE user_id = ?",
        (user_id,)
    )
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO stats (date, total_downloads) VALUES (?, 1)
        ON CONFLICT(date) DO UPDATE SET total_downloads = total_downloads + 1
    """, (today,))
    conn.commit()


def get_download_count(user_id: int) -> int:
    conn = get_conn()
    row = conn.execute("SELECT downloads FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def set_subscribed(user_id: int, status: bool):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET is_subscribed = ? WHERE user_id = ?",
        (1 if status else 0, user_id)
    )
    conn.commit()


def log_download(user_id: int, url: str, status: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO downloads_log (user_id, url, status) VALUES (?, ?, ?)",
        (user_id, url, status)
    )
    conn.commit()


def get_total_users() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


def get_today_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    return conn.execute("SELECT * FROM stats WHERE date = ?", (today,)).fetchone()


def get_all_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    return [r[0] for r in rows]
