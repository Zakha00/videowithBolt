import os
from datetime import datetime

try:
    import libsql_experimental as libsql  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError:
    import libsql  # pyright: ignore[reportMissingImports]

TURSO_URL = os.getenv("TURSO_DB_URL", "")
TURSO_TOKEN = os.getenv("TURSO_DB_TOKEN", "")

FREE_LIMIT = 3       # Бесплатных скачиваний до первой подписки
BATCH_SIZE = 5       # Скачиваний за каждую подписку
REFERRAL_BONUS = 3   # Бонусных скачиваний за каждого приглашённого


def _conn():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)


def init_db():
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id           INTEGER PRIMARY KEY,
            username          TEXT DEFAULT '',
            first_name        TEXT DEFAULT '',
            downloads         INTEGER DEFAULT 0,
            subscription_grants INTEGER DEFAULT 0,
            referral_bonus    INTEGER DEFAULT 0,
            referrer_id       INTEGER DEFAULT NULL,
            joined_at         TEXT DEFAULT (datetime('now')),
            last_active       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS downloads_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            url        TEXT,
            title      TEXT DEFAULT '',
            fmt        TEXT DEFAULT 'video',
            status     TEXT DEFAULT 'ok',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ad_channels (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            url    TEXT NOT NULL,
            name   TEXT NOT NULL,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stats (
            date              TEXT PRIMARY KEY,
            total_downloads   INTEGER DEFAULT 0,
            new_users         INTEGER DEFAULT 0
        );
    """)
    c.commit()


# ─── Users ───────────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str, first_name: str):
    c = _conn()
    c.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_active = datetime('now')
    """, (user_id, username or '', first_name or ''))
    c.commit()


def get_user(user_id: int):
    c = _conn()
    return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_downloads(user_id: int) -> int:
    row = get_user(user_id)
    return row[2] if row else 0   # downloads column index


def _get_user_fields(user_id: int):
    """Returns (downloads, subscription_grants, referral_bonus) or (0,0,0)."""
    c = _conn()
    row = c.execute(
        "SELECT downloads, subscription_grants, referral_bonus FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return row if row else (0, 0, 0)


def downloads_allowed(user_id: int) -> int:
    dl, grants, bonus = _get_user_fields(user_id)
    return FREE_LIMIT + grants * BATCH_SIZE + bonus


def needs_subscription(user_id: int) -> bool:
    dl, grants, bonus = _get_user_fields(user_id)
    allowed = FREE_LIMIT + grants * BATCH_SIZE + bonus
    return dl >= allowed


def remaining_downloads(user_id: int) -> int:
    dl, grants, bonus = _get_user_fields(user_id)
    allowed = FREE_LIMIT + grants * BATCH_SIZE + bonus
    return max(0, allowed - dl)


def grant_subscription(user_id: int):
    """Даём пользователю +BATCH_SIZE скачиваний после подписки."""
    c = _conn()
    c.execute(
        "UPDATE users SET subscription_grants = subscription_grants + 1 WHERE user_id = ?",
        (user_id,)
    )
    c.commit()


def increment_downloads(user_id: int):
    c = _conn()
    c.execute(
        "UPDATE users SET downloads = downloads + 1, last_active = datetime('now') WHERE user_id = ?",
        (user_id,)
    )
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("""
        INSERT INTO stats (date, total_downloads) VALUES (?, 1)
        ON CONFLICT(date) DO UPDATE SET total_downloads = total_downloads + 1
    """, (today,))
    c.commit()


# ─── Referrals ────────────────────────────────────────────────────────────────

def register_referral(referrer_id: int, referred_id: int) -> bool:
    """Регистрирует реферала. Возвращает True если новый."""
    c = _conn()
    existing = c.execute(
        "SELECT id FROM referrals WHERE referred_id = ?", (referred_id,)
    ).fetchone()
    if existing:
        return False
    c.execute(
        "INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
        (referrer_id, referred_id)
    )
    c.execute(
        "UPDATE users SET referral_bonus = referral_bonus + ?, referrer_id = ? WHERE user_id = ?",
        (REFERRAL_BONUS, referrer_id, referrer_id)
    )
    c.execute(
        "UPDATE users SET referrer_id = ? WHERE user_id = ?",
        (referrer_id, referred_id)
    )
    c.commit()
    return True


def get_referral_count(user_id: int) -> int:
    c = _conn()
    row = c.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else 0


# ─── History ──────────────────────────────────────────────────────────────────

def log_download(user_id: int, url: str, title: str, fmt: str, status: str):
    c = _conn()
    c.execute(
        "INSERT INTO downloads_log (user_id, url, title, fmt, status) VALUES (?, ?, ?, ?, ?)",
        (user_id, url, title[:120] if title else url[:120], fmt, status)
    )
    c.commit()


def get_history(user_id: int, limit: int = 8):
    c = _conn()
    return c.execute("""
        SELECT title, fmt, created_at FROM downloads_log
        WHERE user_id = ? AND status = 'ok'
        ORDER BY created_at DESC LIMIT ?
    """, (user_id, limit)).fetchall()


# ─── Ad Channels (rotating) ───────────────────────────────────────────────────

_ad_idx = 0


def get_ad_channels():
    c = _conn()
    return c.execute("SELECT id, url, name FROM ad_channels WHERE active = 1").fetchall()


def get_next_ad_channel():
    global _ad_idx
    channels = get_ad_channels()
    if not channels:
        return None, None
    ch = channels[_ad_idx % len(channels)]
    _ad_idx += 1
    return ch[1], ch[2]  # url, name


def add_ad_channel(url: str, name: str):
    c = _conn()
    c.execute("INSERT INTO ad_channels (url, name) VALUES (?, ?)", (url, name))
    c.commit()


def remove_ad_channel(channel_id: int):
    c = _conn()
    c.execute("DELETE FROM ad_channels WHERE id = ?", (channel_id,))
    c.commit()


# ─── Stats / Admin ────────────────────────────────────────────────────────────

def get_total_users() -> int:
    c = _conn()
    row = c.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


def get_today_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    c = _conn()
    return c.execute("SELECT * FROM stats WHERE date = ?", (today,)).fetchone()


def get_all_user_ids():
    c = _conn()
    rows = c.execute("SELECT user_id FROM users").fetchall()
    return [r[0] for r in rows]


def get_total_downloads_all() -> int:
    c = _conn()
    row = c.execute("SELECT SUM(total_downloads) FROM stats").fetchone()
    return row[0] or 0
