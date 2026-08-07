# BrainrotGPT v3 — "the kid" design

**Date:** 2026-08-07
**Status:** approved, ready for implementation planning

## Summary

BrainrotGPT stops being a reply *generator* and becomes a reply *person*. Today
you forward it a conversation and it hands you one paragraph to paste back.
After this change, you text it and **a single brainrotted kid texts you back** —
in bursts, at human speed, and if you ghost it, it chases you a few times and
eventually gives up.

This is a full pivot. The buffer → confirm-card → ✅ Generate → result-card flow
is deleted.

## The core decisions

| Decision | Choice |
|---|---|
| Scope | Full pivot — the bot **is** the kid |
| Identity | **One kid, globally.** Same name, same personality, every chat |
| Surface | Chat in DMs and groups (on @mention); **ghost-pings in DMs only** |
| Give-up | Stage 5 fires, then permanent silence until *you* speak first |
| Cold opens | Yes, occasional, fuelled by the existing live-trends engine |
| Memory | Last ~20 turns verbatim + an LLM-distilled per-chat notes blob |
| Relationship | Hidden per-chat bond score that drifts with how you treat it |
| Kept from v2 | Screenshot intake, inline mode, trends engine, guard, rate limits |

## 1. Identity — one kid

There is exactly one character. It has a name, an age, and a fixed voice, and
every user talks to the *same* person. What varies between chats is not *who it
is* but *how well it knows you*.

`brainrot.PERSONAS` (12 entries) is **not** deleted and is **not** used as
"which character am I today." It is repurposed as a **mood wheel**: one
consistent teenager who is sigma-brained one day, delulu the next, in their
villain era on Thursday. Mood is a per-chat state that drifts slowly (see §5),
not a per-message reroll.

The identity itself lives in one place — a `KID` block in `chat_engine.py`
holding name, age, and the ~10 lines of fixed voice that never change:
chronically online, thinks in memes, dramatic about nothing, types in lowercase,
has the attention span of a goldfish, genuinely likes you.

The kid's name ships as a single constant, `KID_NAME = "Jayden"` — change that
one line to rename it. Age is 14. Neither is user-configurable; there is one kid.

### Why one kid matters

`brainrot.choose_persona()` currently rerolls the register on *every single
generation*. A contact whose entire personality changes between two messages
reads as software. Pinning the identity globally and letting only mood drift is
the single highest-leverage change for the illusion.

## 2. Architecture

`bot.py` becomes an intake + scheduler. Nothing the kid does is synchronous with
your message.

```
your msg  → persist → cancel pending ghost → reset stage → draw a human delay
          → (later) reply job → build prompt → LLM burst → paced send → arm ghost stage 1

tick(60s) → scan chat_state for anything due → ghost ping | cold open | stranded reply
```

### Modules

| File | Role | Est. lines |
|---|---|---|
| `bot.py` | Telegram handlers, wiring, entry point | ~450 |
| `chat_engine.py` | `KID` identity, conversational prompt assembly, burst generation | ~250 |
| `burst.py` | parse burst → typing actions → delays → typos → send | ~140 |
| `ghost.py` | ping ladder, sleep window, cold opens, the 60s tick | ~200 |
| `memory.py` | message window assembly + notes distillation | ~150 |
| `db.py` | + `messages`, `chat_state` tables | ~450 |
| `brainrot.py` | personas-as-moods, VOCAB, trends blending, inline-mode generation | unchanged |
| `guard.py`, `vision.py`, `rate_limit.py`, `trends.py`, `health.py`, `config.py` | unchanged in role | — |

Every file stays under the 500-line project limit. Each module has one job and a
narrow interface: `burst.send(bot, chat_id, text)` knows nothing about ghosting;
`ghost.next_ping(stage, now)` is a pure function that knows nothing about
Telegram.

## 3. Data model

Two new tables in `db.py`.

```sql
CREATE TABLE messages (
  id      INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  role    TEXT    NOT NULL,   -- 'user' | 'kid'
  text    TEXT    NOT NULL,
  ts      REAL    NOT NULL
);
CREATE INDEX idx_messages_chat_ts ON messages(chat_id, ts);

CREATE TABLE chat_state (
  chat_id          INTEGER PRIMARY KEY,
  mood             TEXT    NOT NULL DEFAULT 'skibidi',  -- a PERSONAS key
  mood_set_at      REAL,
  bond             INTEGER NOT NULL DEFAULT 0,          -- -100..100
  notes            TEXT    NOT NULL DEFAULT '',         -- distilled facts about this user
  msgs_since_notes INTEGER NOT NULL DEFAULT 0,
  ping_stage       INTEGER NOT NULL DEFAULT 0,          -- 0..5
  next_action_at   REAL,                                -- unix ts, NULL = nothing pending
  next_action_kind TEXT,                                -- 'reply' | 'ping' | 'coldopen'
  last_user_ts     REAL,
  last_kid_ts      REAL,
  pings_today      INTEGER NOT NULL DEFAULT 0,
  pings_day        TEXT,                                -- 'YYYY-MM-DD' for the counter reset
  gave_up          INTEGER NOT NULL DEFAULT 0,
  salty            INTEGER NOT NULL DEFAULT 0,          -- 1 = owed one wounded reply after revival
  chattiness       TEXT    NOT NULL DEFAULT 'normal',   -- chill | normal | clingy
  muted            INTEGER NOT NULL DEFAULT 0
);
```

