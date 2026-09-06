"""Siting an encounter's adds, and the four ways that goes quietly wrong.

Every test here pins a decision that, reversed, produces a full set of plausible
numbers rather than an error. That is the failure mode this whole area has: a
spawn map drawn from the attacker's coordinates looks exactly like one drawn from
the add's, and fourteen copies collapsed onto one actor id look exactly like a
boss that spawns one add.
"""

from __future__ import annotations

import math

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


# ── The wiring, which sixteen pure tests could not see ────────────────────────
#
# PR #142 shipped this module with every fold tested and the call site tested by
# nothing, and the first live run died on it: `run()` unwrapped `reportData.report`
# from a payload `client.fight_structure` had already unwrapped, so every fight was
# reported "not in the report's fights" while the cached response plainly held it.
# That is this repository's signature defect, so the fix comes with the test that
# drives the command rather than the functions under it.


class _StubClient:
    """Answers the five calls `run()` makes, and records what it was asked."""

    def __init__(self, *, rankings_rows, report, events):
        self.rankings_rows = rankings_rows
        self.report = report
        self.events = events
        self.structure_calls = []
        self.event_calls = []
        # A REAL ledger, because `fightprobe.check_budget` reads it. A `None` here
        # would make the budget guard raise instead of pass, and the test would then
        # be about the stub rather than about the command.
        from wowdps.warcraftlogs import PointLedger

        self.ledger = PointLedger()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def rate_limit(self):
        return {"limitPerHour": 18000.0, "pointsSpentThisHour": 0.0}

    def query(self, document, variables, label=None):
        return {"worldData": {"encounter": {"characterRankings": {"rankings": self.rankings_rows}}}}

    def encounter_name(self, encounter_id):
        return "The Twin Fangs"

    def fight_structure(self, code, encounter_id, difficulty):
        # The real client returns the REPORT. A stub that returned the envelope
        # would make this test pass against the broken code, which is the whole
        # point of writing it against the client's actual contract.
        self.structure_calls.append((code, encounter_id, difficulty))
        return self.report

    def fight_events(self, code, fight_id, data_type, hostility, start, end, **kw):
        self.event_calls.append((code, fight_id, data_type, kw.get("include_resources")))
        return list(self.events), False


def _args(tmp_path, **over):
    import argparse

    base = dict(
        encounter=3421,
        npc=NPC,
        difficulty=5,
        reports=1,
        report=None,
        streams=["DamageTaken"],
        max_pages=2,
        events_limit=10000,
        point_ceiling=0.9,
        cache=None,
        out=str(tmp_path / "spawn.json"),
    )
    base.update(over)
    return argparse.Namespace(**base)


def _wire(monkeypatch, client):
    from wowdps import warcraftlogs

    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", staticmethod(lambda: object()))
    monkeypatch.setattr(warcraftlogs, "WarcraftLogsClient", lambda *a, **k: client)


def test_the_command_reads_the_fight_the_ranking_named(monkeypatch, tmp_path, capsys):
    """The regression: a fight present in the payload must not read as absent."""
    import json as _json

    client = _StubClient(
        rankings_rows=[{"report": {"code": "abc", "fightID": 22}}],
        report={
            "masterData": {"actors": [{"id": 11, "gameID": NPC, "name": "Broodling"}]},
            "fights": [
                {
                    "id": 22,
                    "encounterID": 3421,
                    "difficulty": 5,
                    "kill": True,
                    "size": 20,
                    "startTime": 0,
                    "endTime": 400_000,
                    "enemyNPCs": [{"id": 11, "gameID": NPC, "instanceCount": 14, "groupCount": 2}],
                }
            ],
        },
        events=[
            flat(timestamp=1000 + i, targetInstance=i, x=float(i), y=float(i)) for i in range(1, 4)
        ],
    )
    _wire(monkeypatch, client)
    assert addspawns.run(_args(tmp_path)) == 0

    printed = capsys.readouterr().out
    assert "no fight 22" not in printed
    assert "report abc fight 22" in printed
    assert client.event_calls == [("abc", 22, "DamageTaken", True)]

    document = _json.loads((tmp_path / "spawn.json").read_text())
    assert document["fights"][0]["fightId"] == 22
    assert len(document["fights"][0]["sightings"]) == 3


