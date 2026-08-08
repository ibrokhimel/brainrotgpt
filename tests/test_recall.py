"""Searchable recall over every conversation the kid has ever had.

The rolling window is 40 messages. Tell the kid something on Monday and by
Friday it is off the bottom of the window and unreachable — the FTS5 index is
what makes it reachable again.

Nothing here touches the network. The migration tests write a real file rather
than `:memory:` on purpose: the case that decides whether this feature works at
all for the owner is an *existing* database being upgraded in place, and that
can only be exercised by closing a populated DB and reopening it.
"""
import sqlite3

import pytest

import db


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db(":memory:")
    yield
    db.close()


# --- the index -------------------------------------------------------------

def test_a_message_is_findable_after_it_is_stored():
    db.add_message(1, "user", "i work in IT and my boss is draining me")
    hits = db.search_messages(1, "boss")
    assert [h["text"] for h in hits] == ["i work in IT and my boss is draining me"]
    assert hits[0]["role"] == "user"
    assert isinstance(hits[0]["ts"], float)


def test_search_finds_the_kids_own_lines_too():
    db.add_message(1, "kid", "bro minecraft is so back 💀")
    assert [h["role"] for h in db.search_messages(1, "minecraft")] == ["kid"]


def test_search_does_not_leak_across_chats():
    """Two people, one bot. What one told it is not the other's to hear."""
    db.add_message(1, "user", "my dog is called biscuit")
    db.add_message(2, "user", "my dog is called rufus")

    assert [h["text"] for h in db.search_messages(1, "dog")] == ["my dog is called biscuit"]
    assert [h["text"] for h in db.search_messages(2, "dog")] == ["my dog is called rufus"]
    assert db.search_messages(3, "dog") == []


def test_search_reaches_past_the_rolling_window():
    """The whole point: message 1 of 200 is still findable."""
    db.add_message(1, "user", "i got a rabbit called kevin")
    for i in range(200):
        db.add_message(1, "user", f"filler message number {i}")

    assert [h["text"] for h in db.search_messages(1, "rabbit")] == ["i got a rabbit called kevin"]


def test_stemming_matches_a_different_form_of_the_word():
    """`porter` is why "swimming" finds "swim" — people don't re-type their
    own phrasing when they refer back to something."""
    db.add_message(1, "user", "i went swimming on tuesday")
    assert db.search_messages(1, "swim")


def test_newest_relevant_first():
    db.add_message(1, "user", "guitar lesson was fine")
    db.add_message(1, "user", "guitar lesson was awful")
    assert [h["text"] for h in db.search_messages(1, "guitar")] == [
        "guitar lesson was awful", "guitar lesson was fine",
    ]


def test_the_limit_is_respected():
    for i in range(20):
        db.add_message(1, "user", f"guitar practice {i}")
    assert len(db.search_messages(1, "guitar", limit=3)) == 3


def test_a_query_matching_nothing_returns_empty():
    db.add_message(1, "user", "hey")
    assert db.search_messages(1, "helicopter") == []


# --- keeping the index honest ---------------------------------------------

def test_deleting_a_message_removes_it_from_the_index():
    """Pruning runs as a maintenance job. An index that keeps pruned rows would
    have the kid recalling text that is no longer in the database."""
    db.add_message(1, "user", "i got a rabbit called kevin")
    for i in range(120):
        db.add_message(1, "user", f"filler message number {i}")

    assert db.prune_messages(1, keep=10) > 0
    assert db.search_messages(1, "rabbit") == []
    assert db.search_messages(1, "filler")          # what survived is still there


def test_editing_a_message_updates_the_index():
    db.add_message(1, "user", "my dog is called biscuit")
    with db._lock:
        db._db().execute("UPDATE messages SET text='my cat is called biscuit'")
        db._db().commit()

    assert db.search_messages(1, "dog") == []
    assert [h["text"] for h in db.search_messages(1, "cat")] == ["my cat is called biscuit"]


# --- untrusted queries -----------------------------------------------------
#
# The query text is written by the model, which is in turn steered by whatever
# the person typed. FTS5 raises a bare OperationalError on unbalanced quotes and
# on dangling operators, and a raise here would take out the whole reply.

