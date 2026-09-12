"""The key space: one entry per thing, rather than one per command.

Every test here pins a rule the module makes rather than an implementation detail,
because the whole value of #170 Schritt 2 is that the rules are checkable: a key
that names the thing, a variant that says what an entry carries, a budget that says
how much was read, and a lifetime that says how long that may be believed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from wowdps import wclstore
from wowdps.wclstore import IMMUTABLE, Key, WclStore

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def store(tmp_path) -> WclStore:
    return WclStore(tmp_path)


def test_a_round_trip_returns_exactly_what_was_stored(tmp_path):
    s = store(tmp_path)
    s.put(Key(("report", "abc", "kills")), {"fights": [1, 2]}, ttl=IMMUTABLE, now=NOW)
    assert s.get(Key(("report", "abc", "kills")), now=NOW) == {"fights": [1, 2]}


def test_the_key_is_the_thing_and_not_the_document():
    """The claim the whole module rests on.

    Two documents asking the same question of the same encounter are ONE entry.
    Under `sha256(document + variables)` they were two, and a document whose text
    was merely reformatted was a cold cache.
    """
    lean = wclstore.encounter_key(3421, variant=frozenset({"name"}))
    rich = wclstore.encounter_key(3421, variant=frozenset({"name", "zone"}))
    assert lean.path() == rich.path()


# --------------------------------------------------------------------------------
# The variant: a lean document answered out of a rich one
# --------------------------------------------------------------------------------


def test_a_rich_entry_answers_a_lean_request(tmp_path):
    """`ENCOUNTER_NAME_QUERY` needs the name; `ENCOUNTER_ZONE_QUERY` writes it too."""
    s = store(tmp_path)
    s.put(
        wclstore.encounter_key(3421, variant=frozenset({"name", "zone"})),
        {"worldData": {"encounter": {"name": "The Twin Fangs", "zone": {"id": 53}}}},
        ttl=wclstore.FROZEN_TTL,
        now=NOW,
    )
    answer = s.get(wclstore.encounter_key(3421, variant=frozenset({"name"})), now=NOW)
    assert answer is not None
    assert answer["worldData"]["encounter"]["name"] == "The Twin Fangs"


def test_a_lean_entry_does_not_answer_a_rich_request(tmp_path):
    """The half that makes the other half safe.

    A name-only payload has no zone, and serving it for a zone request would make
    "the API sent no zone" and "this document never asked for one" the same answer
    -- the failure this project has paid for four times.
    """
    s = store(tmp_path)
    s.put(
        wclstore.encounter_key(3421, variant=frozenset({"name"})),
        {"worldData": {"encounter": {"name": "The Twin Fangs"}}},
        ttl=wclstore.LIVE_TTL,
        now=NOW,
    )
    assert s.get(wclstore.encounter_key(3421, variant=frozenset({"name", "zone"})), now=NOW) is None
    assert s.misses["encounter:variant"] == 1


# --------------------------------------------------------------------------------
# The budget: a prefix that looks like a complete answer
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "asked", "hit"),
    [
        (100, 100, True),
        (100, 50, True),
        (50, 100, False),
        (None, 100, False),  # unknown is not enough
        (None, None, True),  # neither states one, so there is nothing to compare
        (100, None, True),
    ],
)
def test_the_budget_decides_whether_a_prefix_may_answer(tmp_path, stored, asked, hit):
    s = store(tmp_path)
    s.put(Key(("zone", "53", "w1-2", "p1"), budget=stored), {"ok": 1}, ttl=IMMUTABLE, now=NOW)
    got = s.get(Key(("zone", "53", "w1-2", "p1"), budget=asked), now=NOW)
    assert (got is not None) is hit


# --------------------------------------------------------------------------------
# The lifetime
# --------------------------------------------------------------------------------


def test_an_immutable_entry_never_expires(tmp_path):
    s = store(tmp_path)
    s.put(Key(("report", "abc", "kills")), {"ok": 1}, ttl=IMMUTABLE, now=NOW)
    assert s.get(Key(("report", "abc", "kills")), now=NOW + timedelta(days=3650)) is not None


def test_a_dated_entry_expires_and_says_so(tmp_path):
    s = store(tmp_path)
    key = wclstore.encounter_key(3421, variant=frozenset({"name"}))
    s.put(key, {"ok": 1}, ttl=wclstore.LIVE_TTL, now=NOW)
    assert s.get(key, now=NOW + timedelta(hours=5)) is not None
    assert s.get(key, now=NOW + timedelta(hours=7)) is None
    assert s.misses["encounter:expired"] == 1


@pytest.mark.parametrize(
    ("frozen", "expected"),
    [(True, wclstore.FROZEN_TTL), (False, wclstore.LIVE_TTL), (None, wclstore.LIVE_TTL)],
)
def test_unknown_is_not_frozen(frozen, expected):
    """A zone that did not say gets the SHORT lifetime.

    Being wrong that way costs one re-fetch. Being wrong the other way is the error
    this project has made twice -- reading a whole season under the previous
    season's encounter ids, because a frozen answer kept saying live.
    """
    assert wclstore.encounter_ttl(frozen) == expected


# --------------------------------------------------------------------------------
# Writes that would lose something
# --------------------------------------------------------------------------------


def test_a_narrower_write_keeps_the_wider_entry_when_the_thing_cannot_change(tmp_path):
    """One pull's talent codes, asked for twelve actors once and two later.

    On an immutable key the older answer is as good as the newer, so throwing away
    ten paid-for codes to store a fresher copy of two of them is a pure loss.
    """
    s = store(tmp_path)
    key = wclstore.report_key
    wide = key("abc", "f", 1, "talents", variant=frozenset({"a1", "a2", "a3"}))
    s.put(wide, {"all": 3}, ttl=IMMUTABLE, now=NOW)
    s.put(key("abc", "f", 1, "talents", variant=frozenset({"a1"})), {"one": 1}, ttl=IMMUTABLE)
    assert s.get(wide, now=NOW) == {"all": 3}


def test_a_narrower_write_replaces_a_mutable_entry(tmp_path):
    """The opposite call, and it is the opposite for a reason.

    An entry may never claim to carry a field its payload does not have. On a key
    whose VALUE is the thing in question, the fresher narrower answer wins and the
    wider request pays again -- which is the right direction to be wrong in.
    """
    s = store(tmp_path)
    s.put(
        wclstore.encounter_key(3421, variant=frozenset({"name", "zone"})),
        {"old": True},
        ttl=wclstore.FROZEN_TTL,
        now=NOW,
    )
    s.put(
        wclstore.encounter_key(3421, variant=frozenset({"name"})),
        {"new": True},
        ttl=wclstore.LIVE_TTL,
        now=NOW,
    )
    assert s.get(wclstore.encounter_key(3421, variant=frozenset({"name"})), now=NOW) == {
        "new": True
    }
    assert s.get(wclstore.encounter_key(3421, variant=frozenset({"name", "zone"})), now=NOW) is None


# --------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------


def test_an_entry_from_another_schema_is_a_miss_rather_than_a_crash(tmp_path):
    s = store(tmp_path)
    key = Key(("report", "abc", "kills"))
    s.put(key, {"ok": 1}, ttl=IMMUTABLE, now=NOW)
    path = tmp_path / key.path()
    payload = json.loads(path.read_text())
    payload["schemaVersion"] = wclstore.SCHEMA_VERSION + 1
    path.write_text(json.dumps(payload))
    assert s.get(key, now=NOW) is None
    assert s.misses["report:unreadable"] == 1


def test_a_truncated_file_is_a_miss_rather_than_a_crash(tmp_path):
    s = store(tmp_path)
    key = Key(("report", "abc", "kills"))
    s.put(key, {"ok": 1}, ttl=IMMUTABLE, now=NOW)
    (tmp_path / key.path()).write_text('{"schemaVersion": 1, "data": {"ok"')
    assert s.get(key, now=NOW) is None


def test_a_surprising_key_part_cannot_escape_the_directory(tmp_path):
    """A report code is alphanumeric, so nothing is rewritten in practice.

    The stand-in exists for the input nobody expects, and it hashes the ORIGINAL:
    two different surprising parts must not become one file.
    """
    a = Key(("report", "../../etc/passwd")).path()
    b = Key(("report", "../../etc/shadow")).path()
    assert ".." not in str(a) and ".." not in str(b)
    assert a != b


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        Key(())


def test_describe_names_what_was_served_and_what_was_not(tmp_path):
    s = store(tmp_path)
    s.put(Key(("report", "abc", "kills")), {"ok": 1}, ttl=IMMUTABLE, now=NOW)
    s.get(Key(("report", "abc", "kills")), now=NOW)
    s.get(Key(("report", "zzz", "kills")), now=NOW)
    line = s.describe()
    assert "1 hit(s)" in line and "report:absent 1" in line