def test_a_fight_the_report_really_lacks_is_still_reported_as_missing(
    monkeypatch, tmp_path, capsys
):
    """The guard has to keep working: a ranking that names a fight the report does not
    hold is a real finding, and the fix must not turn it into a silent skip."""
    client = _StubClient(
        rankings_rows=[{"report": {"code": "abc", "fightID": 99}}],
        report={"masterData": {"actors": []}, "fights": [{"id": 22, "startTime": 0, "endTime": 1}]},
        events=[],
    )
    _wire(monkeypatch, client)
    assert addspawns.run(_args(tmp_path)) == 0
    assert "no fight 99" in capsys.readouterr().out
    assert client.event_calls == []


# ── The pattern layer: waves, positions, and the two thresholds ───────────────
#
# Every threshold here is derived from a hole in the data rather than typed, so the
# tests that matter are the ones pinning what happens when the hole is not there and
# what happens when a smaller, spurious hole outranks the real one. Both were
# measured against the ten live Mythic kills of The Twin Fangs on 2026-09-06 before
# they were written down.


def sight(x, y, t, instance, actor=11):
    """One copy of the NPC, sited at (x, y) at `t` milliseconds into the fight."""
    return addspawns.SpawnSighting(
        game_id=NPC,
        actor_id=actor,
        instance=instance,
        instance_group=None,
        x=float(x),
        y=float(y),
        timestamp_ms=float(t),
        delay_ms=float(t),
        event_type="damage",
        whose=addspawns.END_TARGET,
        shape="flat",
    )


#: Ten positions a thousand units apart, which is the shape the live sample has:
#: within one position the sightings scatter by 26-187 and the nearest two positions
#: are 620-771 apart.
SPOTS = [(1000 * i, 500 * (i % 3)) for i in range(1, 11)] + [
    (1000 * i, 40_000 + 500 * (i % 3)) for i in range(1, 11)
]


def wave_of(order, *, at, first_instance=1, jitter=4):
    """Sightings for one wave, `order` naming the position each copy took."""
    # The scatter follows the copy's place in the KILL, not in its wave, so two
    # waves of the same order do not land on identical coordinates -- which would
    # put fifty zero-distance pairs at the head of the sorted list and hide exactly
    # the rank-1 artefact the floor exists for.
    return [
        sight(
            SPOTS[position - 1][0] + ((first_instance + index) % 3) * jitter,
            SPOTS[position - 1][1] + ((first_instance + index) % 2) * jitter,
            at + index * 500,
            instance=first_instance + index,
        )
        for index, position in enumerate(order)
    ]


#: The waves a single kill of The Twin Fangs actually holds, in miniature: the live
#: sample runs four to six of them, and one is not enough. `cluster_positions` needs
#: half its pairs to be within a position (see `find_break`), and fourteen copies over
#: ten positions supply four pairs of ninety-one -- so a fixture of one wave is asking
#: the function for an answer the data cannot support, not testing it.
FILLER = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 2, 4, 6, 8]


def kill_of(*waves, every=120_000, at=20_000, jitter=4):
    """Sightings for a whole kill, one list of positions per wave."""
    out = []
    for number, order in enumerate(waves):
        out.extend(
            wave_of(order, at=at + number * every, first_instance=len(out) + 1, jitter=jitter)
        )
    return out


def events_for(sightings):
    """The `DamageTaken` events those sightings would have been read out of."""
    return [
        flat(
            timestamp=int(s.timestamp_ms),
            targetInstance=s.instance,
            x=s.x,
            y=s.y,
        )
        for s in sightings
    ]


