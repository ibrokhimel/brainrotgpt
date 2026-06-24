# BrainrotGPT — Upgrade Wishlist / v2 Status 🗿📈

Status of the roadmap after the v2 build. ✅ shipped · 🟡 partial/limited ·
⏭️ intentionally deferred. Anything needing your infra/account is flagged at the
bottom under **Needs your action**.

---

## ✅ Tier 0 — Output variety
- [x] Persona rotation (12 registers) — `brainrot.PERSONAS`
- [x] Vocab sampling (random 8-of-36) · [x] opener variety · [x] temp/seed jitter
- [x] Smart Regenerate (avoids previous persona + reply)

## ✅ Tier 1 — High impact
- [x] **Style picker buttons** — `/settings` + 🎭 Style on the confirm card + `/persona`
- [x] **Intensity dial** — mild/medium/unhinged (length + flavor + `max_tokens`)
- [x] **Screenshot intake** — `vision.py` (Groq vision OCR → transcript)
- [x] **Persona label footer** on every result
- 🟡 **"More like this / different vibe"** — one Regenerate that diverges from the
  last persona+reply. Pinning a persona keeps it; `🎲 Random` rerolls the register.

## ✅ Tier 2 — Features & UX
- [x] **Inline mode** — `@yourbot <text>` (`on_inline`)
- [x] **`/persona`** quick picker · [x] **tone presets** (roast/cope/hype/deny/gaslight)
- [x] **Language matching** (auto or pick) · [x] **favorites + history** (`/saved`, `/last`)
- 🟡 **Better preview** — kept + truncation; no avatar/role hints yet
- ⏭️ **Edit-before-send** — Telegram can't edit a draft on the user's behalf;
  replaced by **📋 Full** (untruncated text) + the intensity dial for length.

## ✅ Tier 3 — Persistence & state
- [x] **SQLite** (`db.py`): per-chat settings, favorites, analytics, subscriptions, last result
- [x] **Rate-limit persistence** — limiter seeded from DB on startup (`RateLimiter.seed`)
- [x] **Usage analytics** (`generations` table) · [x] **session TTL cleanup** (30 min)

## ✅ Tier 4 — Reliability, safety & ops
- [x] **Retry + fallback model** (`brainrot._complete`)
- [x] **Streaming output** — optional (`STREAMING=true`), throttled message edits
- [x] **Structured logging** — `LOG_LEVEL` + optional `LOG_FILE`
- [x] **Graceful shutdown** — `post_shutdown` closes the DB
- [x] **Health check** (`/healthz`) · [x] **webhook mode** · [x] **owner allowlist toggle**
- [x] **Content-safety screen** on forwarded input (`guard.screen_input`)
- ⏭️ **External error reporting (Sentry)** — file/level logging provided; wire a DSN if you want it.

## ✅ Tier 5 — Cost, models & quality
- [x] **Best-of-N** — concurrent candidates + ◀ ▶ swipe (`generate_many`)
- [x] **Prompt-injection hardening** (`guard.wrap_untrusted`) · [x] **token budgeting** (`guard.trim_transcript`)
- 🟡 **Model A/B** — single configurable fallback chain, not per-persona A/B or a judge pass
- ⏭️ **Caching identical inputs** — deliberately omitted (it fights the variety goal);
  rate-limit + cooldown already absorb accidental double-taps.

## ✅ Tier 6 — Growth
- [x] **Share card / watermark** (`share_card.py`, Pillow)
- [x] **Group-chat mode** (`/brainrot`) · [x] **daily brainrot** (`/daily`) · [x] **leaderboard** (`/leaderboard`)

## ✅ Housekeeping
- [x] **Tests** — 36 passing (`tests/`, pure logic + mocked Groq)
- [x] **Lint** — ruff (`pyproject.toml`) · [x] **CI** — GitHub Actions (ruff + pytest)
- [x] **Dockerfile + compose + .dockerignore** · [x] **deps pinned** (floors + tested versions)
- 🟡 **Type checking** — ruff lint only; `mypy` not wired yet (easy follow-up)
- 🟡 **Config validation** — clearer failures + model fallback; no live model-ping at startup

---

## Needs your action (runtime/account-dependent)
- **Vision model** — `GROQ_VISION_MODEL` must be available on your Groq account;
  swap the id in `.env` if screenshots error.
- **Inline + group mode** — enable inline in @BotFather (`/setinline`) and turn
  **Group Privacy** off for `/brainrot` to read replied-to messages.
- **Owner features** — set `OWNER_IDS=<your telegram id>` for `/stats` (and
  `PRIVATE_MODE=true` to lock the bot to yourself).
- **Webhook / Docker / CI** — run on your host; set `WEBHOOK_URL` for webhooks.
- **Share-card footer** — edit the `t.me/your_bot` handle in `share_card.py`.

## Possible next steps
- mypy + a startup model-ping for config validation
- judge-pass best-of-N (auto-pick the funniest candidate)
- avatar/role-aware previews; richer share-card layouts (color-emoji rendering)
