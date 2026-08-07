# BrainrotGPT — v3 Status 🗿📈

v2 was a style-picker generator: forward a convo, choose a persona, get one
reply back. v3 replaced that with **Jayden**, a single character with his own
memory, mood, and clock, who texts you like an actual chronically-online
14-year-old rather than rewriting your convo on demand. This is the status
after the v3 build. ✅ shipped · 🟡 partial/limited · ⏭️ deliberately out of scope.

---

## ✅ Identity & voice
- [x] **One kid, one identity** — `chat_engine.py` (`IDENTITY`, `HOW_YOU_TEXT`)
- [x] **Mood wheel** — `brainrot.PERSONAS` reused as headspace, drifts every 6–24h
      (`should_reroll_mood`), not per-message
- [x] **Bond** — a per-chat relationship score (stranger/friend/annoyed) that
      shapes tone and reply speed
- [x] **Notes memory** — distilled facts about each person, refreshed every
      ~15 messages (`memory.py`)
- [x] **Shared daily life** — one LLM call a day, the same story told to every
      chat, so it reads as one person rather than N clones (`life.py`)

## ✅ Texture
- [x] **Bursts** — model output split into separately-sent messages (`burst.py`),
      with a sentence/newline fallback for the ~1-in-20 calls that drop the `|||`
      delimiter
- [x] **Paced sending** — typing indicators + human-timed gaps between messages
- [x] **Typos + corrections** — occasional fumble followed by a `*fix`
- [x] **Trailing-period-as-anger** — the kid never ends a message with a period
      unless he's being deliberately cold
- [x] **Reactions instead of replies** — low-content messages sometimes only
      get an emoji, arming no ghost ping
- ⏭️ **Send-then-delete texture (~3%)** — not implemented. It's the one `§4`
      behaviour that can't be verified without a live Telegram client, and a
      deleted message is indistinguishable from a bug in the logs. Add it
      later behind a config flag if you want it.

## ✅ Proactive behaviour
- [x] **Ghost ladder** — chases you across hours then days if you go quiet,
      then gives up (`ghost.py`, `scheduler._do_ping`)
- [x] **Sleep window** — no replies or pings 1am–9am server time
- [x] **Daily ping cap** — `MAX_PINGS_PER_DAY`
- [x] **Cold opens** — texts first unprompted, gated on bond + inactivity
      (`ghost.should_cold_open`, `scheduler._maybe_schedule_cold_opens`)
- [x] **Revival** — coming back after the kid gave up reads as salty for one reply

## ✅ Data model & scheduling
- [x] **SQLite is the single source of truth for scheduling**
      (`chat_state.next_action_at`/`next_action_kind`) — a restart never
      silently drops a pending ping the way an in-memory JobQueue `run_once`
      would
- [x] **`scheduler.py`** — the 60s tick, burst delivery, and daily jobs
      (life refresh, sticker reload, trend refresh)
- 🟡 **`budget.py` is a new module**, not in the original architecture sketch
      — the outbound LLM budget (protects the Groq quota from chats scaling
      pings/cold-opens/notes) needed a home, and `db.py` is the wrong place
      for policy

## ✅ Stickers
- [x] **Owner's own pack** — `stickers.py`, re-read daily so new stickers need
      no redeploy
- [x] **Emoji-keyed picks**, avoiding recent repeats per chat
- [x] **Sticker-in-burst** — the model can emit `[sticker:X]` as one of its
      burst pieces
- 🟡 **Sticker-only replies have no explicit config knob** — they emerge
      naturally when the model returns a lone `[sticker:X]`, which the prompt
      permits. A knob nothing reads would be dead config, so it wasn't added.

## ✅ Trends & memes
- [x] **Wider sources** — Reddit (multiple subreddits) + Know Your Meme's
      popular page, not just one subreddit
- [x] **Meme blurbs**, not just bare slang terms
- [x] **Owner curation** — `/trend list|add|ban|remove|refresh`

## ✅ Groups & safety
- [x] **Group mode** — replies only on `@mention` or reply-to-him, capped
      message count, never proactive (unprompted messages in a group read as spam)
- [x] **Content-safety screen** on inbound text, prompt-injection framing on
      forwarded/buffered content
- [x] **Outbound budget** protects the Groq quota from proactive-call growth

## ✅ Housekeeping
- [x] **Tests** — pure-logic modules (`ghost.py`, `burst.py`, `budget.py`, etc.)
      are testable in milliseconds with no Telegram/DB/network dependency
- [x] **Lint** — ruff (`pyproject.toml`) · **CI** — GitHub Actions (ruff + pytest)
- [x] **`bot.py` split** — commands, the `/settings` keyboard, and its button
      handler moved to `commands.py` to keep files under the repo's line cap

---

## Deleted from v2
The old generator surface — persona picker, intensity/length/tone dials,
best-of-N candidates, `/persona`, `/saved`, `/last`, `/leaderboard`, `/daily`
— is gone. `brainrot.py` survives only to power inline mode
(`@yourbot <text>`), a separate one-off generator unrelated to the kid's
memory, bond, or mood.

## Forward roadmap (out of scope for v3, deliberately)
- **Per-chat timezones** — the sleep window and school hours are server-local
  for everyone; a real per-user timezone would make the "asleep" signal honest
  across timezones instead of just plausible for one
- **Voice notes** — texting is text-only; no synthesized voice replies
- **Instagram integration** — trend sourcing stays on Reddit + Know Your Meme
- **TikTok Creative Center scraping** — same reasoning; adds a source but also
  a maintenance burden against an anti-scraping surface
- **Multi-kid** — one identity, one voice, by design; supporting a second
  character would mean per-chat identity selection and a much bigger prompt
  surface
- **Layered summarisation** — notes are a single distilled blob today, not a
  hierarchy of increasingly-compressed memory over time
- **Learned sticker taste** — sticker picks are emoji-matched and
  repeat-avoiding, not learned per-chat preference

## Needs your action (runtime/account-dependent)
- **Sticker pack** — set `STICKER_PACK_NAME`; see README "Setting up stickers"
- **Vision model** — `GROQ_VISION_MODEL` must be available on your Groq account
- **Groups** — turn **Group Privacy** off in @BotFather (`/setprivacy`) so the
  kid can see messages that don't `@mention` him
- **Inline mode** — enable it in @BotFather (`/setinline`) if you want
  `@yourbot <text>` to work
- **Owner features** — set `OWNER_IDS=<your telegram id>` for `/stats` and
  `/trend` (and `PRIVATE_MODE=true` to lock the bot to yourself)
- **Webhook / Docker / CI** — run on your host; set `WEBHOOK_URL` for webhooks
