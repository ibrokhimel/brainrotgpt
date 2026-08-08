import asyncio
import time

import bot
import burst
import db
import scheduler
import stickers


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
                photo=None, document=None, sticker=None):
        self.text = text
        self.caption = caption
        self.from_user = from_user
        self.entities = entities or []
        self.caption_entities = caption_entities or []
        self.reply_to_message = reply_to_message
        self.message_id = message_id
        self.photo = photo or []
        self.document = document
        self.sticker = sticker
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
    """A reaction-only reply must DISARM the ping the scheduler already armed.

    The ping is armed here on purpose. Asserting `next_action_at is None` on a
    fresh chat proves nothing — it is already None before the handler runs, so
    the assertion passes even if the handler does nothing at all.
    """
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)     # force the reaction branch
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id, user_id = 501, 5010
    # Mid-ladder: scheduler._do_reply armed a stage-2 ping ten minutes out.
    db.update_chat_state(chat_id, next_action_at=time.time() + 600,
                         next_action_kind="ping", ping_stage=2)
    msg = _FakeMessage(text="lol", from_user=_FakeUser(user_id))
    update = _FakeUpdate(msg, chat_id)
    _run(bot.on_user_message(update, _FakeContext()))

    assert msg.reactions == [msg.reactions[0]]     # set_reaction was called once
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is None          # nothing chases them
    assert state["next_action_kind"] is None
    assert state["ping_stage"] == 0                 # the ladder is reset, not paused


def test_reaction_only_reply_revives_a_gave_up_chat(tmp_path, monkeypatch):
    """The reaction path is still a message from a real person, so spec §6's
    revival applies to it exactly as it does to the main path."""
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id = 503
    db.update_chat_state(chat_id, gave_up=1, ping_stage=5)
    msg = _FakeMessage(text="lol", from_user=_FakeUser(5030))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["gave_up"] == 0
    assert state["salty"] == 1


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


# --- Fix 2: how long a chat counts as engaged -------------------------------

def _engaged_spy(monkeypatch):
    """Capture the `engaged` flag bot.on_user_message hands to ghost."""
    seen = {}

    def spy(now, *, engaged, bond, salty, rng, in_school=False):
        seen.update(engaged=engaged)
        return now + 3600       # far out, so no in-process delivery is armed

    monkeypatch.setattr(bot.ghost, "schedule_reply_at", spy)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)     # skip the reaction branch
    return seen


def test_a_chat_is_still_engaged_five_minutes_after_the_kid_spoke(tmp_path, monkeypatch):
    """A 120-second window classified someone who had just been texting as a
    cold open and charged them the 20-90s first-contact delay. In a real
    back-and-forth the other person is still holding their phone for minutes."""
    _fresh(tmp_path)
    seen = _engaged_spy(monkeypatch)
    chat_id = 520
    db.update_chat_state(chat_id, last_kid_ts=time.time() - 5 * 60)
    msg = _FakeMessage(text="what are you up to", from_user=_FakeUser(5200))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    assert seen["engaged"] is True


def test_a_chat_quiet_for_an_hour_is_not_engaged(tmp_path, monkeypatch):
    """The cold path still exists — widening the window must not delete it."""
    _fresh(tmp_path)
    seen = _engaged_spy(monkeypatch)
    chat_id = 521
    db.update_chat_state(chat_id, last_kid_ts=time.time() - 3600)
    msg = _FakeMessage(text="you there", from_user=_FakeUser(5210))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    assert seen["engaged"] is False


# --- revival: coming back after the kid gave up -----------------------------

def test_returning_user_is_not_penalised_again_for_coming_back(tmp_path, monkeypatch):
    """The −25 give-up hit is charged once, by _do_ping, at give-up.

    Charging it a second time on return charges the user for returning, and
    because it was applied after apply_bond's clamp it wrote through the −100
    floor as well.
    """
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)     # skip the reaction branch
    chat_id = 504
    db.update_chat_state(chat_id, gave_up=1, bond=-100, ping_stage=5)
    msg = _FakeMessage(text="hey sorry i was busy all week", from_user=_FakeUser(5040))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["bond"] >= -100          # never written below the floor
    assert state["bond"] == -99           # +1 for the message, and nothing else
    assert state["gave_up"] == 0
    assert state["salty"] == 1