`messages` is pruned to the most recent 100 rows per chat. The existing
`cleanup_sessions` repeating job (currently only evicting in-memory sessions) is
extended to do this prune.

### Why the DB and not `job_queue`

`python-telegram-bot`'s `JobQueue` is in-memory. A `run_once` scheduled three
days out evaporates on restart — which would silently kill every pending ghost
ping in production and be almost impossible to notice. `chat_state.next_action_at`
is therefore the **only** source of truth for anything more than a minute away.
A single `run_repeating` tick every 60s scans:

```sql
SELECT * FROM chat_state
WHERE next_action_at <= :now AND muted = 0 AND gave_up = 0
```

In-process `asyncio` sleeps are used *only* for sub-minute burst pacing, where
losing one on restart is harmless.

## 4. Sounding human

### Bursts

The model is instructed to emit messages separated by `|||`. `burst.parse()`
splits, strips, drops empties, caps at 5 messages, and hard-splits anything over
~180 chars. Burst size is nudged by the prompt with a weighted target:
1 msg 40% · 2 msgs 35% · 3 msgs 20% · 4+ 5%. Most individual messages are under
10 words.

This directly inverts today's `BASE_RULES`, which mandates "NO line breaks... it's
one flowing message." `chat_engine.py` gets its own base prompt; `BASE_RULES`
stays behind only for inline mode.

### Pacing

```
for i, m in enumerate(msgs):
    if i: sleep(uniform(0.5, 1.6))            # think gap
    send_chat_action(TYPING)
    sleep(min(len(m)/14 + uniform(0.2, 0.8), 6.0))
    send(m)
```

The first reply carries an extra read delay — a real person reads before typing.

### Reply latency

Drawn per incoming message:

| Situation | Delay |
|---|---|
| Engaged (last exchange < 2 min ago) | 2–10 s |
| Cold (first message in a while) | 20–90 s |
| Busy (5% of the time) | 3–15 min |

### Texture

- lowercase only, no trailing periods, no paragraphs, never explains itself
- **fake-out** (12%): typing indicator for 8–20 s, then nothing, *then* the message
- **typos** (5%): adjacent-char swap or dropped letter; 60% of those get a
  `*correction` follow-up message
- **reactions instead of replies**: if your message is low-content (`lol`, `ok`, a
  bare emoji), 40% chance the kid just sets a Telegram reaction and says nothing —
  and arms **no** ghost ping, because there's nothing to chase
- occasionally ignores your question and changes the subject

All randomness routes through a single seeded `random.Random` held by `burst.py`
so tests are deterministic.

## 5. Mood & bond

**Mood** is one of the 12 `PERSONAS` keys, held per chat, rerolled when it has
been stale for 6–24 hours (jittered) rather than per message. It contributes one
line to the prompt. The same kid, in a different headspace.

**Bond** is a hidden per-chat integer:

| Event | Δ |
|---|---|
| you send a message | +1 |
| you send a long message (>200 chars) | +3 |
| a ghost stage elapses unanswered | −10 |
| the kid gives up on you | −25 |

It buckets into exactly one prompt line — *barely knows you, slightly guarded* /
*friend, casual, inside jokes* / *genuinely annoyed with you*. Small mechanism,
but it is what makes week three feel different from day one.

**Notes** are distilled lazily: every ~15 messages one cheap LLM pass rewrites
`chat_state.notes` into a short paragraph — your name, what you do, what you keep
complaining about, running bits. Not a fact table; a paragraph the kid
"remembers." Capped at ~600 chars so it can't grow unbounded into the prompt.

## 6. The ghost ladder

Implemented as a pure function, `ghost.next_ping(stage, now, *, rng) -> (fire_at, new_stage)`,
so three-day behavior is testable in milliseconds.

Each stage's delay is measured from the kid's own last outbound message — and a
ping *is* an outbound message, so stage 2 is timed from when stage 1 fired, not
from the original reply.

| Stage | Jittered delay after last kid message | Energy |
|---|---|---|
| 1 | 8–25 min | `yo` |
| 2 | 1–3 hrs | `helloo` |
| 3 | 6–12 hrs | `bro???? 💀` |
| 4 | 20–30 hrs | `damn ok 📉` |
| 5 | 2–3 days | `aight bet` → `gave_up = 1`, **stops permanently** |

Ping *text* is LLM-generated in-persona from the stage plus the last topic
("still thinkin about ur thing btw"), not canned strings — canned lines repeat
and give the game away immediately.

### Guards

- **Sleep window**: no pings 01:00–09:00. A ping landing inside it defers to
  09:00 + up to 90 min of jitter. (Server-local time; a per-chat timezone is
  explicitly out of scope for this iteration.)
