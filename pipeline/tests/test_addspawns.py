"""Siting an encounter's adds, and the four ways that goes quietly wrong.

Every test here pins a decision that, reversed, produces a full set of plausible
numbers rather than an error. That is the failure mode this whole area has: a
spawn map drawn from the attacker's coordinates looks exactly like one drawn from
the add's, and fourteen copies collapsed onto one actor id look exactly like a
boss that spawns one add.
"""

from __future__ import annotations

from wowdps import addspawns

NPC = 270898
#: report-local actor id -> game id, the shape `masterData.actors` provides.
ACTORS = {11: NPC, 12: NPC, 90: 1234}


def flat(**over):
    """An event in the v2 dialect: resource fields on the event, plus resourceActor."""
    row = {
        "timestamp": 1000,
        "type": "damage",
        "sourceID": 90,
        "targetID": 11,
        "targetInstance": 1,
        "x": 100.0,
        "y": 200.0,
        "facing": 3.0,
        "resourceActor": addspawns.RESOURCE_ACTOR_TARGET,
    }
    row.update(over)
    return row


def test_the_nested_dialect_is_read():
    """The Scripting API's shape: sourceResources / targetResources as objects.

    Both dialects are accepted because nobody has measured which one v2 sends for
    these event types, and reading one while calling the other absent is how a
    feature reports nothing with every number beside it looking healthy.
    """
    event = {
        "timestamp": 5,
        "targetID": 11,
        "targetResources": {"x": 7.0, "y": 8.0, "facing": 1.0},
        "sourceResources": {"x": 70.0, "y": 80.0},
    }
    target = addspawns.event_position(event, addspawns.END_TARGET)
    source = addspawns.event_position(event, addspawns.END_SOURCE)
    assert (target.x, target.y, target.shape) == (7.0, 8.0, "nested")
    assert (source.x, source.y, source.shape) == (70.0, 80.0, "nested")


def test_a_flat_block_answers_only_for_the_end_resource_actor_names():
    """THE test of this module, and the one whose reversal is undetectable.

    A flat event carries ONE resource block and `resourceActor` says whose it is.
    On a damage event the source is whoever swung; returning that block for the
    target would put a player's feet on the map and label them an add. Ten such
    readings still cluster -- players stand near what they hit -- so the picture
    would look like a spawn map and be a heat map of melee positions.
    """
    event = flat(resourceActor=addspawns.RESOURCE_ACTOR_TARGET)
    assert addspawns.event_position(event, addspawns.END_TARGET) is not None
    assert addspawns.event_position(event, addspawns.END_SOURCE) is None

    event = flat(resourceActor=addspawns.RESOURCE_ACTOR_SOURCE)
    assert addspawns.event_position(event, addspawns.END_SOURCE) is not None
    assert addspawns.event_position(event, addspawns.END_TARGET) is None


def test_a_flat_block_with_no_discriminator_is_refused_rather_than_guessed():
    """Unknown is not "the end I happen to want".

    If `resourceActor` is ever renamed or omitted, this returns nothing and the
    probe reports zero positioned events -- loud. Guessing an end instead would
    return coordinates that are right half the time, which is worse than none.
    """
    event = flat()
    del event["resourceActor"]
    assert addspawns.event_position(event, addspawns.END_TARGET) is None
    assert addspawns.event_position(event, addspawns.END_SOURCE) is None


def test_zero_is_a_coordinate():
    """`if not x` would drop the origin, which is a place an add can stand.

    Nothing about (0, 0) makes it less real than (100, 200), and a hole at exactly
    one map position is the kind of gap that gets explained as "no add spawns
    there" rather than as a bug.
    """
    position = addspawns.event_position(flat(x=0.0, y=0.0), addspawns.END_TARGET)
    assert position is not None
    assert (position.x, position.y) == (0.0, 0.0)


def test_a_bool_is_not_a_coordinate():
    """`isinstance(True, int)` is True in Python, so a flag would read as x=1."""
    assert addspawns.event_position(flat(x=True, y=False), addspawns.END_TARGET) is None


def test_fourteen_copies_of_one_actor_id_are_fourteen_sightings():
    """The rule CLAUDE.md states repo-wide, applied to position.

    Copies of one add share a single report-local actor id and differ only in
    `targetInstance`. Keyed on the id alone this returns ONE sighting, which for a
    spawn analysis is the whole answer destroyed -- fourteen spawn points reported
    as one.
    """
    events = [
        flat(timestamp=1000 + i, targetInstance=i, x=float(i), y=float(i)) for i in range(1, 15)
    ]
    sightings = addspawns.spawn_sightings(events, ACTORS, NPC, fight_start_ms=1000)
    assert len(sightings) == 14
    assert {s.instance for s in sightings} == set(range(1, 15))


def test_the_earliest_positioned_event_per_copy_wins():
    """A copy is sited where it was FIRST seen, never where it ended up.

    Adds move. Taking any later event turns a spawn map into a map of where the
    raid dragged things, and the two are not distinguishable after the fact.
    """
    events = [
        flat(timestamp=9000, targetInstance=1, x=999.0, y=999.0),
        flat(timestamp=1500, targetInstance=1, x=10.0, y=20.0),
        flat(timestamp=4000, targetInstance=1, x=500.0, y=500.0),
    ]
    (sighting,) = addspawns.spawn_sightings(events, ACTORS, NPC, fight_start_ms=1000)
    assert (sighting.x, sighting.y) == (10.0, 20.0)
    assert sighting.delay_ms == 500


def test_an_actor_the_map_does_not_know_is_skipped_not_guessed():
    """An unknown actor is exactly where a guess would attribute another NPC's
    position to this one, so it is dropped."""
    events = [flat(targetID=77, targetInstance=1)]
    assert addspawns.spawn_sightings(events, ACTORS, NPC, fight_start_ms=0) == []


