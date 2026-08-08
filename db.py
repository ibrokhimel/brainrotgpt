"""SQLite persistence: per-chat state, analytics, trends, and chat memory.

Everything used to live in process memory and reset on restart. This module
keeps the durable bits — chat_state (mood/bond/notes/chattiness/scheduling),
the message log, trends, and generation analytics — in a small SQLite file.

Calls are synchronous but tiny (local file); a lock keeps them safe across the
event loop and the job-queue threads.
"""
import sqlite3
import string
import threading
import time

import config

# Reentrant on purpose: every accessor calls _db() *inside* `with _lock`, and
# _db() falls back to init_db() when the connection is closed, which takes the
# lock again. On a plain Lock that lazy-init path can only ever hang -- and a
# hung process under restartPolicy "always" is never restarted, because the
# supervisor still sees it running.
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init_db(path: str | None = None) -> None:
    """Open the database and create tables. Safe to call once at startup."""
    global _conn
    db_path = path or config.DB_PATH
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
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
            CREATE TABLE IF NOT EXISTS facts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER NOT NULL,
                fact      TEXT    NOT NULL,
                created   REAL    NOT NULL,
                last_seen REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_facts_chat_seen ON facts(chat_id, last_seen);
            """
        )
        # Migrate older DBs that predate meme blurbs on trends.
        tcols = {r[1] for r in _conn.execute("PRAGMA table_info(trends)").fetchall()}
        if "blurb" not in tcols:
            _conn.execute("ALTER TABLE trends ADD COLUMN blurb TEXT NOT NULL DEFAULT ''")
        if "kind" not in tcols:
            _conn.execute("ALTER TABLE trends ADD COLUMN kind TEXT NOT NULL DEFAULT 'term'")
        # One-time cleanup: drop tables from the old generator surface so
        # upgraded installs don't carry dead tables. `generations` joins them:
        # v3 has no persona/intensity/tone to attribute, so nothing writes it.
        for dead in ("favorites", "subscriptions", "last_results", "settings",
                     "generations"):
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


def recent_messages(chat_id: int, limit: int = 20, role: str | None = None) -> list[dict]:
    """The newest `limit` messages, returned oldest-first for prompt assembly.

    `role` filters inside the query rather than after the rows come back, which
    matters: the kid sends two or three messages per turn against the user's
    one, so a mixed window of N is mostly the kid's own output and filtering it
    afterwards leaves a handful of lines.
    """
    q = "SELECT role, text, ts FROM messages WHERE chat_id=?"
    args: list = [chat_id]
    if role is not None:
        q += " AND role=?"
        args.append(role)
    with _lock:
        rows = _db().execute(f"{q} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
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


# --- Facts (the accumulating half of memory) ------------------------------

FACTS_MAX = 40            # per chat; older facts fall off the bottom

# Curly quotes are not in string.punctuation, and phone keyboards produce them.
_PUNCT = str.maketrans("", "", string.punctuation + "‘’“”")

# A leading pronoun is voice, not content: live, every fact was stored twice,
# "I work in IT" beside "They work in IT." Only the FIRST word goes.
_VOICE_PREFIXES = frozenset({"i", "im", "my", "mine", "they", "theyre", "their", "theirs"})


def _normalise_fact(fact: str) -> str:
    """Fold a fact to its comparison key: lowercase, no punctuation, one space,
    no leading first/third-person pronoun.

    Exact match after folding, deliberately not fuzzy. "Their name is WALTER!",
    "their name is walter" and "My name is Walter" are one fact; "their job
    drains them" and "their boss drains them" are two, and a similarity
    threshold that merged them would quietly lose the second one.
    """
    words = fact.lower().translate(_PUNCT).split()
    if words and words[0] in _VOICE_PREFIXES:
        words = words[1:]
    return " ".join(words)


def add_fact(chat_id: int, fact: str) -> bool:
    """Record one atomic fact about a chat. True only when a new row landed.

    Every distillation re-reads an overlapping window, so the same fact comes
    back over and over; a repeat bumps `last_seen` on the existing row instead
    of inserting, which both keeps the prompt clean and lets recency ordering
    float what they keep bringing up back to the top.
    """
    fact = " ".join((fact or "").split())
    norm = _normalise_fact(fact)
    if not norm:
        return False
    now = time.time()
    with _lock:
        conn = _db()
        # At most FACTS_MAX rows per chat, so folding in Python is cheaper than
        # carrying a denormalised key column and keeping it in sync.
        for row in conn.execute("SELECT id, fact FROM facts WHERE chat_id=?",
                                (chat_id,)).fetchall():
            if _normalise_fact(row["fact"]) == norm:
                conn.execute("UPDATE facts SET last_seen=? WHERE id=?", (now, row["id"]))
                conn.commit()
                return False
        conn.execute(
            "INSERT INTO facts (chat_id, fact, created, last_seen) VALUES (?,?,?,?)",
            (chat_id, fact, now, now),
        )
        conn.commit()
    prune_facts(chat_id, keep=FACTS_MAX)
    return True


def recent_facts(chat_id: int, limit: int = FACTS_MAX) -> list[dict]:
    """What the kid knows about this chat, most recently confirmed first."""
    with _lock:
        rows = _db().execute(
            "SELECT id, chat_id, fact, created, last_seen FROM facts WHERE chat_id=? "
            "ORDER BY last_seen DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_facts(chat_id: int) -> int:
    """Drop everything the kid thinks it knows about one chat. Returns the count.

    Needed because the first version of the extractor read the whole rendered
    transcript, which includes the kid's own lines, so it summarised the bot's
    output and filed it under the person it was texting.
    """
    with _lock:
        cur = _db().execute("DELETE FROM facts WHERE chat_id=?", (chat_id,))
        _db().commit()
        return cur.rowcount


def prune_facts(chat_id: int, keep: int = FACTS_MAX) -> int:
    """Drop the least recently seen facts for one chat. Other chats untouched."""
    with _lock:
        cur = _db().execute(
            "DELETE FROM facts WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM facts WHERE chat_id=? ORDER BY last_seen DESC, id DESC LIMIT ?)",
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
    assigns = ", ".join(f"{k}=?" for k in fields)
    # One lock for the whole read-modify-write. It used to take three (two
    # get_chat_state calls bracketing the UPDATE), which let a concurrent
    # writer interleave between the row-create and the read-back.
    with _lock:
        conn = _db()
        conn.execute("INSERT OR IGNORE INTO chat_state (chat_id) VALUES (?)", (chat_id,))
        conn.execute(
            f"UPDATE chat_state SET {assigns} WHERE chat_id=?",
            (*fields.values(), chat_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row)


def all_chat_ids() -> list[int]:
    """Every chat the bot knows about, for maintenance jobs like pruning.

    Deliberately not `due_chats`: that query's filters (next_action_at IS NOT
    NULL, muted=0, gave_up=0) were written to decide what to *act* on, and
    borrowing them for pruning skipped every group, muted, given-up and idle
    chat — which are exactly the ones whose message tables grow unbounded.
    Unions chat_state with messages so a group that has never had a state row
    written is still reached.
    """
    with _lock:
        rows = _db().execute(
            "SELECT chat_id FROM chat_state UNION SELECT DISTINCT chat_id FROM messages"
        ).fetchall()
    return [r["chat_id"] for r in rows]


def claim_due_action(chat_id: int, now: float,
                     kinds: tuple[str, ...] | None = None) -> dict | None:
    """Atomically take ownership of a chat's due action. Returns the row it was
    claimed from, or None if there was nothing to claim.

    The 60s tick and the in-process fast path (scheduler.arm_fast_reply) race
    for the same row, so clearing `next_action_at` has to be the same
    indivisible step as deciding to act on it: whichever caller gets here first
    wins and the other is handed None. The whole read-modify-write is under
    `_lock`, and there is no await inside it, so no second claimer can
    interleave.

    Restricting the claim to actions that are *already due* is what makes a
    stale fast-path task harmless — a newer message that re-armed the row for
    later leaves next_action_at in the future, so the old task claims nothing
    instead of firing the pending reply early and clearing it.
    """
    where = ("chat_id=? AND next_action_at IS NOT NULL AND next_action_at <= ? "
             "AND muted=0 AND gave_up=0")
    args: list = [chat_id, now]
    if kinds is not None:
        # A NULL kind means "reply" everywhere else, so it has to here too.
        where += f" AND COALESCE(next_action_kind, 'reply') IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    with _lock:
        conn = _db()
        row = conn.execute(f"SELECT * FROM chat_state WHERE {where}", args).fetchone()
        if row is None:
            return None
        conn.execute(
            f"UPDATE chat_state SET next_action_at=NULL, next_action_kind=NULL WHERE {where}",
            args,
        )
        conn.commit()
    return dict(row)


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

def get_kid_state(key: str, default: str | None = "") -> str | None:
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


def coldopen_candidates(now: float, *, min_bond: int, active_within_s: float,
                        quiet_for_s: float) -> list[dict]:
    """Chats the kid could plausibly text first.

    Evaluated in SQL rather than by waking a job per chat, so the tick cost stays
    flat as the number of chats grows.
    """
    with _lock:
        rows = _db().execute(
            "SELECT * FROM chat_state WHERE muted=0 AND gave_up=0 "
            "AND next_action_at IS NULL AND bond >= ? "
            "AND last_user_ts IS NOT NULL AND last_user_ts >= ? "
            "AND (last_kid_ts IS NULL OR last_kid_ts <= ?)",
            (min_bond, now - active_within_s, now - quiet_for_s),
        ).fetchall()
    return [dict(r) for r in rows]