@pytest.mark.parametrize("query", [
    'unbalanced "quote',
    "AND",
    "OR OR OR",
    "NOT",
    "dog AND",
    "dog OR OR cat",
    "* NEAR",
    "(unclosed",
    "col:umn",
    "^",
    "",
    "   ",
    '"""',
    "-",
    "dog*(",
    "{brace}",
])
def test_a_malformed_query_never_raises(query):
    """Raw, every one of these is an FTS5 syntax error, and an OperationalError
    here would take out the whole reply rather than just the recall."""
    db.add_message(1, "user", "my dog is called biscuit")
    assert isinstance(db.search_messages(1, query), list)


def test_operators_are_neutralised_rather_than_obeyed():
    """"dog AND" is a syntax error raw. Sanitised it is a search for the words
    "dog" and "and" — so it finds the dog, and the operator did nothing."""
    db.add_message(1, "user", "my dog is called biscuit")
    assert [h["text"] for h in db.search_messages(1, "dog AND")] == [
        "my dog is called biscuit"]
    assert db.search_messages(1, "AND") == []      # the operator alone matches no text


def test_a_query_with_punctuation_still_finds_the_words():
    """Sanitising must not be so blunt that ordinary phrasing stops matching."""
    db.add_message(1, "user", "my dog is called biscuit")
    assert db.search_messages(1, "what's their dog's name?")


def test_a_query_that_is_only_symbols_is_harmless():
    db.add_message(1, "user", "my dog is called biscuit")
    assert db.search_messages(1, "!!! ??? ...") == []


# --- migrating a live database --------------------------------------------

def _populate(path: str) -> None:
    """A database as it exists in production today: messages, no FTS index."""
    db.close()
    db.init_db(path)
    db.add_message(1, "user", "i work in IT and my boss is draining me")
    db.add_message(1, "kid", "bro thats rough")
    db.add_message(2, "user", "i got a rabbit called kevin")
    db.close()


def test_a_populated_database_is_backfilled_on_migration(tmp_path):
    """The test that decides whether this feature works at all for the owner.

    Creating the FTS table on an existing database gives an EMPTY index —
    `content=` external-content tables are not retroactive, and the triggers
    only fire on rows written from then on. Everything said before the upgrade
    would be invisible to search, which is precisely the history the owner
    wants back.
    """
    path = str(tmp_path / "live.db")
    _populate(path)

    # Simulate the pre-migration state: drop the index the fixture just built.
    conn = sqlite3.connect(path)
    conn.executescript(
        "DROP TRIGGER IF EXISTS messages_fts_ai;"
        "DROP TRIGGER IF EXISTS messages_fts_ad;"
        "DROP TRIGGER IF EXISTS messages_fts_au;"
        "DROP TABLE IF EXISTS messages_fts;"
    )
    conn.commit()
    conn.close()

    db.init_db(path)                       # the upgrade
    assert [h["text"] for h in db.search_messages(1, "boss")] == [
        "i work in IT and my boss is draining me"]
    assert db.search_messages(2, "rabbit")
    assert db.search_messages(1, "rabbit") == []      # still scoped


def test_migration_does_not_double_index_on_every_restart(tmp_path):
    """init_db runs on every boot. A backfill that re-ran would return each old
    message once per restart the bot has ever had."""
    path = str(tmp_path / "live.db")
    _populate(path)

    for _ in range(3):
        db.init_db(path)
        db.close()

    db.init_db(path)
    assert len(db.search_messages(1, "boss")) == 1


def test_writes_still_work_after_migrating_a_populated_database(tmp_path):
    path = str(tmp_path / "live.db")
    _populate(path)
    db.init_db(path)

    db.add_message(1, "user", "actually i quit the IT job")
    assert len(db.search_messages(1, "IT")) == 2


def test_recall_can_be_switched_off(tmp_path, monkeypatch):
    """A kill switch that needs no redeploy, same as WEB_SEARCH_ENABLED."""
    import config
    db.add_message(1, "user", "my dog is called biscuit")
    monkeypatch.setattr(config, "RECALL_ENABLED", False)
    assert db.search_messages(1, "dog") == []