def test_a_rank_one_gap_does_not_become_the_split():
    """The defect this floor exists for, from the live sample rather than invented.

    On kill `w4dtPVTfJH7jzXnL` two sightings landed 2 units apart, so the sorted
    pairwise distances opened with `2 -> 10.4`, a ratio of 5.2 -- beating the real
    hole at `166 -> 701` (4.2). Taken, it split 69 sightings into 68 "positions" and
    reported every wave as fourteen distinct spots: a full set of plausible numbers
    answering the question being asked.
    """
    # Dense between 20 and 170, which is what a real position's scatter looks like:
    # the only two holes are the spurious pair at the head and the real one at 170.
    values = [2.0, 10.4] + [float(v) for v in range(20, 175, 5)] + [700.0, 720.0]
    unsupported = addspawns.find_break(values, min_support=1)
    assert (unsupported.below, unsupported.above) == (2.0, 10.4)

    supported = addspawns.find_break(values, min_support=len(values) // 2)
    assert (supported.below, supported.above) == (170.0, 700.0)
    assert supported.count == len(values) - 2
    # Geometrically halfway, so one sighting shifting either edge does not move the
    # answer the way an arithmetic midpoint would.
    assert 170.0 < supported.threshold < 700.0


def test_two_sightings_two_units_apart_do_not_become_the_position_grid():
    """The same defect one layer up, where it actually shipped a wrong answer.

    `test_a_rank_one_gap_does_not_become_the_split` pins `find_break`, and reverting
    `cluster_positions`' floor leaves it green -- the floor is only reachable through
    a sample that HAS such a pair. This is that sample: a live-shaped kill (positions
    a thousand apart, copies of one position scattered by 60-170) with one pair
    nudged to two units, which is what `w4dtPVTfJH7jzXnL` contained.

    Unfloored, the sorted pairwise distances open `2 -> 60`, ratio 30, and the whole
    grid collapses into one cluster per sighting.
    """
    sightings = kill_of(FILLER, FILLER, FILLER, jitter=60)
    # Position 1's copy in wave 2, moved two units from its copy in wave 1.
    twin = sightings[0]
    sightings[14] = sight(
        twin.x + 2.0, twin.y, twin.timestamp_ms + 120_000, instance=twin.instance + 14
    )

    clusters, found, _ = addspawns.cluster_positions(sightings)
    assert len(clusters) == 10
    assert found.below > 100

    unfloored = addspawns.find_break(
        [
            math.hypot(a.x - b.x, a.y - b.y)
            for i, a in enumerate(sightings)
            for b in sightings[i + 1 :]
        ],
        min_support=1,
    )
    assert unfloored.below == 2.0


def test_a_smooth_list_has_no_break_at_all():
    """`min_ratio` is what stops this inventing structure. Values that grow smoothly
    have a largest ratio near 1, and splitting them anywhere produces a boundary that
    is an artifact of the search rather than of the encounter."""
    assert addspawns.find_break([100.0, 110.0, 121.0, 133.0, 146.0, 161.0]) is None


def test_two_values_cannot_establish_a_hole():
    assert addspawns.find_break([1.0, 900.0]) is None


def test_positions_that_do_not_separate_are_reported_as_not_separating():
    """The refusal, and the reason `_report_pattern` prints it in words.

    With no distance hole every sighting becomes its own cluster, so the wave rows
    read as N distinct positions each taking one copy -- which is exactly the wrong
    answer to "do positions repeat". The calibration is the only thing that says so,
    so it must come back null rather than carrying a tuned threshold.
    """
    scattered = [sight(10 * i, 7 * i, 1000 + 400 * i, instance=i) for i in range(1, 12)]
    pattern = addspawns.describe_pattern(scattered)
    assert pattern["calibration"]["distanceBreak"] is None
    assert len(pattern["positions"]) == len(scattered)


def test_ten_distinct_then_four_repeats_reads_as_ten_before_the_first_repeat():
    """The canary for `distinctBeforeFirstRepeat`.

    `sequence.index(repeats[0])` finds where the repeated position FIRST appeared,
    not where the repeat stands -- so a wave whose first ten copies are ten different
    positions and whose eleventh repeats the third answered **2**. That reads as
    "the repeats start immediately", which is the opposite of what the sample shows
    and is the whole of question 1.
    """
    order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 5, 7, 9]
    pattern = addspawns.describe_pattern(kill_of(order, FILLER, FILLER))
    row = pattern["waves"][0]
    assert row["distinctPositions"] == 10
    assert row["distinctBeforeFirstRepeat"] == 10
    assert row["repeated"] == [3, 5, 7, 9]
    assert row["maxPerPosition"] == 2


def test_a_third_copy_at_one_position_is_reported_rather_than_capped():
    """Question 3, and the reason it is answered as a count rather than as a rule.

    Over ten live kills the tally across 53 waves is `{2: 50, 3: 3}`. A reader who
    saw only "max 2" on one kill would conclude a position CANNOT take a third; the
    field is what was seen, and it has to be able to say three.
    """
    order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 3, 5, 7]
    row = addspawns.describe_pattern(kill_of(order, FILLER, FILLER))["waves"][0]
    assert row["maxPerPosition"] == 3
    assert row["repeated"] == [3, 3, 5, 7]


