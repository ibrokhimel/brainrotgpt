import pytest

import share_card


@pytest.mark.skipif(not share_card.available(), reason="Pillow not installed")
def test_render_returns_png():
    png = share_card.render("bro the situation is cooked 😭🙏", "🏋️ Gym Sigma")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic header
    assert len(png) > 1000


def test_emoji_stripped():
    cleaned = share_card._EMOJI.sub("", "hi 😭🙏🗿 there")
    assert "😭" not in cleaned and "hi" in cleaned and "there" in cleaned
