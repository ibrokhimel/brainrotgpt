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
