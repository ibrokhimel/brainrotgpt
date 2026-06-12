"""Read a forwarded screenshot of a chat into a transcript via a Groq vision model.

The most common real input is a screenshot, not pasted text. We send the image
to a multimodal Groq model and ask it to transcribe the conversation into the
same 'Sender: text' lines the generation engine already expects.
"""
import base64

from groq import AsyncGroq

import config

_client = AsyncGroq(api_key=config.GROQ_API_KEY)

VISION_PROMPT = (
    "You are reading a screenshot of a chat/DM conversation. Transcribe it as plain "
    "text, one line per message bubble, in the form 'Sender: message'. Use the visible "
    "names; if the right-aligned side has no name use 'Me' and the left side 'Them'. "
    "Preserve the order top-to-bottom. Output ONLY the transcript — no commentary, no "
    "code fences. If the image contains no readable conversation, output exactly NO_CHAT."
)


class VisionError(Exception):
    """Raised when the image can't be read into a transcript."""


async def transcribe_image(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    try:
        resp = await _client.chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=1500,
        )
    except Exception as e:  # noqa: BLE001 — model may be unavailable on the account
        raise VisionError(str(e)[:200]) from e

    text = (resp.choices[0].message.content or "").strip()
    if not text or text.upper().startswith("NO_CHAT"):
        raise VisionError("couldn't read a conversation from that image")
    return text