def test_two_waves_separated_by_a_silence_are_two_waves():
    """A wave is a run of sightings with no long silence in it, and the silence is
    found rather than configured: within a wave the copies arrive 500 ms apart and
    the next wave is a minute later."""
    second = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 5, 7, 9]
    pattern = addspawns.describe_pattern(kill_of(FILLER, second))

    assert len(pattern["waves"]) == 2
    assert [row["sightings"] for row in pattern["waves"]] == [14, 14]
    assert [row["startSeconds"] for row in pattern["waves"]] == [20.0, 140.0]
    # Ten positions over 28 sightings: the copies really do repeat places.
    assert len(pattern["positions"]) == 10
    assert pattern["calibration"]["timeBreakMs"] is not None
    assert pattern["calibration"]["distanceBreak"] is not None
    # Single linkage chains, so how close two surviving clusters came is published
    # rather than assumed comfortable.
    assert pattern["calibration"]["closestTwoClustersCame"] > 100


def test_two_copies_ten_milliseconds_apart_do_not_become_the_wave_grid():
    """The wave half of the same floor, and it is honest about being constructed.

    The live sample has this artefact twice -- `4d7gGMRCjArnzWaT` opens its sorted
    gaps `10 -> 110` (ratio 11.0) and `GRt7aH8NKbdM3XYA` `12 -> 42` (3.5) -- and on
    both the real between-wave hole won on ratio anyway (19.4). So the floor was never
    demonstrated on real data, only on the distance axis beside it. A guard that fires
    nowhere anybody can check is a guard nobody can check, so the fixture pushes the
    artefact past the real break: two copies ten milliseconds apart against a
    three-second cadence beats a forty-eight-second silence.

    Unfloored, the split lands at 173 ms and every one of the 42 copies is its own
    "wave" -- which reads as a boss that spawns one add at a time. Floored, the
    surviving hole is the silence between waves: 5,990 ms (the stretch either side of
    the displaced copy) against 48,000.
    """
    from dataclasses import replace

    sightings = []
    for wave, start in enumerate((20_000, 107_000, 194_000)):
        for index in range(len(FILLER)):
            # The artefact: the second copy of the first wave arrives 10 ms after the
            # first, where every other pair is three seconds apart.
            offset = 10 if (wave, index) == (0, 1) else index * 3_000
            sightings.append(
                replace(
                    kill_of(FILLER, FILLER, FILLER)[wave * len(FILLER) + index],
                    timestamp_ms=float(start + offset),
                    delay_ms=float(start + offset),
                )
            )

    waves, found = addspawns.split_waves(sightings)
    assert len(waves) == 3
    assert (found.below, found.above) == (5_990.0, 48_000.0)

    gaps = [b.timestamp_ms - a.timestamp_ms for a, b in zip(sightings, sightings[1:], strict=False)]
    assert addspawns.find_break(gaps, min_support=1).above == 3_000.0


def test_waves_that_share_a_position_are_one_area():
    """The finding this exists to publish, and it needs no threshold of its own.

    Measured over ten Mythic kills of The Twin Fangs on 2026-09-06: every kill's
    positions fall into THREE sets of ten, waves 1-2 use the first set, 3-4 the
    second and 5-6 the third, and the three sets sit at fixed places in the world
    -- around (0, 67,500), (-5,500, 58,000) and (+5,400, 58,000) on all ten. So
    "ten distinct positions" is a fact about an area, not about a kill, and a kill
    read as thirty positions is the same statement.

    An area is a connected component of the wave-to-position graph, so nothing is
    configured and nothing is assumed about the encounter.
    """
    north = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 2, 4, 6, 8]
    south = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 12, 14, 16, 18]
    pattern = addspawns.describe_pattern(kill_of(north, north, south, south))

    assert [block["waves"] for block in pattern["areas"]] == [[1, 2], [3, 4]]
    assert [len(block["positions"]) for block in pattern["areas"]] == [10, 10]
    assert [row["area"] for row in pattern["waves"]] == [1, 1, 2, 2]
    # Every position carries its own area, so the two answers cannot drift.
    assert {p["area"] for p in pattern["positions"]} == {1, 2}


def test_sightings_that_never_pause_stay_one_wave():
    """One list and no break is the honest answer for adds that trickle in, rather
    than a cut placed somewhere plausible."""
    steady = wave_of([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], at=20_000)
    pattern = addspawns.describe_pattern(steady)
    assert len(pattern["waves"]) == 1
    assert pattern["calibration"]["timeBreakMs"] is None