def test_returning_user_gets_the_slow_salty_reply_delay(tmp_path, monkeypatch):
    """The turn a user returns on is the one the design wants slowed to x2.5.
    Passing the *old* salty (still 0 at that point) gave them a fast warm one."""
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)
    seen = {}

    def spy(now, *, engaged, bond, salty, rng, in_school=False):
        seen.update(engaged=engaged, bond=bond, salty=salty)
        return now + 60

    monkeypatch.setattr(bot.ghost, "schedule_reply_at", spy)
    chat_id = 505
    db.update_chat_state(chat_id, gave_up=1, bond=-100)
    msg = _FakeMessage(text="hey", from_user=_FakeUser(5050))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    assert seen["salty"] is True          # the freshly computed salty, not state["salty"]
    assert seen["bond"] == -99            # the new bond, not the pre-update one


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

class _TgFile:
    async def download_as_bytearray(self):
        return bytearray(b"fake-image-bytes")


class _PhotoBot(_FakeBotObj):
    async def get_file(self, file_id):
        return _TgFile()


def _photo_msg(user_id):
    photo = type("Photo", (), {"file_id": "abc123"})()
    return _FakeMessage(from_user=_FakeUser(user_id), photo=[photo])


def _stub_vision(monkeypatch):
    async def fake_transcribe(image_bytes):
        return "a screenshot of a group chat arguing about pineapple pizza"

    monkeypatch.setattr(bot.vision, "transcribe_image", fake_transcribe)


def test_photo_schedules_a_reply_like_a_text_message(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _stub_vision(monkeypatch)
    chat_id = 801
    _run(bot.on_photo(_FakeUpdate(_photo_msg(8010), chat_id), _FakeContext(_PhotoBot())))

    rows = db.recent_messages(chat_id)
    assert len(rows) == 1
    assert "pineapple pizza" in rows[0]["text"]
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is not None
    assert state["next_action_kind"] == "reply"


def test_photo_from_a_gave_up_chat_is_actually_deliverable(tmp_path, monkeypatch):
    """A photo must revive a given-up chat, not deadlock it.

    `gave_up=1` is armed here on purpose: it is 0 on a fresh chat, so asserting
    it is 0 afterwards proves nothing. The real assertion is `due_chats` — the
    scheduler's query excludes `gave_up=1`, so a reply scheduled without
    clearing it can never fire, and `coldopen_candidates` requires
    `next_action_at IS NULL`, so the chat is stuck in both directions forever.
    """
    _fresh(tmp_path)
    _stub_vision(monkeypatch)
    chat_id = 802
    db.update_chat_state(chat_id, gave_up=1, ping_stage=5)
    _run(bot.on_photo(_FakeUpdate(_photo_msg(8020), chat_id), _FakeContext(_PhotoBot())))

    state = db.get_chat_state(chat_id)
    assert state["gave_up"] == 0
    assert state["salty"] == 1        # they're back — one wounded reply is owed
    assert state["ping_stage"] == 0
    due = [row["chat_id"] for row in db.due_chats(state["next_action_at"])]
    assert chat_id in due             # the scheduler can actually see it


def test_photo_does_not_narrate_that_it_is_looking(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _stub_vision(monkeypatch)
    msg = _photo_msg(8030)
    _run(bot.on_photo(_FakeUpdate(msg, 803), _FakeContext(_PhotoBot())))
    assert msg.replies == []          # no "reading the screenshot" status message


def test_photo_is_rate_limited_like_a_text_message(tmp_path, monkeypatch):
    """A photo costs a multimodal Groq call. Without the limiter one user
    holding down send on an album drains the quota for every chat."""
    _fresh(tmp_path)
    _stub_vision(monkeypatch)
    calls = []
    monkeypatch.setattr(bot.limiter, "check", lambda uid: (calls.append(uid), (False, "slow down"))[1])
    _run(bot.on_photo(_FakeUpdate(_photo_msg(8040), 804), _FakeContext(_PhotoBot())))

    assert calls == [8040]                       # the limiter was consulted
    assert db.recent_messages(804) == []         # and the refusal was honoured
    assert db.get_chat_state(804)["next_action_at"] is None


def test_intake_consults_the_school_block(tmp_path, monkeypatch):
    """The wiring, not the arithmetic: in_school reaches schedule_reply_at.
    ghost.py stays pure -- bot.py reads life and passes the answer in."""
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)     # skip the reaction branch
    monkeypatch.setattr(bot.life, "in_school_block", lambda ts: True)
    seen = {}

    def spy(now, *, engaged, bond, salty, rng, in_school=False):
        seen["in_school"] = in_school
        return now + 60

    monkeypatch.setattr(bot.ghost, "schedule_reply_at", spy)
    msg = _FakeMessage(text="what are you up to", from_user=_FakeUser(5060))
    _run(bot.on_user_message(_FakeUpdate(msg, 506), _FakeContext()))

    assert seen["in_school"] is True


def test_reaction_does_not_cancel_a_reply_the_kid_still_owes(tmp_path, monkeypatch):
    """A reaction answers THIS message. It must not delete the reply the kid
    still owes for an EARLIER one.

    The sequence is ordinary: a substantive message arms a reply 20-90s out,
    then the user double-texts "lol" before it fires. C2 only ever required
    disarming a *ping* -- a ping chases someone who has gone quiet, and someone
    who just reacted has not. A pending reply is the opposite: it is a debt.
    """
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)     # force the reaction branch
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id = 507
    due = time.time() + 45
    db.update_chat_state(chat_id, next_action_at=due, next_action_kind="reply")
    msg = _FakeMessage(text="lol", from_user=_FakeUser(5070))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    assert msg.reactions                            # it really took the reaction branch
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] == due, "the owed reply was silently discarded"
    assert state["next_action_kind"] == "reply"


