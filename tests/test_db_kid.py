import threading
import time

import config
import db


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "t.db"))


def test_messages_roundtrip_oldest_first(tmp_path):
    _fresh(tmp_path)
    db.add_message(1, "user", "hi")
    db.add_message(1, "kid", "yo")
    rows = db.recent_messages(1)
    assert [r["role"] for r in rows] == ["user", "kid"]
    assert rows[0]["text"] == "hi"


def test_recent_messages_returns_newest_window_in_order(tmp_path):
    _fresh(tmp_path)
    for i in range(30):
        db.add_message(1, "user", f"m{i}")
    rows = db.recent_messages(1, limit=5)
    assert [r["text"] for r in rows] == ["m25", "m26", "m27", "m28", "m29"]


def test_prune_messages_keeps_newest(tmp_path):
    _fresh(tmp_path)
    for i in range(120):
        db.add_message(1, "user", f"m{i}")
    removed = db.prune_messages(1, keep=100)
    assert removed == 20
    rows = db.recent_messages(1, limit=200)
    assert len(rows) == 100
    assert rows[0]["text"] == "m20"


def test_chat_state_created_with_defaults(tmp_path):
    _fresh(tmp_path)
    s = db.get_chat_state(42)
    assert s["chat_id"] == 42
    assert s["bond"] == 0
    assert s["ping_stage"] == 0
    assert s["gave_up"] == 0
    assert s["muted"] == 0
    assert s["chattiness"] == "normal"
    assert s["next_action_at"] is None


def test_update_chat_state_persists(tmp_path):
    _fresh(tmp_path)
    db.get_chat_state(42)
    db.update_chat_state(42, bond=7, ping_stage=2, next_action_at=123.0)
    s = db.get_chat_state(42)
    assert (s["bond"], s["ping_stage"], s["next_action_at"]) == (7, 2, 123.0)


