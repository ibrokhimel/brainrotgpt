import asyncio

import bot
import burst
import db


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "bk.db"))


def _run(coro):
    return asyncio.run(coro)


# --- fake Telegram objects, just enough surface for the handlers under test -

class _FakeUser:
    def __init__(self, uid, full_name="Them"):
        self.id = uid
        self.full_name = full_name


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, text=None, caption=None, from_user=None, entities=None,
                caption_entities=None, reply_to_message=None, message_id=1,
                photo=None, document=None):
        self.text = text
        self.caption = caption
        self.from_user = from_user
        self.entities = entities or []
        self.caption_entities = caption_entities or []
        self.reply_to_message = reply_to_message
        self.message_id = message_id
        self.photo = photo or []
        self.document = document
        self.reactions = []
        self.replies = []

    async def set_reaction(self, reaction):
        self.reactions.append(reaction)

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, message, chat_id):
        self.message = message
        self.effective_chat = _FakeChat(chat_id)


class _FakeBotObj:
    def __init__(self, uid=999, username="brainrotcbot"):
        self.id = uid
        self.username = username
        self.sent = []

    async def get_file(self, file_id):
        raise NotImplementedError

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_chat_action(self, chat_id, action):
        pass


class _FakeContext:
    def __init__(self, bot_obj=None):
        self.bot = bot_obj or _FakeBotObj()


def test_bond_increases_per_message(tmp_path):
    _fresh(tmp_path)
    state = db.get_chat_state(1)
    assert bot.apply_bond(state, "hi") == bot.BOND_PER_MESSAGE


def test_long_messages_are_worth_more(tmp_path):
    _fresh(tmp_path)
    state = db.get_chat_state(1)
    assert bot.apply_bond(state, "x" * 250) == bot.BOND_LONG_MESSAGE


def test_bond_is_clamped(tmp_path):
    _fresh(tmp_path)
    state = dict(db.get_chat_state(1), bond=100)
    assert bot.apply_bond(state, "hi") == 100
    state = dict(db.get_chat_state(1), bond=-100)
    assert bot.apply_bond(state, "hi") > -100      # positive input still helps


def test_low_content_detection():
    assert bot.is_low_content("lol")
    assert bot.is_low_content("💀")
    assert bot.is_low_content("ok")
    assert not bot.is_low_content("what do you think about this")


def test_pings_today_resets_on_a_new_day(tmp_path):
    _fresh(tmp_path)
    db.update_chat_state(1, pings_today=3, pings_day="2026-08-06")
    assert bot.pings_remaining(db.get_chat_state(1), "2026-08-07") > 0


def test_pings_remaining_is_zero_at_the_cap(tmp_path):
    _fresh(tmp_path)
    import config
    db.update_chat_state(1, pings_today=config.MAX_PINGS_PER_DAY, pings_day="2026-08-07")
    assert bot.pings_remaining(db.get_chat_state(1), "2026-08-07") == 0


# --- reactions instead of replies -------------------------------------------

def test_low_content_message_can_earn_only_a_reaction_and_arms_no_ghost_ping(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)     # force the reaction branch
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id, user_id = 501, 5010
    msg = _FakeMessage(text="lol", from_user=_FakeUser(user_id))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_user_message(update, _FakeContext()))

    assert msg.reactions == [msg.reactions[0]]     # set_reaction was called once
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is None          # nothing chases them
    assert state["next_action_kind"] is None


def test_reaction_chance_miss_falls_through_to_a_scheduled_reply(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)     # never take the reaction branch
    chat_id, user_id = 502, 5020
    msg = _FakeMessage(text="lol", from_user=_FakeUser(user_id))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_user_message(update, _FakeContext()))

    assert msg.reactions == []
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is not None
    assert state["next_action_kind"] == "reply"


# --- /shutup and /yo --------------------------------------------------------

