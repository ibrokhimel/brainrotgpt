"""SQLite persistence: per-chat settings, favorites, analytics, subscriptions.

Everything used to live in process memory and reset on restart. This module
keeps the durable bits in a small SQLite file. Buffers being built mid-input
stay in memory by design (they're transient staging) — see bot.sessions.

Calls are synchronous but tiny (local file); a lock keeps them safe across the
event loop and the job-queue threads.
"""
import sqlite3
import threading
import time

import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Allowed setting values (validated on write so the prompt layer can trust them).
INTENSITIES = ("mild", "medium", "unhinged")
TONES = ("default", "roast", "cope", "hype", "deny", "gaslight")


def _defaults() -> dict:
    return {
        "persona": config.DEFAULT_PERSONA,
        "intensity": config.DEFAULT_INTENSITY,
        "tone": config.DEFAULT_TONE,
        "language": config.DEFAULT_LANGUAGE,
        "candidates": config.DEFAULT_CANDIDATES,
    }


def init_db(path: str | None = None) -> None:
    """Open the database and create tables. Safe to call once at startup."""
    global _conn
    db_path = path or config.DB_PATH
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id    INTEGER PRIMARY KEY,
                persona    TEXT,
                intensity  TEXT,
                tone       TEXT,
                language   TEXT,
                candidates INTEGER,
                updated    REAL
            );
            CREATE TABLE IF NOT EXISTS favorites (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                text    TEXT,
                persona TEXT,
                created REAL
            );
            CREATE INDEX IF NOT EXISTS idx_fav_chat ON favorites(chat_id, created);
            CREATE TABLE IF NOT EXISTS generations (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER,
                user_id  INTEGER,
                persona  TEXT,
                intensity TEXT,
                tone     TEXT,
                is_regen INTEGER,
                tokens   INTEGER,
                created  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_gen_created ON generations(created);
            CREATE INDEX IF NOT EXISTS idx_gen_persona ON generations(persona, created);
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER PRIMARY KEY,
                hour    INTEGER,
                enabled INTEGER,
                created REAL
            );
            CREATE TABLE IF NOT EXISTS last_results (
                chat_id INTEGER PRIMARY KEY,
                text    TEXT,
                persona TEXT,
                updated REAL
            );
            """
        )
        _conn.commit()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.commit()
            _conn.close()
            _conn = None


def _db() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# --- Settings -------------------------------------------------------------

def get_settings(chat_id: int) -> dict:
    """Return the chat's settings, falling back to configured defaults."""
    out = _defaults()
    with _lock:
        row = _db().execute(
            "SELECT * FROM settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row:
        for k in ("persona", "intensity", "tone", "language", "candidates"):
            if row[k] is not None:
                out[k] = row[k]
    return out


def set_setting(chat_id: int, key: str, value) -> dict:
    """Validate + persist one setting, returning the updated settings dict."""
    if key == "intensity" and value not in INTENSITIES:
        raise ValueError(f"bad intensity: {value}")
    if key == "tone" and value not in TONES:
        raise ValueError(f"bad tone: {value}")
    if key == "candidates":
        value = max(1, min(int(value), config.MAX_CANDIDATES))
    current = get_settings(chat_id)
    current[key] = value
    with _lock:
        _db().execute(
            """INSERT INTO settings (chat_id, persona, intensity, tone, language, candidates, updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 persona=excluded.persona, intensity=excluded.intensity,
                 tone=excluded.tone, language=excluded.language,
                 candidates=excluded.candidates, updated=excluded.updated""",
            (
                chat_id, current["persona"], current["intensity"], current["tone"],
                current["language"], current["candidates"], time.time(),
            ),
        )
        _db().commit()
    return current


# --- Favorites ------------------------------------------------------------

def add_favorite(chat_id: int, user_id: int, text: str, persona: str) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO favorites (chat_id, user_id, text, persona, created) VALUES (?,?,?,?,?)",
            (chat_id, user_id, text, persona, time.time()),
        )
        _db().commit()
        return cur.lastrowid


