"""The kid's own clock: the 60-second tick, burst delivery, and the daily jobs.

SQLite (`chat_state.next_action_at`) is the only source of truth for scheduling.
python-telegram-bot's JobQueue is in-memory, so a `run_once` days out would
silently evaporate on restart — the tick re-derives everything due from the DB
instead.

A 60-second tick is far too coarse for a reply that should land in three
seconds, so `arm_fast_reply` delivers imminent replies in-process. That is a
fast path layered ON TOP of the row, not instead of it: the schedule is written
to SQLite first, both paths take the same atomic claim (`db.claim_due_action`),
and losing the task to a restart loses nothing.

This module never imports `bot`: it is orchestrated *by* bot.py
(startup registers `scheduler.tick` on the job queue), not the other way
around, so the import graph stays acyclic. bot.py imports the ghost-ladder
bond constants and `pings_remaining` back out of here for its own intake path.
"""
import asyncio
import datetime as dt
import logging
import random
import time

from telegram.error import Forbidden
from telegram.ext import ContextTypes

import brainrot
import burst
import chat_engine
import config
import db
import ghost
import life
import memory
import stickers
import trends

logger = logging.getLogger("brainrotgpt.scheduler")

_rng = random.Random()

# Bond deltas from the ghost ladder — a person who chases and gets nothing
# feels progressively less warm about it. apply_bond's per-message deltas
# live in bot.py (the intake side); these live here because _do_ping is the
# only thing that applies them.
BOND_GHOST_STAGE = -10       # bond hit per unanswered ping, from the second rung on
BOND_GHOST_STAGE_FIRST = -3  # ...but the first rung is only a nudge
BOND_GAVE_UP = -25           # bond hit when the ladder is exhausted and the kid gives up


def bond_penalty(stage: int) -> int:
    """What an unanswered ping at this rung costs.

    A flat -10 per rung punished ordinary human latency as if it were ghosting:
    the owner replied after 22 minutes, took two rungs on the way, and sat at
    bond -18 — three points from the annoyed register (chat_engine's bond <=
    -20), which makes the kid colder and shorter with someone who did nothing
    wrong. Stage 1 fires 8-25 minutes after the kid's own message, so it must
    be cheap. From stage 2 (an hour or more) the full penalty applies, and
    someone who genuinely disappears for days still lands where the spec
    intended.
    """
    return BOND_GHOST_STAGE_FIRST if stage <= 1 else BOND_GHOST_STAGE

# Spec §12: a Groq failure during a reply job is retried once on the next tick,
# then dropped silently. The retry is encoded in next_action_kind rather than a
# new column so it survives a restart like every other scheduling decision.
RETRY_KIND = "reply:retry"
RETRY_DELAY_S = 60
REPLY_KINDS = ("reply", RETRY_KIND)

# The tick runs every 60s, which used to swamp the reply latency entirely: a
# computed 3-second delay landed anywhere from 3 to 63 seconds later. Anything
# due before the next tick could plausibly reach it is delivered in-process
# instead. Long delays — ghost pings, cold opens, sleep-window deferrals — stay
# on the tick, because an hours-long asyncio.sleep is exactly the thing SQLite
# scheduling exists to avoid.
FAST_PATH_MAX_S = 55

# Strong references to the in-flight sleepers. Without this the event loop only
# holds a weak one and a task can be garbage-collected mid-sleep.
_pending: set[asyncio.Task] = set()