def test_shutup_mutes_and_clears_the_schedule(tmp_path):
    _fresh(tmp_path)
    chat_id = 601
    db.update_chat_state(chat_id, next_action_at=123.0, next_action_kind="ping")
    msg = _FakeMessage(from_user=_FakeUser(6010))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.cmd_shutup(update, _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["muted"] == 1
    assert state["next_action_at"] is None
    assert state["next_action_kind"] is None
    assert msg.replies


def test_yo_unmutes_and_resets_the_ghost_ladder(tmp_path):
    _fresh(tmp_path)
    chat_id = 602
    db.update_chat_state(chat_id, muted=1, gave_up=1, ping_stage=4)
    msg = _FakeMessage(from_user=_FakeUser(6020))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.cmd_yo(update, _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["muted"] == 0
    assert state["gave_up"] == 0
    assert state["ping_stage"] == 0
    assert msg.replies


# --- /settings, rebuilt on chat_state ---------------------------------------

def test_settings_keyboard_has_only_the_three_kid_dials(tmp_path):
    _fresh(tmp_path)
    kb = bot.settings_kb(1)
    labels = " ".join(b.text.lower() for row in kb.inline_keyboard for b in row)
    assert "mood" in labels
    assert "chatt" in labels
    for gone in ("intensity", "length", "tone", "best-of"):
        assert gone not in labels


def test_settings_text_reports_chattiness_and_mute(tmp_path):
    _fresh(tmp_path)
    db.update_chat_state(1, chattiness="clingy", muted=1)
    text = bot.settings_text(1).lower()
    assert "clingy" in text
    assert "muted" in text


# --- group mode: no proactive behaviour -------------------------------------

def test_unmentioned_group_message_is_buffered_but_gets_no_reply(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = []
    monkeypatch.setattr(bot.scheduler, "deliver", lambda *a, **kw: calls.append((a, kw)))
    chat_id = 701
    msg = _FakeMessage(text="just chatting", from_user=_FakeUser(7010, "Alex"))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_group_message(update, _FakeContext(_FakeBotObj())))

    assert calls == []
    rows = db.recent_messages(chat_id)
    assert [r["text"] for r in rows] == ["just chatting"]
    assert db.get_chat_state(chat_id)["next_action_at"] is None


def test_mentioned_group_message_replies_capped_and_arms_no_schedule(tmp_path, monkeypatch):
    _fresh(tmp_path)
    delivered = {}

    async def fake_deliver(bot_obj, chat_id, pieces, state, reply_to=None):
        delivered["pieces"] = pieces
        delivered["reply_to"] = reply_to

    async def fake_reply(chat_id, state, *, rng):
        return [burst.Piece("text", "a"), burst.Piece("text", "b"), burst.Piece("text", "c")]

    monkeypatch.setattr(bot.scheduler, "deliver", fake_deliver)
    monkeypatch.setattr(bot.chat_engine, "reply", fake_reply)

    fake_bot = _FakeBotObj(uid=999, username="brainrotcbot")
    chat_id = 702
    ent = type("Ent", (), {"type": "mention", "offset": 0, "length": len("@brainrotcbot"), "user": None})()
    msg = _FakeMessage(text="@brainrotcbot roast him", from_user=_FakeUser(7020),
                       entities=[ent], message_id=55)
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_group_message(update, _FakeContext(fake_bot)))

    assert len(delivered["pieces"]) == bot.GROUP_MAX_MESSAGES
    assert delivered["reply_to"] == 55
    assert db.get_chat_state(chat_id)["next_action_at"] is None


def test_reply_to_bot_in_group_also_summons_a_reply(tmp_path, monkeypatch):
    _fresh(tmp_path)
    delivered = {}

    async def fake_deliver(bot_obj, chat_id, pieces, state, reply_to=None):
        delivered["called"] = True

    async def fake_reply(chat_id, state, *, rng):
        return [burst.Piece("text", "a")]

    monkeypatch.setattr(bot.scheduler, "deliver", fake_deliver)
    monkeypatch.setattr(bot.chat_engine, "reply", fake_reply)

    fake_bot = _FakeBotObj(uid=999, username="brainrotcbot")
    chat_id = 703
    bot_msg = _FakeMessage(from_user=_FakeUser(999))
    msg = _FakeMessage(text="fr fr", from_user=_FakeUser(7030), reply_to_message=bot_msg)
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_group_message(update, _FakeContext(fake_bot)))

    assert delivered.get("called") is True


# --- photo intake: same scheduling path as text -----------------------------

def test_photo_schedules_a_reply_like_a_text_message(tmp_path, monkeypatch):
    _fresh(tmp_path)

    async def fake_transcribe(image_bytes):
        return "a screenshot of a group chat arguing about pineapple pizza"

    monkeypatch.setattr(bot.vision, "transcribe_image", fake_transcribe)

    class _TgFile:
        async def download_as_bytearray(self):
            return bytearray(b"fake-image-bytes")

    class _PhotoBot(_FakeBotObj):
        async def get_file(self, file_id):
            return _TgFile()

    chat_id = 801
    photo = type("Photo", (), {"file_id": "abc123"})()
    msg = _FakeMessage(from_user=_FakeUser(8010), photo=[photo])
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_photo(update, _FakeContext(_PhotoBot())))

    rows = db.recent_messages(chat_id)
    assert len(rows) == 1
    assert "pineapple pizza" in rows[0]["text"]
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is not None
    assert state["next_action_kind"] == "reply"
    assert msg.replies == []          # no "reading the screenshot" status message
