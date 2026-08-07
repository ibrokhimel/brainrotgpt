import asyncio
import random

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
