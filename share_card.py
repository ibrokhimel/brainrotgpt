"""Render a reply into a shareable, watermarked PNG (Pillow).

Note: base Pillow fonts can't draw color emoji, so emoji are stripped from the
card for legibility — the text reads clean and carries a small BrainrotGPT tag.
"""
import io
import re

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:  # noqa: BLE001 — Pillow optional
    _PIL_OK = False

# Strip emoji / pictographs / symbols / variation selectors for the card text.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # symbols & pictographs, emoji, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002190-\U000021FF"   # arrows
    "\U00002B00-\U00002BFF"   # misc symbols & arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "]+"
)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "arial.ttf",
]
_FONT_BOLD = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "arialbd.ttf",
]


class ShareCardError(Exception):
    pass


def available() -> bool:
    return _PIL_OK


def _font(size: int, bold: bool = False):
    for path in (_FONT_BOLD if bold else _FONT_CANDIDATES):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        line = ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=font) <= max_w:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
    return lines


def _gradient(w: int, h: int, top: tuple, bottom: tuple):
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def render(text: str, persona_label: str = "BrainrotGPT") -> bytes:
    """Return PNG bytes for a share card, or raise ShareCardError."""
    if not _PIL_OK:
        raise ShareCardError("Pillow not installed")

    clean = _EMOJI.sub("", text)
    clean = re.sub(r"[ \t]+", " ", clean).strip()
    if not clean:
        clean = "(brainrot redacted to pure emoji 💀)"

    width = 1080
    margin = 80
    max_text_w = width - 2 * margin
    body = _font(36)
    brand = _font(40, bold=True)
    tag = _font(28, bold=True)

    probe = Image.new("RGB", (10, 10))
    pdraw = ImageDraw.Draw(probe)
    lines = _wrap(pdraw, clean, body, max_text_w)

    line_h = body.getbbox("Ag")[3] + 14
    header_h = 130
    footer_h = 90
    height = header_h + len(lines) * line_h + footer_h + margin

    img = _gradient(width, height, (24, 16, 48), (12, 10, 30))
    draw = ImageDraw.Draw(img)

    draw.text((margin, 56), "BrainrotGPT", font=brand, fill=(180, 255, 120))
    draw.text((margin, 104), persona_label, font=tag, fill=(150, 150, 200))

    y = header_h + 20
    for line in lines:
        draw.text((margin, y), line, font=body, fill=(240, 240, 245))
        y += line_h

    footer = "🗿📈  t.me/your_bot — paste it back, win the convo"
    footer = _EMOJI.sub("", footer).strip()
    draw.text((margin, height - footer_h), footer, font=tag, fill=(120, 120, 160))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