def test_both_ends_are_considered_so_a_cast_stream_sites_the_caster():
    """An add is the TARGET of damage and the SOURCE of its own casts.

    Which end a stream carries is a property of the stream. Reading only targets
    would return nothing at all from a `Casts` fetch and report it as "no
    positions", which reads as the API not carrying them.
    """
    cast = {
        "timestamp": 2000,
        "type": "cast",
        "sourceID": 12,
        "sourceInstance": 3,
        "x": 42.0,
        "y": 43.0,
        "resourceActor": addspawns.RESOURCE_ACTOR_SOURCE,
    }
    (sighting,) = addspawns.spawn_sightings([cast], ACTORS, NPC, fight_start_ms=0)
    assert (sighting.x, sighting.y, sighting.whose) == (42.0, 43.0, "source")
    assert sighting.instance == 3


def test_the_diagnostic_separates_positioned_from_positioned_for_the_npc():
    """The state that would otherwise be read as success.

    A damage stream whose every resource block belongs to the ATTACKER has a
    position on every event and none of them is an add's. `positioned == events`
    with `npc_positioned == 0` is what says so, and collapsing the two counters
    would print a healthy-looking line over an empty map.
    """
    events = [
        flat(timestamp=1000 + i, targetInstance=i, resourceActor=addspawns.RESOURCE_ACTOR_SOURCE)
        for i in range(3)
    ]
    report = addspawns.describe_event_shapes(events, "DamageTaken", ACTORS, NPC)
    assert report.events == 3
    assert report.positioned == 3
    assert report.npc_events == 3
    assert report.npc_positioned == 0


def test_the_diagnostic_counts_one_positioned_event_once():
    """An event with both ends positioned is one positioned event, not two."""
    event = {
        "timestamp": 1,
        "targetID": 11,
        "targetResources": {"x": 1.0, "y": 2.0},
        "sourceResources": {"x": 3.0, "y": 4.0},
    }
    report = addspawns.describe_event_shapes([event], "DamageTaken", ACTORS, NPC)
    assert report.events == 1
    assert report.positioned == 1
    assert report.npc_positioned == 1


def test_no_positions_at_all_is_visible_rather_than_empty():
    """`includeResources` not taking effect must not look like an encounter with
    no adds. The counters say events were read and none carried a position."""
    events = [{"timestamp": 1, "type": "damage", "targetID": 11, "targetInstance": 0}]
    report = addspawns.describe_event_shapes(events, "DamageTaken", ACTORS, NPC)
    assert report.events == 1
    assert report.positioned == 0
    assert report.npc_events == 1
    assert report.npc_positioned == 0
    assert report.x_range is None


def test_enemy_npc_counts_reads_what_the_payload_already_carried():
    """`FIGHT_STRUCTURE_QUERY` has always asked for this and nothing read it.

    It is "how many copies, in how many groups" for free -- no extra query, and
    already cached for every fight this project has probed.
    """
    fight = {
        "enemyNPCs": [
            {"id": 11, "gameID": NPC, "instanceCount": 14, "groupCount": 2},
            {"id": 90, "gameID": 1234, "instanceCount": 1, "groupCount": 1},
        ]
    }
    rows = addspawns.enemy_npc_counts(fight)
    assert rows[0] == {
        "actorId": 11,
        "gameId": NPC,
        "instanceCount": 14,
        "groupCount": 2,
    }


def test_a_fight_without_enemy_npcs_is_empty_not_an_error():
    assert addspawns.enemy_npc_counts({}) == []
    assert addspawns.enemy_npc_counts({"enemyNPCs": None}) == []


def test_actor_game_id_map_skips_rows_that_cannot_join():
    actors = [
        {"id": 11, "gameID": NPC},
        {"id": 12},
        {"gameID": 999},
        "not a dict",
    ]
    assert addspawns.actor_game_id_map(actors) == {11: NPC}


def test_sightings_are_ordered_reproducibly():
    """Two events on the same millisecond must not resolve by dict order.

    This project publishes its numbers; a pass whose answer depends on iteration
    order is not reproducible, and the difference would show up as a spawn map
    that moves between runs for no reason anybody could name.
    """
    events = [
        flat(timestamp=1000, targetID=12, targetInstance=2, x=2.0, y=2.0),
        flat(timestamp=1000, targetID=11, targetInstance=1, x=1.0, y=1.0),
    ]
    first = addspawns.spawn_sightings(events, ACTORS, NPC, fight_start_ms=0)
    second = addspawns.spawn_sightings(list(reversed(events)), ACTORS, NPC, fight_start_ms=0)
    assert [(s.actor_id, s.instance) for s in first] == [(s.actor_id, s.instance) for s in second]


def test_a_cache_directory_given_as_a_string_still_builds_a_path():
    """`argparse` hands `--cache` back as a `str`, and the annotation does not stop it.

    Measured on 2026-09-06 (run 34030450826): the spawn probe sent three paid queries
    and then died on `TypeError: unsupported operand type(s) for /: 'str' and 'str'`
    inside `_cache_path`. Every call site satisfies `cache_dir: Path | None` with a
    string, so the promise was decorative until the first cache write -- which is deep
    inside a live run rather than at construction.
    """
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    client = WarcraftLogsClient(Credentials("id", "secret"), cache_dir="some/dir")
    try:
        path = client._cache_path("query {}", {"a": 1})
        assert path is not None
        assert path.parent.as_posix() == "some/dir"
        assert path.suffix == ".json"
        # Absent stays absent: a falsy directory must not become `Path('.')`, which
        # would silently start caching into the working directory.
        assert WarcraftLogsClient(Credentials("id", "secret"))._cache_path("q", {}) is None
    finally:
        client.close()