def list_favorites(chat_id: int, limit: int = 10) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id, text, persona, created FROM favorites WHERE chat_id=? ORDER BY created DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_favorite(fav_id: int, chat_id: int) -> bool:
    with _lock:
        cur = _db().execute(
            "DELETE FROM favorites WHERE id=? AND chat_id=?", (fav_id, chat_id)
        )
        _db().commit()
        return cur.rowcount > 0


# --- Analytics (generations) ---------------------------------------------

def log_generation(
    chat_id: int, user_id: int, persona: str, intensity: str,
    tone: str, is_regen: bool, tokens: int,
) -> None:
    with _lock:
        _db().execute(
            """INSERT INTO generations
               (chat_id, user_id, persona, intensity, tone, is_regen, tokens, created)
               VALUES (?,?,?,?,?,?,?,?)""",
            (chat_id, user_id, persona, intensity, tone, int(is_regen), tokens, time.time()),
        )
        _db().commit()


def recent_generation_times(window_s: float = 60.0) -> list[tuple[int, float]]:
    """(user_id, epoch) for generations in the last window — seeds the limiter."""
    cutoff = time.time() - window_s
    with _lock:
        rows = _db().execute(
            "SELECT user_id, created FROM generations WHERE created>=? ORDER BY created",
            (cutoff,),
        ).fetchall()
    return [(r["user_id"], r["created"]) for r in rows]


def leaderboard(days: int = 7, limit: int = 10) -> list[dict]:
    cutoff = time.time() - days * 86400
    with _lock:
        rows = _db().execute(
            """SELECT persona, COUNT(*) AS n FROM generations
               WHERE created>=? GROUP BY persona ORDER BY n DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _lock:
        db = _db()
        total = db.execute("SELECT COUNT(*) AS n FROM generations").fetchone()["n"]
        users = db.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM generations"
        ).fetchone()["n"]
        day = db.execute(
            "SELECT COUNT(*) AS n FROM generations WHERE created>=?",
            (time.time() - 86400,),
        ).fetchone()["n"]
        regens = db.execute(
            "SELECT COUNT(*) AS n FROM generations WHERE is_regen=1"
        ).fetchone()["n"]
        favs = db.execute("SELECT COUNT(*) AS n FROM favorites").fetchone()["n"]
    return {"total": total, "users": users, "last_24h": day, "regens": regens, "favorites": favs}


# --- Last result (for /last + regenerate persistence) ---------------------

def set_last_result(chat_id: int, text: str, persona: str) -> None:
    with _lock:
        _db().execute(
            """INSERT INTO last_results (chat_id, text, persona, updated) VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 text=excluded.text, persona=excluded.persona, updated=excluded.updated""",
            (chat_id, text, persona, time.time()),
        )
        _db().commit()


def get_last_result(chat_id: int) -> dict | None:
    with _lock:
        row = _db().execute(
            "SELECT text, persona FROM last_results WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return dict(row) if row else None


# --- Daily subscriptions --------------------------------------------------

def set_subscription(chat_id: int, hour: int, enabled: bool = True) -> None:
    with _lock:
        _db().execute(
            """INSERT INTO subscriptions (chat_id, hour, enabled, created) VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 hour=excluded.hour, enabled=excluded.enabled""",
            (chat_id, hour, int(enabled), time.time()),
        )
        _db().commit()


def remove_subscription(chat_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
        _db().commit()


def list_subscriptions() -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT chat_id, hour FROM subscriptions WHERE enabled=1"
        ).fetchall()
    return [dict(r) for r in rows]


def get_subscription(chat_id: int) -> dict | None:
    with _lock:
        row = _db().execute(
            "SELECT chat_id, hour, enabled FROM subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return dict(row) if row else None
