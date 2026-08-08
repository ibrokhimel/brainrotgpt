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


# --- facts (the accumulating half of memory) -------------------------------

def test_facts_accumulate_and_come_back_newest_first(tmp_path):
    _fresh(tmp_path)
    assert db.add_fact(1, "their name is walter") is True
    assert db.add_fact(1, "hates their job") is True
    assert [f["fact"] for f in db.recent_facts(1)] == ["hates their job",
                                                       "their name is walter"]


def test_facts_are_scoped_to_their_chat(tmp_path):
    _fresh(tmp_path)
    db.add_fact(1, "their name is walter")
    db.add_fact(2, "their name is jesse")
    assert [f["fact"] for f in db.recent_facts(2)] == ["their name is jesse"]


def test_a_near_duplicate_fact_bumps_last_seen_instead_of_inserting(tmp_path):
    """Every distil re-reads the same window, so the same fact comes back over
    and over. Inserting each one floods the prompt with forty copies of the
    person's name and crowds out everything else they ever said."""
    _fresh(tmp_path)
    db.add_fact(1, "their name is walter")
    db.add_fact(1, "older fact")
    first = db.recent_facts(1)
    assert db.add_fact(1, "  Their name is WALTER!  ") is False   # not a new row
    rows = db.recent_facts(1)
    assert len(rows) == 2
    assert rows[0]["fact"] == "their name is walter"              # bumped to newest
    walter = next(r for r in rows if r["fact"] == "their name is walter")
    was = next(r for r in first if r["fact"] == "their name is walter")
    assert walter["last_seen"] > was["last_seen"]
    assert walter["created"] == was["created"]                    # same row, kept


def test_a_first_person_fact_collapses_into_its_third_person_twin(tmp_path):
    """The live DB held every fact twice -- "I work in IT" beside "They work in
    IT.", "My name is Walter" beside "Their name is Walter." -- because the
    normaliser folded punctuation and case but not the pronoun voice. Each fact
    was eating two of the forty slots."""
    _fresh(tmp_path)
    assert db.add_fact(1, "They work in IT.") is True
    assert db.add_fact(1, "I work in IT") is False
    assert db.add_fact(1, "Their name is Walter.") is True
    assert db.add_fact(1, "My name is Walter") is False
    assert db.add_fact(1, "I'm from tashkent") is True
    assert db.add_fact(1, "They're from tashkent") is False   # curly-quote-proof too
    assert db.add_fact(1, "I’m from tashkent") is False
    assert len(db.recent_facts(1)) == 3


def test_the_voice_fold_does_not_merge_different_facts(tmp_path):
    """Stripping the leading pronoun must not make everything after it collide."""
    _fresh(tmp_path)
    assert db.add_fact(1, "their job drains them") is True
    assert db.add_fact(1, "their boss drains them") is True
    assert len(db.recent_facts(1)) == 2


def test_a_bare_pronoun_is_not_a_fact(tmp_path):
    _fresh(tmp_path)
    assert db.add_fact(1, "they") is False
    assert db.recent_facts(1) == []


def test_empty_facts_are_not_stored(tmp_path):
    _fresh(tmp_path)
    assert db.add_fact(1, "   ") is False
    assert db.recent_facts(1) == []


def test_prune_facts_drops_the_oldest_first(tmp_path):
    _fresh(tmp_path)
    for i in range(10):
        db.add_fact(1, f"fact {i}")
    assert db.prune_facts(1, keep=4) == 6
    assert [f["fact"] for f in db.recent_facts(1)] == [
        "fact 9", "fact 8", "fact 7", "fact 6"]


def test_prune_facts_leaves_other_chats_alone(tmp_path):
    _fresh(tmp_path)
    db.add_fact(1, "a")
    db.add_fact(2, "b")
    db.prune_facts(1, keep=0)
    assert [f["fact"] for f in db.recent_facts(2)] == ["b"]


def test_facts_survive_a_second_init_db(tmp_path):
    """The migration lands on a live database with real rows in it."""
    _fresh(tmp_path)
    db.add_fact(1, "their name is walter")
    db.close()
    db.init_db(str(tmp_path / "t.db"))     # idempotent second run
    assert [f["fact"] for f in db.recent_facts(1)] == ["their name is walter"]


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


def test_clear_facts_empties_one_chat_and_leaves_others_alone(tmp_path):
    """Needed to purge production: every fact stored so far was extracted from
    the kid's own messages and attributed to the person it was texting."""
    _fresh(tmp_path)
    db.add_fact(1, "they are disciplined")
    db.add_fact(1, "they have a laundry task")
    db.add_fact(2, "their name is jesse")
    assert db.clear_facts(1) == 2
    assert db.recent_facts(1) == []
    assert [f["fact"] for f in db.recent_facts(2)] == ["their name is jesse"]


def test_clear_facts_on_a_chat_with_none_is_a_no_op(tmp_path):
    _fresh(tmp_path)
    assert db.clear_facts(999) == 0


def test_recent_messages_can_be_filtered_to_one_role(tmp_path):
    """The window has to be filtered in the query: the kid sends two or three
    messages per turn, so a mixed window of N is mostly the kid's own output."""
    _fresh(tmp_path)
    db.add_message(1, "user", "oldest user line")
    for i in range(30):
        db.add_message(1, "kid", f"k{i}")
    db.add_message(1, "user", "newest user line")
    rows = db.recent_messages(1, limit=5, role="user")
    assert [r["text"] for r in rows] == ["oldest user line", "newest user line"]
