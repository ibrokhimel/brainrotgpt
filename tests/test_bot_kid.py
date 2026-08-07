import bot
import db


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "bk.db"))


def test_bond_increases_per_message(tmp_path):
    _fresh(tmp_path)
    state = db.get_chat_state(1)
    assert bot.apply_bond(state, "hi") == bot.BOND_PER_MESSAGE


def test_long_messages_are_worth_more(tmp_path):
    _fresh(tmp_path)
    state = db.get_chat_state(1)
    assert bot.apply_bond(state, "x" * 250) == bot.BOND_LONG_MESSAGE


def test_bond_is_clamped(tmp_path):
    _fresh(tmp_path)
    state = dict(db.get_chat_state(1), bond=100)
    assert bot.apply_bond(state, "hi") == 100
    state = dict(db.get_chat_state(1), bond=-100)
    assert bot.apply_bond(state, "hi") > -100      # positive input still helps


def test_low_content_detection():
    assert bot.is_low_content("lol")
    assert bot.is_low_content("💀")
    assert bot.is_low_content("ok")
    assert not bot.is_low_content("what do you think about this")


def test_pings_today_resets_on_a_new_day(tmp_path):
    _fresh(tmp_path)
    db.update_chat_state(1, pings_today=3, pings_day="2026-08-06")
    assert bot.pings_remaining(db.get_chat_state(1), "2026-08-07") > 0


def test_pings_remaining_is_zero_at_the_cap(tmp_path):
    _fresh(tmp_path)
    import config
    db.update_chat_state(1, pings_today=config.MAX_PINGS_PER_DAY, pings_day="2026-08-07")
    assert bot.pings_remaining(db.get_chat_state(1), "2026-08-07") == 0
