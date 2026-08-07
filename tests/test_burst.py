import burst


def test_splits_on_delimiter():
    pieces = burst.parse("yo ||| wait ||| thats crazy")
    assert [p.value for p in pieces] == ["yo", "wait", "thats crazy"]
    assert all(p.kind == "text" for p in pieces)


def test_drops_empty_segments():
    assert [p.value for p in burst.parse("yo ||| ||| ok")] == ["yo", "ok"]


def test_caps_message_count():
    pieces = burst.parse(" ||| ".join(f"m{i}" for i in range(12)), max_msgs=5)
    assert len(pieces) == 5


def test_extracts_sticker_elements():
    pieces = burst.parse("lmao ||| [sticker:💀] ||| fr")
    assert [(p.kind, p.value) for p in pieces] == [
        ("text", "lmao"), ("sticker", "💀"), ("text", "fr")
    ]


def test_sticker_only_reply():
    pieces = burst.parse("[sticker:🗿]")
    assert [(p.kind, p.value) for p in pieces] == [("sticker", "🗿")]


def test_fallback_splits_sentences_when_no_delimiter():
    raw = "bro what. that is insane. i cannot believe it"
    pieces = burst.parse(raw)
    assert len(pieces) == 3
    assert pieces[0].value == "bro what"


def test_fallback_splits_on_newlines():
    pieces = burst.parse("yo\nwsp\nu up")
    assert [p.value for p in pieces] == ["yo", "wsp", "u up"]


def test_long_single_message_is_hard_split():
    raw = "a" * 400
    pieces = burst.parse(raw, max_chars=180)
    assert len(pieces) >= 3
    assert all(len(p.value) <= 180 for p in pieces)


def test_empty_input_yields_nothing():
    assert burst.parse("") == []
    assert burst.parse("   \n  ") == []


def test_strips_quotes_and_model_preamble_artifacts():
    assert burst.parse('"yo" ||| "wsp"')[0].value == "yo"
