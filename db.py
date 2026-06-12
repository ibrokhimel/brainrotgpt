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
LENGTHS = ("short", "medium", "long", "max")
TONES = ("default", "roast", "cope", "hype", "deny", "gaslight")

# Columns the prompt layer reads back out of the settings row.
SETTING_KEYS = ("persona", "intensity", "length", "tone", "language", "candidates")


def _defaults() -> dict:
    return {
        "persona": config.DEFAULT_PERSONA,
        "intensity": config.DEFAULT_INTENSITY,
        "length": config.DEFAULT_LENGTH,
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
                length     TEXT,
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
            CREATE TABLE IF NOT EXISTS trends (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                term    TEXT UNIQUE COLLATE NOCASE,
                source  TEXT,                 -- 'manual' | 'auto'
                banned  INTEGER DEFAULT 0,    -- 1 = hidden + blocked from auto re-add
                created REAL
            );
            CREATE INDEX IF NOT EXISTS idx_trends_active ON trends(banned, created);
            """
        )
        # Migrate older DBs that predate the standalone `length` setting.
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(settings)").fetchall()}
        if "length" not in cols:
            _conn.execute("ALTER TABLE settings ADD COLUMN length TEXT")
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
        for k in SETTING_KEYS:
            if row[k] is not None:
                out[k] = row[k]
    return out


def set_setting(chat_id: int, key: str, value) -> dict:
    """Validate + persist one setting, returning the updated settings dict."""
    if key == "intensity" and value not in INTENSITIES:
        raise ValueError(f"bad intensity: {value}")
    if key == "length" and value not in LENGTHS:
        raise ValueError(f"bad length: {value}")
    if key == "tone" and value not in TONES:
        raise ValueError(f"bad tone: {value}")
    if key == "candidates":
        value = max(1, min(int(value), config.MAX_CANDIDATES))
    current = get_settings(chat_id)
    current[key] = value
    with _lock:
        _db().execute(
            """INSERT INTO settings
                 (chat_id, persona, intensity, length, tone, language, candidates, updated)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 persona=excluded.persona, intensity=excluded.intensity,
                 length=excluded.length, tone=excluded.tone, language=excluded.language,
                 candidates=excluded.candidates, updated=excluded.updated""",
            (
                chat_id, current["persona"], current["intensity"], current["length"],
                current["tone"], current["language"], current["candidates"], time.time(),
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


# --- Trends (live brainrot vocab, manual + auto-fetched) ------------------

def add_trend(term: str, source: str = "manual") -> bool:
    """Insert a trend term. A manual add un-bans a previously banned term;
    an auto add of a banned/existing term is skipped. Returns True if it landed."""
    term = (term or "").strip()
    if not term:
        return False
    with _lock:
        row = _db().execute("SELECT id, banned FROM trends WHERE term=?", (term,)).fetchone()
        if row is not None:
            if row["banned"] and source == "manual":
                _db().execute(
                    "UPDATE trends SET banned=0, source='manual' WHERE id=?", (row["id"],)
                )
                _db().commit()
                return True
            return False  # already present, or banned and only an auto add
        _db().execute(
            "INSERT INTO trends (term, source, banned, created) VALUES (?,?,0,?)",
            (term, source, time.time()),
        )
        _db().commit()
    return True


def ban_trend(term: str) -> bool:
    """Hide a term and block the fetcher from re-adding it (inserts a tombstone)."""
    term = (term or "").strip()
    if not term:
        return False
    with _lock:
        row = _db().execute("SELECT id FROM trends WHERE term=?", (term,)).fetchone()
        if row is not None:
            _db().execute("UPDATE trends SET banned=1 WHERE id=?", (row["id"],))
        else:
            _db().execute(
                "INSERT INTO trends (term, source, banned, created) VALUES (?, 'manual', 1, ?)",
                (term, time.time()),
            )
        _db().commit()
    return True


def remove_trend(term: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM trends WHERE term=?", ((term or "").strip(),))
        _db().commit()
    return cur.rowcount > 0


def list_trends(limit: int = 50, active_only: bool = True) -> list[dict]:
    q = "SELECT id, term, source, banned, created FROM trends"
    if active_only:
        q += " WHERE banned=0"
    q += " ORDER BY created DESC LIMIT ?"
    with _lock:
        rows = _db().execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_trends(source: str | None = None, active_only: bool = True) -> int:
    q = "SELECT COUNT(*) AS n FROM trends WHERE 1=1"
    args: list = []
    if active_only:
        q += " AND banned=0"
    if source:
        q += " AND source=?"
        args.append(source)
    with _lock:
        return _db().execute(q, args).fetchone()["n"]


def banned_trend_terms() -> set[str]:
    with _lock:
        rows = _db().execute("SELECT term FROM trends WHERE banned=1").fetchall()
    return {r["term"].lower() for r in rows}


def trend_terms_for_generation(limit: int = 20) -> list[str]:
    """Active trend terms to blend into a reply. NON-initializing: if the DB
    isn't open yet (e.g. unit tests calling the prompt builder), returns []."""
    if _conn is None:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "SELECT term FROM trends WHERE banned=0 ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["term"] for r in rows]
    except Exception:  # noqa: BLE001 — never let trend lookup break generation
        return []
