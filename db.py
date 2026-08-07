"""SQLite persistence: per-chat settings, analytics, trends, and chat memory.

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
            CREATE TABLE IF NOT EXISTS trends (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                term    TEXT UNIQUE COLLATE NOCASE,
                source  TEXT,                 -- 'manual' | 'auto'
                banned  INTEGER DEFAULT 0,    -- 1 = hidden + blocked from auto re-add
                created REAL
            );
            CREATE INDEX IF NOT EXISTS idx_trends_active ON trends(banned, created);
            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role    TEXT    NOT NULL,
                text    TEXT    NOT NULL,
                ts      REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id          INTEGER PRIMARY KEY,
                mood             TEXT    NOT NULL DEFAULT 'skibidi',
                mood_set_at      REAL,
                bond             INTEGER NOT NULL DEFAULT 0,
                notes            TEXT    NOT NULL DEFAULT '',
                msgs_since_notes INTEGER NOT NULL DEFAULT 0,
                ping_stage       INTEGER NOT NULL DEFAULT 0,
                next_action_at   REAL,
                next_action_kind TEXT,
                last_user_ts     REAL,
                last_kid_ts      REAL,
                pings_today      INTEGER NOT NULL DEFAULT 0,
                pings_day        TEXT,
                gave_up          INTEGER NOT NULL DEFAULT 0,
                salty            INTEGER NOT NULL DEFAULT 0,
                chattiness       TEXT    NOT NULL DEFAULT 'normal',
                muted            INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_chat_state_due ON chat_state(next_action_at);
            CREATE TABLE IF NOT EXISTS kid_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Migrate older DBs that predate the standalone `length` setting.
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(settings)").fetchall()}
        if "length" not in cols:
            _conn.execute("ALTER TABLE settings ADD COLUMN length TEXT")
        # Migrate older DBs that predate meme blurbs on trends.
        tcols = {r[1] for r in _conn.execute("PRAGMA table_info(trends)").fetchall()}
        if "blurb" not in tcols:
            _conn.execute("ALTER TABLE trends ADD COLUMN blurb TEXT NOT NULL DEFAULT ''")
        if "kind" not in tcols:
            _conn.execute("ALTER TABLE trends ADD COLUMN kind TEXT NOT NULL DEFAULT 'term'")
        # One-time cleanup: drop tables from the old generator surface so
        # upgraded installs don't carry dead tables.
        for dead in ("favorites", "subscriptions", "last_results"):
            _conn.execute(f"DROP TABLE IF EXISTS {dead}")
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
    return {"total": total, "users": users, "last_24h": day, "regens": regens}


# --- Trends (live brainrot vocab, manual + auto-fetched) ------------------

def add_trend(term: str, source: str = "manual", blurb: str = "", kind: str = "term") -> bool:
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
            "INSERT INTO trends (term, source, banned, created, blurb, kind) VALUES (?,?,0,?,?,?)",
            (term, source, time.time(), blurb, kind),
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


def trend_memes_for_generation(limit: int = 5) -> list[dict]:
    """Active memes that have an explanation — what the kid can actually talk about."""
    with _lock:
        rows = _db().execute(
            "SELECT term, blurb FROM trends WHERE banned=0 AND kind='meme' "
            "AND blurb != '' ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Messages (the kid's conversation memory) -----------------------------

def add_message(chat_id: int, role: str, text: str) -> None:
    if role not in ("user", "kid"):
        raise ValueError(f"bad role: {role}")
    with _lock:
        _db().execute(
            "INSERT INTO messages (chat_id, role, text, ts) VALUES (?,?,?,?)",
            (chat_id, role, text, time.time()),
        )
        _db().commit()


def recent_messages(chat_id: int, limit: int = 20) -> list[dict]:
    """The newest `limit` messages, returned oldest-first for prompt assembly."""
    with _lock:
        rows = _db().execute(
            "SELECT role, text, ts FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def prune_messages(chat_id: int, keep: int = 100) -> int:
    with _lock:
        cur = _db().execute(
            "DELETE FROM messages WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, keep),
        )
        _db().commit()
        return cur.rowcount


# --- Chat state (per-chat relationship + scheduling) ----------------------

CHATTINESS = ("chill", "normal", "clingy")

CHAT_STATE_FIELDS = (
    "mood", "mood_set_at", "bond", "notes", "msgs_since_notes", "ping_stage",
    "next_action_at", "next_action_kind", "last_user_ts", "last_kid_ts",
    "pings_today", "pings_day", "gave_up", "salty", "chattiness", "muted",
)


def get_chat_state(chat_id: int) -> dict:
    """Return the chat's state row, creating it with defaults on first access."""
    with _lock:
        _db().execute("INSERT OR IGNORE INTO chat_state (chat_id) VALUES (?)", (chat_id,))
        _db().commit()
        row = _db().execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row)


def update_chat_state(chat_id: int, **fields) -> dict:
    bad = set(fields) - set(CHAT_STATE_FIELDS)
    if bad:
        raise ValueError(f"unknown chat_state column(s): {sorted(bad)}")
    if not fields:
        return get_chat_state(chat_id)
    get_chat_state(chat_id)  # ensure the row exists
    assigns = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _db().execute(
            f"UPDATE chat_state SET {assigns} WHERE chat_id=?",
            (*fields.values(), chat_id),
        )
        _db().commit()
    return get_chat_state(chat_id)


def due_chats(now: float) -> list[dict]:
    """Chats with a scheduled action that is due. Muted and gave-up chats never fire."""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM chat_state WHERE next_action_at IS NOT NULL "
            "AND next_action_at <= ? AND muted=0 AND gave_up=0 ORDER BY next_action_at",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Kid state (global singletons: daily life, budget counters) -----------

def get_kid_state(key: str, default: str = "") -> str:
    with _lock:
        row = _db().execute("SELECT value FROM kid_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_kid_state(key: str, value: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO kid_state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        _db().commit()
