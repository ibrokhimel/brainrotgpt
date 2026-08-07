import asyncio

import bot
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
