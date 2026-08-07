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
| Life | One shared daily state — the same kid has the same day in every chat |
| Memes | Free sources only (Reddit + Know Your Meme); trends store a blurb, not just a term |
| Stickers | Sends from your own pack, model-picked contextually + occasional random |
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

### One kid means one life

A single character with N independent chats is still N clones. What makes it one
*person* is a shared **daily life state**: one LLM call per day generates what is
going on with the kid today — *mom took my phone*, *got a new game*, *sick*,
*exams this week*, **grounded** — stored globally and injected as one line into
every chat's prompt. Two different people talking to it on the same day hear
about the same thing.

It also gates availability (§4): the day state carries a school block on
weekdays, during which replies are slower and shorter, because the kid is
supposedly in class.

Lives in `life.py`, refreshed by a daily job, cached in `kid_state`. If the
generation fails, the previous day's state is reused — never a blocker.

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
| `life.py` | the kid's shared daily life state | ~90 |
| `stickers.py` | pack loading, emoji index, selection, no-repeat guard | ~120 |
| `db.py` | + `messages`, `chat_state`, `kid_state` tables; `trends` gains a blurb | ~480 |
| `trends.py` | + Know Your Meme source, meme blurbs, wider subreddit set | ~200 |
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

-- Global (not per-chat): the kid's shared life + any other singleton state.
CREATE TABLE kid_state (
  key   TEXT PRIMARY KEY,   -- 'day_state', 'day_date', 'outbound_budget_day', ...
  value TEXT NOT NULL
);
```

The existing `trends` table gains two columns so a meme can be *understood*, not
just name-dropped:

```sql
ALTER TABLE trends ADD COLUMN blurb TEXT NOT NULL DEFAULT '';  -- what it is, why it's funny
ALTER TABLE trends ADD COLUMN kind  TEXT NOT NULL DEFAULT 'term';  -- 'term' | 'meme'
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

**Delimiter fallback (required).** Llama drops format instructions roughly
1-in-20 calls. If `|||` is absent from the response, `burst.parse()` falls back
to splitting on sentence boundaries and newlines, then re-applies the caps.
Without this, one in twenty replies is a single wall of text — which is exactly
the tell the whole design exists to avoid.

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

Response speed is itself a social signal — teens read a too-fast reply as
desperate and a too-slow one as disinterest. So latency is drawn from the
situation and then **modulated by bond**:

| Situation | Base delay |
|---|---|
| Engaged (last exchange < 2 min ago) | 2–10 s |
| Cold (first message in a while) | 20–90 s |
| Busy (5% of the time) | 3–15 min |

- **high bond** → ×0.6, and a 15% chance it double-texts *before* you reply
- **low bond / `salty`** → ×2.5. Leaving you on read is a message.

### Availability — the kid is not always at its phone

**The sleep window gates replies, not just pings.** Text it at 03:00 and the
reply lands after 09:00, like a person whose phone was face-down all night. The
same window from §6 applies to both paths; this is not optional flavor, it is the
difference between a person and a service.

On weekdays the day state's school block (§1) slows and shortens replies rather
than deferring them entirely — a kid texts in class, just badly.

### Texture

- lowercase only, no paragraphs, never explains itself
- **the trailing period is a weapon, not a style rule.** A period at the end of a
  text reads as cold or angry to this generation. So: never a trailing period
  normally — but when `salty` or bond is low, use them. `k.` / `fine.` / `ok.`
  carries more attitude than any amount of emoji, and costs one prompt line.
- **fake-out** (12%): typing indicator for 8–20 s, then nothing, *then* the message
- **typos** (5%): adjacent-char swap or dropped letter; 60% of those get a
  `*correction` follow-up message
- **reactions instead of replies**: if your message is low-content (`lol`, `ok`, a
  bare emoji), 40% chance the kid just sets a Telegram reaction and says nothing —
  and arms **no** ghost ping, because there's nothing to chase
- occasionally ignores your question and changes the subject
- **reply-quotes an older message** (~8%) instead of the latest one, dragging the
  conversation back to something from ten minutes ago
- **sends then deletes** (~3%): a message that vanishes a few seconds later.
  Telegram supports it natively and nothing reads more like a real teenager.

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

## 8. Stickers

The kid sends stickers from **your own pack**, which you can update at any time.

`stickers.py` calls `get_sticker_set(config.STICKER_PACK_NAME)` on startup and
again once a day, caching `[(file_id, emoji)]` in memory. Every sticker in a
Telegram pack carries an associated emoji, so **the pack labels itself** — no
manual tagging, and adding stickers in Telegram makes them available to the kid
within a day with no redeploy.

### Selection — two paths

1. **Contextual (primary).** The pack's distinct emoji are listed in the prompt
   and the model may emit `[sticker:💀]` as an element of a burst. The sticker
   then *answers* what you said, which is what makes it read as a person; a
   randomly-fired sticker reads as a bot. If the model picks an emoji the pack
   doesn't have, fall back to the nearest available or drop the element.
2. **Random (secondary).** Independently, a small per-burst chance
   (`STICKER_RANDOM_CHANCE`, default ~7%) fires a random sticker from the pack
   regardless of context — because teenagers genuinely do that.

