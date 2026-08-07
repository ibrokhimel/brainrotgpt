import asyncio

from telegram.error import Conflict

import bot

# --- group mode helpers ---------------------------------------------------

class _Ent:
    def __init__(self, type_, offset, length, user=None):
        self.type, self.offset, self.length, self.user = type_, offset, length, user


class _User:
    def __init__(self, uid):
        self.id = uid


class _GMsg:
    def __init__(self, text, entities=None, reply_to_message=None):
        self.text = text
        self.caption = None
        self.entities = entities or []
        self.caption_entities = []
        self.reply_to_message = reply_to_message


def test_parse_mention_detects_and_strips():
    text = "@brainrotcbot roast him fr"
    m = _GMsg(text, [_Ent("mention", 0, len("@brainrotcbot"))])
    mentioned, leftover = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is True
    assert leftover == "roast him fr"


def test_parse_mention_ignores_other_username():
    m = _GMsg("@someoneelse hey", [_Ent("mention", 0, len("@someoneelse"))])
    mentioned, _ = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is False


def test_parse_mention_text_mention_by_id():
    m = _GMsg("hey you", [_Ent("text_mention", 0, 3, user=_User(999))])
    mentioned, _ = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is True


def test_reply_to_bot_true_when_replying_to_the_bots_own_message():
    bot_msg = _GMsg("earlier", entities=[])
    bot_msg.from_user = _User(999)
    m = _GMsg("fr fr", reply_to_message=bot_msg)
    assert bot.reply_to_bot(m, 999) is True


def test_reply_to_bot_false_for_a_reply_to_someone_else():
    other_msg = _GMsg("earlier", entities=[])
    other_msg.from_user = _User(123)
    m = _GMsg("fr fr", reply_to_message=other_msg)
    assert bot.reply_to_bot(m, 999) is False


def test_reply_to_bot_false_when_not_a_reply():
    m = _GMsg("fr fr")
    assert bot.reply_to_bot(m, 999) is False


# --- single-instance guard + error handler --------------------------------

def test_single_instance_lock_blocks_second_acquire():
    first = bot.acquire_single_instance_lock()
    try:
        if first:  # only assert the blocking property once we actually hold it
            assert bot.acquire_single_instance_lock() is False
    finally:
        if bot._instance_lock_sock is not None:
            bot._instance_lock_sock.close()
            bot._instance_lock_sock = None


def test_on_error_swallows_conflict_and_generic():
    class _Ctx:
        def __init__(self, error):
            self.error = error

    # neither should raise out of the handler
    asyncio.run(bot.on_error(None, _Ctx(Conflict("boom"))))
    asyncio.run(bot.on_error(None, _Ctx(RuntimeError("kaboom"))))
