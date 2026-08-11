import asyncio
import random

import pytest
from telegram.error import Forbidden

import burst


class FakeBot:
    def __init__(self):
        self.sent, self.stickers, self.actions = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((text, kw.get("reply_to_message_id")))
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_sticker(self, chat_id, sticker, **kw):
        self.stickers.append(sticker)
        return type("M", (), {"message_id": 99})()

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)


def _run(coro):
    return asyncio.run(coro)


def _sleeper():
    delays = []

    async def sleep(s):
        delays.append(s)
    return sleep, delays


def test_typing_time_scales_with_length_and_is_capped():
    rng = random.Random(0)
    short = burst.typing_time("yo", rng=rng)
    long = burst.typing_time("x" * 400, rng=rng)
    assert short < long
    assert long <= 6.0


def test_send_emits_typing_action_before_each_message():
    bot, (sleep, _) = FakeBot(), _sleeper()
    pieces = [burst.Piece("text", "yo"), burst.Piece("text", "wsp")]
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep))
    assert len(bot.actions) == 2
    assert [t for t, _ in bot.sent] == ["yo", "wsp"]


def test_send_sleeps_between_messages():
    bot, (sleep, delays) = FakeBot(), _sleeper()
    pieces = [burst.Piece("text", "a"), burst.Piece("text", "b")]
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep))
    assert len(delays) >= 3          # typing for a, think gap, typing for b
    assert all(d >= 0 for d in delays)


def test_send_resolves_stickers_via_callback():
    bot, (sleep, _) = FakeBot(), _sleeper()
    pieces = [burst.Piece("sticker", "💀")]
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep,
                    sticker_for=lambda e: "FILEID"))
    assert bot.stickers == ["FILEID"]
    assert bot.sent == []


def test_unknown_sticker_emoji_is_dropped_not_sent_as_text():
    bot, (sleep, _) = FakeBot(), _sleeper()
    pieces = [burst.Piece("sticker", "🦄"), burst.Piece("text", "yo")]
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep,
                    sticker_for=lambda e: None))
    assert bot.stickers == []
    assert [t for t, _ in bot.sent] == ["yo"]


def test_reply_to_is_applied_to_first_message_only():
    bot, (sleep, _) = FakeBot(), _sleeper()
    pieces = [burst.Piece("text", "a"), burst.Piece("text", "b")]
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep, reply_to=77))
    assert bot.sent[0][1] == 77
    assert bot.sent[1][1] is None


def test_apply_typos_is_deterministic_for_a_seed():
    pieces = [burst.Piece("text", "absolutely unhinged behaviour")] * 10
    a = [p.value for p in burst.apply_typos(pieces, rng=random.Random(7))]
    b = [p.value for p in burst.apply_typos(pieces, rng=random.Random(7))]
    assert a == b


def test_apply_typos_never_touches_stickers():
    pieces = [burst.Piece("sticker", "💀")] * 50
    out = burst.apply_typos(pieces, rng=random.Random(1))
    assert all(p.kind == "sticker" and p.value == "💀" for p in out)


def test_send_survives_a_failing_send():
    class Broken(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            raise RuntimeError("network")

    bot, (sleep, _) = Broken(), _sleeper()
    sent = _run(burst.send(bot, 1, [burst.Piece("text", "yo")],
                           rng=random.Random(0), sleeper=sleep))
    assert sent == []


def test_send_propagates_forbidden_so_the_caller_can_mute():
    """Unlike an ordinary send failure, a blocked-bot signal must not be
    swallowed — the caller (scheduler.tick) relies on it to mute the chat
    permanently instead of retrying forever."""
    class Blocked(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            raise Forbidden("bot was blocked by the user")

    bot, (sleep, _) = Blocked(), _sleeper()
    with pytest.raises(Forbidden):
        _run(burst.send(bot, 1, [burst.Piece("text", "yo")],
                        rng=random.Random(0), sleeper=sleep))


# --- pacing and the reply-quote after a failed send -------------------------

def test_a_failed_send_paces_the_next_piece_but_keeps_the_reply_quote():
    """A failed send has been attempted but did not land. It should earn the
    next piece a think gap -- it used to skip it -- without spending the
    reply-quote on a message the group never saw."""
    gaps = []

    async def sleeper(s):
        gaps.append(s)

    class _Bot:
        def __init__(self):
            self.calls = []
            self.n = 0

        async def send_chat_action(self, chat_id, action):
            pass

        async def send_message(self, chat_id, text, **kw):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("first send blew up")
            self.calls.append((text, kw.get("reply_to_message_id")))

    b = _Bot()
    pieces = [burst.Piece("text", "one"), burst.Piece("text", "two")]
    out = asyncio.run(burst.send(b, 1, pieces, rng=random.Random(7), sleeper=sleeper,
                                 reply_to=55))

    assert out == ["two"]
    assert b.calls == [("two", 55)], "the reply-quote moved to the message that landed"
    assert len(gaps) > 2, "the second piece still got its think gap"


def test_a_sticker_skipped_for_want_of_a_pack_consumes_no_gap_and_no_quote():
    gaps = []

    async def sleeper(s):
        gaps.append(s)

    class _Bot:
        def __init__(self):
            self.calls = []

        async def send_chat_action(self, chat_id, action):
            pass

        async def send_message(self, chat_id, text, **kw):
            self.calls.append((text, kw.get("reply_to_message_id")))

    b = _Bot()
    pieces = [burst.Piece("sticker", "skull"), burst.Piece("text", "one")]
    asyncio.run(burst.send(b, 1, pieces, rng=random.Random(7), sleeper=sleeper,
                           sticker_for=lambda e: None, reply_to=55))

    assert b.calls == [("one", 55)]


def test_parsed_sticker_with_no_pack_match_is_dropped_not_leaked():
    """End-to-end: a message that is nothing but an unresolvable sticker
    directive must vanish entirely, not surface as a sticker send or as
    literal text."""
    bot, (sleep, _) = FakeBot(), _sleeper()
    pieces = burst.parse("[sticker:🦄]")
    _run(burst.send(bot, 1, pieces, rng=random.Random(0), sleeper=sleep,
                    sticker_for=lambda e: None))
    assert bot.stickers == []
    assert bot.sent == []