def test_one_copy_seen_on_two_streams_is_one_copy():
    """`merge_sightings`, and the count it protects.

    A copy is the target of the damage it takes and the source of the spells it
    casts, so reading three streams produces up to three sightings of the same
    `(actor, instance)`. Concatenating them would report a wave of fourteen as a
    wave of forty-two -- against an owner statement of exactly fourteen.
    """
    early = sight(100, 100, 5_000, instance=1)
    late = sight(140, 160, 9_000, instance=1)
    other = sight(900, 900, 6_000, instance=2)

    merged = addspawns.merge_sightings([late, other], [early])
    assert len(merged) == 2
    # Earliest wins: a sighting is an upper bound on where the copy appeared, so the
    # earliest of several is the tightest bound available.
    assert merged[0].timestamp_ms == 5_000
    assert (merged[0].x, merged[0].y) == (100.0, 100.0)
    assert [s.instance for s in merged] == [1, 2]


def test_a_sample_too_small_to_show_a_pattern_reports_none(capsys):
    """Two sightings cannot establish a wave or a cluster, and an empty table beside
    a real one reads as an encounter with no pattern rather than as a sample that
    cannot show one."""
    assert addspawns._report_pattern([sight(1, 1, 10, 1), sight(2, 2, 20, 2)]) is None
    assert capsys.readouterr().out == ""


def test_the_pattern_reaches_the_payload(monkeypatch, tmp_path):
    """The wiring, because sixteen pure tests could not see the last one.

    `describe_pattern` folds sightings; nothing published them until this call site
    existed, and the analysis of ten live kills was run by hand outside the command.
    """
    import json as _json

    events = events_for(kill_of([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 5, 7, 9], FILLER, FILLER))
    client = _StubClient(
        rankings_rows=[{"report": {"code": "abc", "fightID": 22}}],
        report={
            "masterData": {"actors": [{"id": 11, "gameID": NPC, "name": "Broodling"}]},
            "fights": [
                {
                    "id": 22,
                    "encounterID": 3421,
                    "difficulty": 5,
                    "kill": True,
                    "size": 20,
                    "startTime": 0,
                    "endTime": 400_000,
                    "enemyNPCs": [{"id": 11, "gameID": NPC, "instanceCount": 14, "groupCount": 1}],
                }
            ],
        },
        events=events,
    )
    _wire(monkeypatch, client)
    assert addspawns.run(_args(tmp_path)) == 0

    pattern = _json.loads((tmp_path / "spawn.json").read_text())["fights"][0]["pattern"]
    assert len(pattern["positions"]) == 10
    assert pattern["waves"][0]["distinctBeforeFirstRepeat"] == 10
    assert pattern["waves"][0]["maxPerPosition"] == 2


def test_reading_two_streams_does_not_double_the_copies(monkeypatch, tmp_path):
    """The same fourteen copies read twice must stay fourteen. Without the merge the
    pattern is computed over a concatenation and every position takes twice as many
    copies as it did -- which is a plausible answer to question 3."""
    import json as _json

    events = events_for(kill_of([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 5, 7, 9], FILLER, FILLER))
    client = _StubClient(
        rankings_rows=[{"report": {"code": "abc", "fightID": 22}}],
        report={
            "masterData": {"actors": [{"id": 11, "gameID": NPC, "name": "Broodling"}]},
            "fights": [
                {
                    "id": 22,
                    "encounterID": 3421,
                    "difficulty": 5,
                    "kill": True,
                    "size": 20,
                    "startTime": 0,
                    "endTime": 400_000,
                    "enemyNPCs": [{"id": 11, "gameID": NPC, "instanceCount": 14, "groupCount": 1}],
                }
            ],
        },
        events=events,
    )
    _wire(monkeypatch, client)
    assert addspawns.run(_args(tmp_path, streams=["DamageTaken", "Casts"])) == 0

    fight = _json.loads((tmp_path / "spawn.json").read_text())["fights"][0]
    # Both streams are recorded, so the raw list really does hold each copy twice.
    assert len(fight["sightings"]) == 84
    # The pattern is over copies, not over rows: three waves of fourteen, not six.
    assert len(fight["pattern"]["waves"]) == 3
    assert [row["sightings"] for row in fight["pattern"]["waves"]] == [14, 14, 14]
    assert fight["pattern"]["waves"][0]["maxPerPosition"] == 2