def test_update_chat_state_rejects_unknown_column(tmp_path):
    _fresh(tmp_path)
    db.get_chat_state(42)
    try:
        db.update_chat_state(42, drop_table=1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown column")


def test_due_chats_excludes_muted_gaveup_and_future(tmp_path):
    _fresh(tmp_path)
    for cid in (1, 2, 3, 4):
        db.get_chat_state(cid)
    db.update_chat_state(1, next_action_at=50.0)                 # due
    db.update_chat_state(2, next_action_at=50.0, muted=1)        # muted
    db.update_chat_state(3, next_action_at=50.0, gave_up=1)      # gave up
    db.update_chat_state(4, next_action_at=500.0)                # not yet
    assert [c["chat_id"] for c in db.due_chats(100.0)] == [1]


def test_kid_state_kv(tmp_path):
    _fresh(tmp_path)
    assert db.get_kid_state("day_state", "none") == "none"
    db.set_kid_state("day_state", "grounded")
    db.set_kid_state("day_state", "sick")     # upsert
    assert db.get_kid_state("day_state") == "sick"


def test_trend_blurb_stored_and_returned(tmp_path):
    _fresh(tmp_path)
    assert db.add_trend("skibidi toilet", blurb="a toilet with a head", kind="meme")
    memes = db.trend_memes_for_generation()
    assert memes[0]["term"] == "skibidi toilet"
    assert memes[0]["blurb"] == "a toilet with a head"


def test_trend_memes_excludes_blurbless_and_banned(tmp_path):
    _fresh(tmp_path)
    db.add_trend("bare term")                                     # no blurb -> not a meme
    db.add_trend("banned meme", blurb="x", kind="meme")
    db.ban_trend("banned meme")
    assert db.trend_memes_for_generation() == []


# --- lock discipline --------------------------------------------------------

class _CountingLock:
    """Wraps the real lock and counts acquisitions, so a test can assert how
    many times a single call re-enters it."""

    def __init__(self, inner):
        self._inner = inner
        self.acquires = 0

    def __enter__(self):
        self.acquires += 1
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def test_lazy_init_after_close_does_not_deadlock(tmp_path, monkeypatch):
    """Every accessor calls _db() *inside* `with _lock`, and _db() calls
    init_db() when _conn is None -- which takes _lock again. On a plain
    threading.Lock that is a permanent hang, not a crash.

    The reachable window in production is anything touching the DB after
    on_shutdown's db.close(). Under a supervisor with restartPolicy "always" a
    hung process is not a dead one, so it is never restarted: silent, permanent,
    green dashboard.

    Run on a worker thread with a join timeout, because on the buggy code the
    call never returns. The lock is swapped for a fresh one before asserting so
    a failure here cannot wedge the rest of the suite behind the stuck thread.
    """
    _fresh(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lazy.db"))
    db.close()                       # _conn is None; the next call must re-init
    result = {}

    def worker():
        result["state"] = db.get_chat_state(1)   # hangs on a non-reentrant lock

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=5)
    hung = t.is_alive()
    if hung:
        # The stuck thread owns the old lock forever; hand the module a fresh
        # one so the remaining tests aren't blocked behind it.
        db._lock = threading.RLock()
    assert not hung, "DEADLOCKED: _db() re-entered _lock via init_db()"
    assert result["state"]["chat_id"] == 1


def test_update_chat_state_takes_the_lock_once(tmp_path, monkeypatch):
    """It used to take _lock three times -- two get_chat_state calls bracketing
    the UPDATE -- so the read-modify-write was not atomic against a concurrent
    writer, and it paid three round trips for one logical write."""
    _fresh(tmp_path)
    db.get_chat_state(1)             # create the row outside the measurement
    counter = _CountingLock(db._lock)
    monkeypatch.setattr(db, "_lock", counter)
    db.update_chat_state(1, bond=7)

    assert counter.acquires == 1
    assert db.get_chat_state(1)["bond"] == 7


def test_all_chat_ids_reaches_chats_due_chats_never_returns(tmp_path):
    """The prune job borrowed due_chats, whose filters were written for a
    different purpose: next_action_at IS NOT NULL AND muted=0 AND gave_up=0.

    So pruning never reached any group chat (groups reply synchronously and
    never set next_action_at, yet on_group_message writes a messages row for
    EVERY message in the group), nor any given-up, muted, or idle chat. Those
    are precisely the chats whose message tables grow without bound.
    """
    _fresh(tmp_path)
    db.update_chat_state(-100, next_action_at=None)          # a group: never scheduled
    db.update_chat_state(2, gave_up=1)
    db.update_chat_state(3, muted=1)
    db.update_chat_state(4, next_action_at=time.time() + 10**6)   # idle, far future
    db.update_chat_state(5, next_action_at=time.time() - 10)      # the only due one

    assert set(db.all_chat_ids()) == {-100, 2, 3, 4, 5}
    # What the old query saw, for contrast:
    assert {r["chat_id"] for r in db.due_chats(time.time())} == {5}


def test_all_chat_ids_includes_chats_that_only_have_messages(tmp_path):
    _fresh(tmp_path)
    db.add_message(-200, "user", "group chatter with no chat_state row yet")
    assert -200 in db.all_chat_ids()


def test_init_db_drops_a_legacy_generations_table(tmp_path):
    """v3 has no persona/intensity/tone to attribute, so nothing writes this
    table. Upgraded installs shouldn't carry it around, and leaving it would
    let a future /stats read it and report stale v2 numbers as if they were
    current."""
    import sqlite3
    path = str(tmp_path / "legacy.db")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE generations (id INTEGER PRIMARY KEY, persona TEXT)")
    conn.execute("INSERT INTO generations (persona) VALUES ('gym_sigma')")
    conn.commit()
    conn.close()

    db.init_db(path)
    names = {r[0] for r in db._db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "generations" not in names
    assert "chat_state" in names          # the migration still built the real schema