def arm_fast_reply(bot_obj, chat_id: int, state: dict) -> asyncio.Task | None:
    """Deliver an imminent reply in-process rather than waiting for the tick.

    This is a *fast path*, never a replacement for the schedule: the caller has
    already written `next_action_at` to SQLite, so a process that dies mid-sleep
    still recovers the pending reply through the tick. Returns the spawned task
    (for tests) or None when the action belongs to the tick after all.
    """
    fire_at = state.get("next_action_at")
    if fire_at is None or (state.get("next_action_kind") or "reply") not in REPLY_KINDS:
        return None
    delay = float(fire_at) - time.time()
    if delay > FAST_PATH_MAX_S:
        return None
    try:
        task = asyncio.get_running_loop().create_task(_fast_reply(chat_id, float(fire_at), bot_obj))
    except RuntimeError:        # no running loop — the tick still has it
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def _fast_reply(chat_id: int, fire_at: float, bot_obj):
    delay = fire_at - time.time()
    if delay > 0:
        await asyncio.sleep(delay)
    now = time.time()
    # The same atomic claim the tick makes, so exactly one of us delivers. It
    # also declines an action that is no longer due, which is what makes this
    # task harmless once a newer message has rescheduled the reply.
    state = db.claim_due_action(chat_id, now, kinds=REPLY_KINDS)
    if state is None:
        return
    await _run_action(bot_obj, chat_id, state, now,
                      dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d"))


def pings_remaining(state: dict, today: str) -> int:
    if state.get("pings_day") != today:
        return config.MAX_PINGS_PER_DAY
    return max(0, config.MAX_PINGS_PER_DAY - int(state.get("pings_today") or 0))


async def deliver(bot_obj, chat_id: int, pieces, state: dict,
                  reply_to: int | None = None) -> bool:
    """Send a burst and record what was said. Returns True if anything landed.

    `Forbidden` is handled here rather than at each call site. This function has
    two callers holding the same contract — the tick and bot.on_group_message —
    and the right response to being blocked is identical for both: mute
    permanently and cancel all scheduling. Keeping it here means a third caller
    can't forget it, which is exactly how the group path ended up without it.
    """
    if not pieces:
        return False
    pieces = burst.apply_typos(pieces, rng=_rng)
    if stickers.enabled() and _rng.random() < config.STICKER_RANDOM_CHANCE:
        pieces = list(pieces) + [burst.Piece("sticker", _rng.choice(stickers.available_emoji()))]

    def sticker_for(emoji: str):
        return stickers.pick(chat_id, emoji, rng=_rng) or stickers.pick_random(chat_id, rng=_rng)

    try:
        sent = await burst.send(bot_obj, chat_id, pieces, rng=_rng, sleeper=asyncio.sleep,
                                sticker_for=sticker_for, reply_to=reply_to)
    except Forbidden:
        logger.info("chat %s blocked the bot — muting permanently", chat_id)
        db.update_chat_state(chat_id, muted=1, next_action_at=None, next_action_kind=None)
        return False
    for text in sent:
        db.add_message(chat_id, "kid", text)
    # Only `sent` — not `pieces`, which is guaranteed non-empty by the early
    # return above. Recording that the kid spoke when every send failed burns
    # the one wounded reply a returning user earned, and advances last_kid_ts
    # from a message that does not exist — which the ghost ladder and the
    # cold-open quiet window are both measured from.
    if sent:
        db.update_chat_state(chat_id, last_kid_ts=time.time(), salty=0)
    return bool(sent)


async def tick(context: ContextTypes.DEFAULT_TYPE):
    """The scheduler. SQLite is the source of truth, so restarts lose nothing."""
    now = time.time()
    today = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    for row in db.due_chats(now):
        # Claim the action before acting, so a failure below can never loop
        # forever re-firing it every tick — and so the in-process fast path,
        # which claims the same row, can never deliver the same reply twice.
        state = db.claim_due_action(row["chat_id"], now)
        if state is None:
            continue        # the fast path got there first
        await _run_action(context.bot, state["chat_id"], state, now, today)

    if config.COLDOPEN_ENABLED:
        await _maybe_schedule_cold_opens(now)


async def _run_action(bot_obj, chat_id: int, state: dict, now: float, today: str):
    """Perform one already-claimed action. Shared by the tick and the fast path.

    The claim has already cleared `next_action_at`, so nothing here can re-fire
    on its own — only the explicit re-arm at the bottom brings an action back.
    """
    kind = state["next_action_kind"] or "reply"
    done = True
    try:
        if kind in REPLY_KINDS:
            done = await _do_reply(bot_obj, chat_id, state, now)
        elif kind == "ping":
            await _do_ping(bot_obj, chat_id, state, now, today)
        elif kind == "coldopen":
            await _do_cold_open(bot_obj, chat_id, state)
    except Forbidden:
        logger.info("chat %s blocked the bot — muting permanently", chat_id)
        db.update_chat_state(chat_id, muted=1, next_action_at=None, next_action_kind=None)
        return
    except Exception as e:  # noqa: BLE001 — one bad chat must not stall the tick
        logger.warning("scheduled %s failed for chat %s: %s", kind, chat_id, e)
        done = False
    # A reply is owed to a person, so it gets one retry (spec §12) — on a
    # shared free-tier key a transient 5xx is the expected case, not the exotic
    # one, and dropping it answers them never. A missed ping or cold open is
    # just one the kid didn't send; retrying those would spend budget chasing
    # rather than answering.
    #
    # `done` covers a failed *delivery* as well as a raised exception:
    # burst.send swallows every non-Forbidden error, so a Telegram 5xx never
    # reaches the handler above and the retry would otherwise be blind to the
    # most likely way a reply gets lost. The muted check keeps a chat that just
    # blocked us out of it — re-arming there would leave next_action_at set on
    # a row the tick can never see again.
    if not done and kind == "reply" and not db.get_chat_state(chat_id)["muted"]:
        db.update_chat_state(chat_id, next_action_at=time.time() + RETRY_DELAY_S,
                             next_action_kind=RETRY_KIND)


async def _do_reply(bot_obj, chat_id: int, state: dict, now: float) -> bool:
    """Answer a due reply. Returns False if nothing reached them, so the tick
    can re-arm the one retry spec §12 asks for."""
    if chat_engine.should_reroll_mood(state, now, rng=_rng):
        mood = _rng.choice(brainrot.PERSONAS)[0]
        state = db.update_chat_state(chat_id, mood=mood, mood_set_at=now)
    pieces = await chat_engine.reply(chat_id, state, rng=_rng)
    if not await deliver(bot_obj, chat_id, pieces, state):
        return False    # nothing reached them — chasing an unreceived message is absurd
    if config.GHOST_ENABLED:
        fire_at, stage = ghost.next_ping(0, time.time(), rng=_rng,
                                         chattiness=state["chattiness"])
        if fire_at:
            db.update_chat_state(chat_id, next_action_at=fire_at,
                                 next_action_kind="ping", ping_stage=stage)
    state = db.get_chat_state(chat_id)
    if memory.should_distill(state):
        await memory.distill(chat_id, state)
    return True


async def _do_ping(bot_obj, chat_id: int, state: dict, now: float, today: str):
    stage = int(state["ping_stage"] or 1)
    if pings_remaining(state, today) <= 0:
        fire_at = ghost.defer_for_sleep(now + 12 * 3600, rng=_rng)
        db.update_chat_state(chat_id, next_action_at=fire_at, next_action_kind="ping")
        return
    pieces = await chat_engine.ping(chat_id, state, stage, rng=_rng)
    await deliver(bot_obj, chat_id, pieces, state)
    used = (int(state["pings_today"] or 0) + 1) if state["pings_day"] == today else 1
    bond = max(-100, int(state["bond"] or 0) + bond_penalty(stage))
    fire_at, new_stage = ghost.next_ping(stage, time.time(), rng=_rng,
                                         chattiness=state["chattiness"])
    if fire_at is None:
        db.update_chat_state(chat_id, gave_up=1, bond=max(-100, bond + BOND_GAVE_UP),
                             pings_today=used, pings_day=today, ping_stage=ghost.FINAL_STAGE)
        return
    db.update_chat_state(chat_id, next_action_at=fire_at, next_action_kind="ping",
                         ping_stage=new_stage, bond=bond, pings_today=used, pings_day=today)


async def _do_cold_open(bot_obj, chat_id: int, state: dict):
    pieces = await chat_engine.cold_open(chat_id, state, rng=_rng)
    await deliver(bot_obj, chat_id, pieces, state)


async def _maybe_schedule_cold_opens(now: float):
    candidates = db.coldopen_candidates(
        now, min_bond=config.COLDOPEN_MIN_BOND,
        active_within_s=7 * 24 * 3600, quiet_for_s=18 * 3600)
    for state in candidates:
        if ghost.should_cold_open(state, now, rng=_rng):
            db.update_chat_state(state["chat_id"],
                                 next_action_at=ghost.cold_open_at(now, rng=_rng),
                                 next_action_kind="coldopen")


# --- Daily jobs -------------------------------------------------------------

async def life_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    await life.refresh()


async def sticker_reload_job(context: ContextTypes.DEFAULT_TYPE):
    await stickers.load(context.bot)


async def trend_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled best-effort pull of fresh slang into the live trends table."""
    try:
        n = await trends.refresh()
        logger.info("scheduled trend refresh added %d term(s)", n)
    except Exception as e:  # noqa: BLE001 — never let the job crash the queue
        logger.warning("trend refresh job failed: %s", e)


async def prune_job(context: ContextTypes.DEFAULT_TYPE):
    """Enforce spec §3's 100-rows-per-chat cap on `messages`.

    This was `cleanup_sessions` and also evicted idle intake-session buffers.
    bot.py has had no `sessions` dict since the generator surface was deleted,
    so that half operated on the empty dict it was handed and did nothing.
    """
    for chat_id in db.all_chat_ids():
        db.prune_messages(chat_id)