Sometimes (~15% of sticker sends) the sticker is the **entire reply** with no
text at all.

A no-repeat guard blocks the same `file_id` twice within the last 10 sends per
chat. If the pack is missing, renamed, or fails to load, log it, disable stickers
for the day, and keep chatting — never surface an error.

Config: `STICKER_PACK_NAME`, `STICKER_CHANCE`, `STICKER_RANDOM_CHANCE`. Empty
pack name disables the feature entirely.

## 9. Following the memes

Free sources only. No Instagram API, no paid scrapers, no ToS violations.

Instagram is deliberately **not** queried directly. Its Graph API has no public
trending endpoint; Hashtag Search requires a Business account plus App Review for
*Instagram Public Content Access* and is capped at 30 unique hashtags per rolling
7 days — roughly four a day, useless as a trend feed. More importantly Instagram
is *downstream*: Reels memes are TikTok memes several days late, so chasing IG
means chasing the slow copy.

### Sources

`TREND_SUBREDDITS` widens to include:

- **`r/OutOfTheLoop`** — the highest-signal source available for free. People ask
  *"what does X mean"* exactly as a meme peaks.
- `r/memes`, `r/dankmemes` — volume
- `r/InstagramReels` — actual Instagram content, reached through Reddit's public
  JSON rather than Instagram's wall
- existing: `r/brainrot`, `r/GenZ`, `r/teenagers`, `r/tiktokcringe`

Plus **Know Your Meme**'s newest/trending feed, which supplies curated
*explanations* rather than bare terms.

### Terms → memes

The current pipeline extracts bare slang words and injects them as vocabulary.
That is enough for a bot that decorates a sentence, not for a kid that *follows*
memes. So the extraction step now returns, per item, a term **and a one-line
blurb** — what it is and why it's funny — stored in the new `trends.blurb` /
`trends.kind` columns.

This is what gives cold opens (§7) actual content: `yo have u seen the [meme]`
with a real joke behind it, instead of a slang word dropped into a void. Prompt
injection stays cheap — a handful of terms, and at most one full meme with its
blurb per call.

All of `trends.py`'s existing safety applies unchanged: the `_DENY` substring
screen, the `_TERM_OK` shape check, owner curation via `/trend add|ban|remove`,
and best-effort failure so a dead source never reaches the bot.

## 10. Groups

On @mention or a reply to one of its messages, the same kid replies with the same
engine and the same burst machinery, capped at 2 messages. Memory is keyed to the
group chat_id, so it remembers the group's running conversation. **No ghost
pings, no cold opens** — unprompted messages in a group read as spam and risk
Telegram flagging the bot.

The existing `group_history` rolling buffer is replaced by the `messages` table,
which now serves both surfaces.

## 11. What gets deleted

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

## 12. Safety, cost, and failure handling

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

### Outbound cost budget (required)

`rate_limit.py` guards *inbound* traffic — it was built for a bot that only spoke
when spoken to. Ghost pings, cold opens, notes distillation and the daily life
state are all LLM calls **nobody asked for**, and they scale with the number of
chats. A few hundred chats quietly exhausts the Groq key, and the failure looks
like "the bot went quiet" rather than an error.

So:

- A global daily cap on proactive LLM calls, counted in `kid_state`
  (`outbound_budget_day` + a counter), reset at midnight. On exhaustion, ghost
  pings and cold opens are skipped silently; **replies to real users are never
  skipped** — they come out of a separate, unbudgeted path.
- Ghost pings and cold opens route to `config.GROQ_FALLBACK_MODEL` (the small,
  fast, cheap one). A three-word `yo` does not need the 70B model.
- Notes distillation already runs only once per ~15 messages; the daily life
  state is one call per day globally.
- Cold-open eligibility is evaluated in SQL over `chat_state`, not by waking a
  job per chat, so the tick cost stays flat as chats grow.

## 13. Testing

Pure logic, no network, no real sleeping:

- `ghost.next_ping` — every stage transition, sleep-window deferral, the daily
  cap, give-up at stage 5, reset on reply
- `burst.parse` — delimiter splitting, the **no-delimiter sentence-split
  fallback**, over-long hard-split, the 5-message cap, empty/garbage model output,
  `[sticker:X]` element extraction
- sleep-window gating of **replies** (a 03:00 message defers past 09:00) — the
  easiest thing here to get wrong and the most damaging when wrong
- latency bond modulation, and the trailing-period rule flipping on `salty`
- `stickers` — emoji indexing, unknown-emoji fallback, the no-repeat guard,
  missing/renamed pack degrading gracefully
- outbound budget — proactive calls blocked at the cap, user replies unaffected
- trend extraction producing term+blurb pairs, and `_DENY` / `_TERM_OK` still
  screening them
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
- Voice notes and the kid sending images of its own
- Any direct Instagram integration — Graph API, scraping, or paid actors (§9)
- TikTok Creative Center scraping (viable later; unofficial endpoint, can break)
- Multi-kid / user-selectable characters (explicitly rejected — there is one kid)
- Layered long-horizon summarization beyond the notes blob
- Per-chat sticker preferences or learned sticker taste
