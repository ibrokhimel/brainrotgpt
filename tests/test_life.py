import asyncio
import datetime as dt

import db
import life


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "l.db"))


def _run(coro):
    return asyncio.run(coro)


def _at(day, hour):
    return dt.datetime(2026, 8, day, hour).timestamp()


def test_current_is_empty_before_any_refresh(tmp_path):
    _fresh(tmp_path)
    assert life.current() == ""


def test_refresh_stores_state_for_today(tmp_path, monkeypatch):
    _fresh(tmp_path)

    async def fake(prompt):
        return "mom took my phone til friday"

    monkeypatch.setattr(life, "_ask", fake)
    assert _run(life.refresh()) == "mom took my phone til friday"
    assert life.current() == "mom took my phone til friday"


def test_refresh_falls_back_to_yesterdays_state_on_failure(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.set_kid_state("day_state", "got a new game")
    db.set_kid_state("day_date", "2026-08-06")

    async def boom(prompt):
        raise RuntimeError("groq down")

    monkeypatch.setattr(life, "_ask", boom)
    assert _run(life.refresh()) == "got a new game"


def test_school_block_is_weekday_daytime_only():
    # 2026-08-10 is a Monday, 2026-08-08 is a Saturday
    assert life.in_school_block(_at(10, 10))
    assert not life.in_school_block(_at(10, 19))
    assert not life.in_school_block(_at(8, 10))