def test_reaction_does_not_cancel_a_pending_reply_retry(tmp_path, monkeypatch):
    """A retry is an owed reply too -- one whose first attempt already failed."""
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id = 508
    due = time.time() + 60
    db.update_chat_state(chat_id, next_action_at=due, next_action_kind=scheduler.RETRY_KIND)
    msg = _FakeMessage(text="ok", from_user=_FakeUser(5080))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] == due
    assert state["next_action_kind"] == scheduler.RETRY_KIND


def test_reaction_disarms_a_pending_cold_open(tmp_path, monkeypatch):
    """A cold open is the kid texting first. The user just texted, so it is
    moot -- same reasoning as the ping, opposite of the owed reply."""
    _fresh(tmp_path)
    monkeypatch.setattr(bot._rng, "random", lambda: 0.0)
    monkeypatch.setattr(bot._rng, "choice", lambda seq: seq[0])
    chat_id = 509
    db.update_chat_state(chat_id, next_action_at=time.time() + 600, next_action_kind="coldopen")
    msg = _FakeMessage(text="lol", from_user=_FakeUser(5090))
    _run(bot.on_user_message(_FakeUpdate(msg, chat_id), _FakeContext()))

    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is None
    assert state["next_action_kind"] is None


def test_on_photo_ignores_an_edited_message_with_no_message_object(tmp_path):
    """allowed_updates=ALL_TYPES means an edited_message arrives with
    update.message None. on_user_message and on_group_message both guard it."""
    _fresh(tmp_path)

    class _EditedUpdate:
        message = None
        effective_chat = _FakeChat(810)

    _run(bot.on_photo(_EditedUpdate(), _FakeContext(_PhotoBot())))   # must not raise
    assert db.get_chat_state(810)["next_action_at"] is None


# --- Stickers: /stickers's capture prompt vs. an ordinary sticker ---------

class _StickerBot(_FakeBotObj):
    def __init__(self, items=None, *a, **kw):
        super().__init__(*a, **kw)
        self._items = items if items is not None else [("a", "💀"), ("b", "🗿")]
        self.requested = None

    async def get_sticker_set(self, name):
        self.requested = name
        return type("Set", (), {"stickers": [
            type("S", (), {"file_id": f, "emoji": e})() for f, e in self._items
        ]})()


def _sticker_msg(user_id, set_name="pack1", emoji="💀"):
    sticker = type("Sticker", (), {"set_name": set_name, "emoji": emoji, "file_id": "sid1"})()
    return _FakeMessage(from_user=_FakeUser(user_id), sticker=sticker)


def test_sticker_with_no_capture_pending_is_recorded_as_a_message_and_schedules_a_reply(tmp_path):
    _fresh(tmp_path)
    stickers.reset()
    chat_id = 901
    _run(bot.on_sticker(_FakeUpdate(_sticker_msg(9010), chat_id), _FakeContext(_StickerBot())))

    rows = db.recent_messages(chat_id)
    assert len(rows) == 1
    assert "💀" in rows[0]["text"]
    state = db.get_chat_state(chat_id)
    assert state["next_action_at"] is not None
    assert state["next_action_kind"] == "reply"


def test_sticker_capture_bypasses_conversation_intake_when_owner_and_pending(tmp_path, monkeypatch):
    _fresh(tmp_path)
    stickers.reset()
    monkeypatch.setattr(bot.config, "OWNER_IDS", {9020})
    stickers.arm_capture()
    chat_id = 902
    _run(bot.on_sticker(
        _FakeUpdate(_sticker_msg(9020, set_name="ownerpack"), chat_id), _FakeContext(_StickerBot())
    ))

    assert db.recent_messages(chat_id) == []             # not treated as conversation
    assert db.get_chat_state(chat_id)["next_action_at"] is None
    assert stickers.pack_names() == ["ownerpack"]
    assert not stickers.capture_pending()