- **Daily cap**: max 3 unanswered pings per chat per day (`pings_today` /
  `pings_day`).
- **Reset**: any message from you sets `ping_stage = 0` and clears the pending
  action.
- **DMs only.** Groups never get ghost-pinged.

### Revival after give-up

`gave_up` stops all scheduling. Your next message revives the chat
(`gave_up = 0`) and sets `salty = 1`. The next reply generated gets an extra
prompt line — *they ghosted you for days and are only NOW replying; be wounded
and salty about it* — and clears the flag on send. Exactly one wounded reply,
then normal.

## 7. Cold opens

The kid occasionally texts first, unprompted. Eligibility, all required:

- not `gave_up`, not `muted`, no action already pending
- you were active within the last 7 days
- ≥18 h since the kid last spoke
- inside awake hours
- `bond >= 10` (roughly: you've exchanged ten or so messages — it doesn't cold-open
  at strangers)

For an eligible chat, ~1-in-3 chance per day, scheduled at a random plausible
hour. Content pulls a fresh term from the existing live-trends engine
(`db.trend_terms_for_generation`) — `yo have u seen the ___` — or calls back to
something in `notes`.

**`/daily` and the `subscriptions` table are deleted.** A fixed-9am horoscope is
a bot behavior; cold opens do the same job the way a person would.

## 8. Groups

On @mention or a reply to one of its messages, the same kid replies with the same
engine and the same burst machinery, capped at 2 messages. Memory is keyed to the
group chat_id, so it remembers the group's running conversation. **No ghost
pings, no cold opens** — unprompted messages in a group read as spam and risk
Telegram flagging the bot.

The existing `group_history` rolling buffer is replaced by the `messages` table,
which now serves both surfaces.

## 9. What gets deleted

From `bot.py`: `confirm_keyboard`, `confirm_message`, `start_fresh_if_done`,
`result_keyboard`, `cook`, `_cook_stream`, `render_result`, `animate`, the
cooking-frames animation, candidate/best-of-N flipping, `schedule_confirm` /
`send_confirm_job` / `show_confirm`, `cmd_done`, `cmd_saved`, `cmd_last`,
`cmd_leaderboard`, `cmd_daily`, `schedule_daily`, `daily_job`.

Files: `share_card.py`.

Tables: `favorites`, `subscriptions`, `last_results`.

`/settings` shrinks from six dials to three:

- **mood reroll** — force a new mood now instead of waiting for the 6–24 h drift
- **chattiness** — `chill` / `normal` / `clingy`, one multiplier applied to burst
  size, ghost-ladder delays, and cold-open frequency together
- **mute** — same as `/shutup`

Length, intensity, tone, best-of-N and persona-pinning all go — a person does not
have a length dial.

### What survives

- **Screenshot intake** (`vision.py`) — the kid looks at your picture and reacts
  to it in a burst. Very much a thing a person does.
- **Inline mode** (`on_inline` + `brainrot.generate`) — a leftover paste-a-reply
  utility that costs nothing to keep and doesn't touch the illusion.
- **Trends engine** (`trends.py`, `/trend`) — now feeds both replies and cold opens.
- `guard.py` screening + prompt-injection wrapping on all inbound text,
  `rate_limit.py`, `health.py`, webhook/polling, single-instance lock.

## 10. Safety and failure handling

- **Blocked by user**: any `telegram.error.Forbidden` on an outbound message sets
  `muted = 1` permanently and cancels all scheduling for that chat. This is the
  single most important guard — a bot that keeps scheduling sends to someone who
  blocked it is how you get the token banned.
- `/shutup` mutes, `/yo` unmutes.
- Groq failure during a reply job: log, do **not** send an error card (a person
  does not say "⚠️ generation failed"), retry once on the next tick, then drop it
  silently.
- `guard.screen_input` still runs on everything inbound; the kid's own output
  keeps the existing "never hateful, threatening, or harmful" rule.
- Rate limiting stays, but is now about protecting the Groq quota rather than
  gating a user-facing button.

## 11. Testing

Pure logic, no network, no real sleeping:

- `ghost.next_ping` — every stage transition, sleep-window deferral, the daily
  cap, give-up at stage 5, reset on reply
- `burst.parse` — delimiter splitting, over-long hard-split, the 5-message cap,
  empty/garbage model output
- burst pacing — an injected clock asserts the delay *sequence* without sleeping
- typo injection and reaction-instead-of-reply, with a seeded RNG
- `memory` window assembly and the notes cap
- cold-open eligibility — each precondition independently
- `chat_engine` prompt assembly against a mocked Groq, as the existing
  `tests/test_brainrot.py` already does

Existing tests for `guard`, `rate_limit`, `trends`, `db` stay; `test_share_card.py`
and the deleted-surface parts of `test_bot_helpers.py` go.

## Out of scope for this iteration

- Per-chat timezone detection for the sleep window (server-local only)
- Voice notes, stickers, or the kid sending images
- Multi-kid / user-selectable characters (explicitly rejected — there is one kid)
- Layered long-horizon summarization beyond the notes blob
