"""Live A/B: does the closing line veto function calling?

Not a suite test — it hits the real Gemini API. The suite stubs the provider,
which is exactly why this bug survived three rounds of green tests.

Test query is `whats the weather in tashkent rn`: no model can know it from
training, so a text answer is necessarily an invention. (`sybau` proves nothing
— Gemini knows it already and answers correctly without searching.)

Run from the repo root:

    BOT_TOKEN=x GROQ_API_KEY=x PYTHONPATH=. ./.venv/bin/python scripts/live_toolcall_check.py

Expect TOOL CALLED 0/3 under the strict closing and 3/3 under the permissive
one. A 429 means the Gemini free tier (20 requests/day/model) is spent — that
is what blocked this check when the fix was written, not a problem with it.
"""
import asyncio
import sys

import db
import gemini
import guard
import persona
import recall
import search

N = 3
QUERY = "whats the weather in tashkent rn"


async def trial(*, tools_offered: bool):
    db.init_db(":memory:")
    state = db.get_chat_state(1)
    system = persona.build_system_prompt(
        state, day_state="", memes=[], vocab=[], sticker_emoji=[], burst_target=2,
        facts=[], can_look_up=True, tools_offered=tools_offered,
    )
    user = (guard.wrap_untrusted(f"them: {QUERY}")
            + "\n\nReply as Jayden, 2 message(s), separated by |||.")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    try:
        return await gemini.complete(msgs, temperature=1.05, max_tokens=400,
                                     tools=[search.TOOL, recall.TOOL])
    except Exception as e:  # noqa: BLE001
        return f"<ERROR {type(e).__name__}: {str(e)[:110]}>"


async def main():
    if not gemini.enabled():
        print("gemini disabled / no key — cannot run")
        return 2
    for label, offered in ((f"STRICT closing ({persona.CLOSING_STRICT[:34]}…)", False),
                           ("PERMISSIVE closing (the fix)", True)):
        outs = [await trial(tools_offered=offered) for _ in range(N)]
        n_calls = sum(1 for o in outs if not isinstance(o, str))
        print(f"\n=== {label}: TOOL CALLED {n_calls}/{N} ===")
        for o in outs:
            if isinstance(o, str):
                print("   text:", o[:110].replace("\n", " "))
            else:
                print(f"   TOOL CALL -> {o.name}({o.query!r})")
    return 0


sys.exit(asyncio.run(main()))
