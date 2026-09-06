"""What the pooled spawn map may claim, and what it refuses to.

Every fixture here is a whole KILL rather than a lone wave, for the reason
``test_addspawns`` records: ``cluster_positions`` needs half its pairwise distances
to sit inside a position, and fourteen copies over ten positions supply four pairs of
ninety-one. A one-wave fixture asks the fold for an answer its own data cannot
support, so a test built on one pins whatever the code happens to do.
"""

from __future__ import annotations

import json

import pytest

from wowdps import spawnmap

NPC = 270898

#: Ten places a thousand apart, in three areas the fight visits in turn -- the shape
#: the live sample has, in miniature.
GRID = {
    1: [(1000 * i, 60_000 + 500 * (i % 3)) for i in range(1, 11)],
    2: [(1000 * i, 40_000 + 500 * (i % 3)) for i in range(1, 11)],
    3: [(1000 * i, 20_000 + 500 * (i % 3)) for i in range(1, 11)],
}

#: One wave: all ten places, then four of them a second time. The commonest shape in
#: the live sample and not the only one, which is why nothing here asserts it is a rule.
FULL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 2, 4, 6, 8]


def sightings_for(waves, *, area=1, jitter=4.0, at=20_000, every=120_000, seed=0):
    """Rows as the payload carries them, for `waves` lists of place numbers.

    The scatter follows the copy's place in the KILL, not in its wave: two waves of
    the same order would otherwise land on byte-identical coordinates and put fifty
    zero-distance pairs at the head of the sorted list -- hiding exactly the rank-one
    artefact ``find_break``'s floor exists for.
    """
    rows = []
    for number, order in enumerate(waves):
        for index, place in enumerate(order):
            n = len(rows) + seed
            x, y = GRID[area][place - 1]
            rows.append(
                {
                    "actorId": 11,
                    "instance": n + 1,
                    "instanceGroup": None,
                    "x": x + (n % 3) * jitter,
                    "y": y + (n % 2) * jitter,
                    "delayMs": float(at + number * every + index * 500),
                    "eventType": "damage",
                    "whose": "target",
                    "shape": "flat",
                    "dataType": "DamageTaken",
                }
            )
    return rows


def fight(
    *,
    code="AAA",
    fight_id=1,
    waves=(FULL, FULL, FULL),
    area=1,
    difficulty=5,
    started_at=1_756_000_000_000,
    truncated=False,
    seed=0,
):
    return {
        "reportCode": code,
        "fightId": fight_id,
        "difficulty": difficulty,
        "startedAt": started_at,
        "durationSeconds": 400.0,
        "raidSize": 20,
        "enemyNpcs": [{"actorId": 11, "gameId": NPC, "instanceCount": 42, "groupCount": 3}],
        "streams": [{"dataType": "DamageTaken", "truncated": truncated}],
        "sightings": sightings_for(waves, area=area, seed=seed),
    }


def payload(fights=None, **over):
    out = {
        "requestedEncounter": 53421,
        "usedEncounter": 3421,
        "npc": NPC,
        "npcName": "Broodling of Ithraz",
        "difficulty": 5,
        "fights": list(fights) if fights is not None else [fight(), fight(code="BBB", seed=7)],
    }
    out.update(over)
    return out


# --------------------------------------------------------------------------- refusals


def test_a_payload_that_does_not_state_its_difficulty_publishes_nothing():
    """The refusal that matters most, because the wrong answer looks right.

    A Heroic map under a Mythic heading is the mislabelling `fights.json`'s own
    `measuredDifficulty` exists to prevent, and a publisher that assumed Mythic would
    make it silently -- every number in the block would be real.

    **The fights state no difficulty either, and that is what makes this a canary
    rather than a coincidence.** A payload whose FIGHTS carry the field is refused by
    the foreign-difficulty check further down whatever this guard does, so a fixture
    built that way passes with this guard deleted -- measured. The case only this
    guard covers is the real stored payload from 2026-09-06: written before the probe
    recorded a difficulty anywhere, so nothing in it contradicts an assumed Mythic.
    """
    silent = payload(
        [fight(difficulty=None), fight(code="BBB", seed=7, difficulty=None)],
        difficulty=None,
    )
    result = spawnmap.build_encounter(silent)
    assert result.spots == []
    assert "states no difficulty" in (result.refusal or "")


def test_a_fight_at_another_difficulty_is_refused_rather_than_filtered():
    """The fetch is already scoped, so a foreign difficulty means it did not hold."""
    mixed = payload([fight(), fight(code="BBB", seed=7, difficulty=4)])
    result = spawnmap.build_encounter(mixed)
    assert result.spots == []
    assert "difficulty 5" in (result.refusal or "")
    assert "(4)" in (result.refusal or "")


def test_a_fight_stating_no_difficulty_is_allowed_through():
    """Unknown is not the same as wrong -- `harvest`'s and `firstkills`' rule.

    A payload written before the probe recorded the field states none, and refusing
    those would make every past artefact unpublishable.
    """
    older = payload([fight(), fight(code="BBB", seed=7, difficulty=None)])
    assert spawnmap.build_encounter(older).refusal is None


def test_one_kill_is_not_a_map():
    """One kill's clusters are already what `spawn-probe` prints.

    Re-publishing them as "the encounter's places" would promise a repetition
    nothing observed, which is the whole difference between the two folds.
    """
    result = spawnmap.build_encounter(payload([fight()]))
    assert result.spots == []
    assert "pooling needs" in (result.refusal or "")


def test_clusters_that_fall_into_no_shared_places_publish_no_spots():
    """No hole in the pooled distances -> no map, rather than one spot per sighting."""
    scattered = payload(
        [
            fight(waves=(FULL, FULL, FULL)),
            # A second kill on a grid 250 units off the first: close enough that the
            # pooled distances grade smoothly and no hole separates "same place".
            {
                **fight(code="BBB", seed=7),
                "sightings": [
                    {**row, "x": row["x"] + 250, "y": row["y"] + 250}
                    for row in sightings_for((FULL, FULL, FULL), seed=7)
                ],
            },
        ]
    )
    result = spawnmap.build_encounter(scattered)
    if result.spots:
        # The hole survived; then the claim under test is the weaker one -- that the
        # two kills did NOT collapse to ten places, which is what a real pooling of
        # two offset grids has to say.
        assert len(result.spots) >= 10
    else:
        assert "no hole" in (result.refusal or "")


# ------------------------------------------------------------------- the pooling itself


def test_two_kills_over_one_grid_pool_into_one_set_of_places():
    """The claim the whole module exists to make, and the one a reader has to check.

    Two kills, ten places each, the same ten: **ten** pooled spots and not twenty.
    Each is seen in both kills, which is the evidence published beside it.
    """
    result = spawnmap.build_encounter(payload())
    assert result.refusal is None
    assert len(result.spots) == 10
    assert all(spot.seen_in_kills == 2 for spot in result.spots)
    assert result.tolerance is not None
    assert result.tolerance["ratio"] >= 3.0


def test_three_areas_are_derived_from_the_waves_rather_than_the_coordinates():
    """An area is a connected component of the wave-to-place graph.

    Nothing here knows north from south: two kills that visit three sets of ten in
    turn produce three areas because no wave ever spans two sets, not because the
    coordinates were bucketed.
    """
    three = payload(
        [
            {
                **fight(),
                "sightings": (
                    sightings_for((FULL, FULL), area=1, at=20_000, seed=0)
                    + sightings_for((FULL, FULL), area=2, at=300_000, seed=40)
                    + sightings_for((FULL, FULL), area=3, at=600_000, seed=80)
                ),
            },
            {
                **fight(code="BBB"),
                "sightings": (
                    sightings_for((FULL, FULL), area=1, at=20_000, seed=5)
                    + sightings_for((FULL, FULL), area=2, at=300_000, seed=45)
                    + sightings_for((FULL, FULL), area=3, at=600_000, seed=85)
                ),
            },
        ]
    )
    result = spawnmap.build_encounter(three)
    assert result.refusal is None
    assert len(result.spots) == 30
    assert sorted({s.area for s in result.spots}) == [1, 2, 3]
    assert [len(a["spots"]) for a in result.areas] == [10, 10, 10]


def rotating(waves: int):
    """Waves whose four repeats rotate, so no place is preferred over the sample."""
    return [list(range(1, 11)) + [((k * 3 + j) % 10) + 1 for j in range(4)] for k in range(waves)]


def test_the_repeat_test_separates_a_preferred_spot_from_a_uniform_draw():
    """`separates` is a test, not a sentence, so it has to fire on a real difference.

    Note `FULL` is NOT the uniform case and cannot be used here: it repeats places
    2, 4, 6 and 8 in every wave, so a sample made of it is by construction the
    preferred one. That is what the rotating fixture is for.
    """
    uniform = spawnmap.build_encounter(
        payload([fight(waves=rotating(5)), fight(code="BBB", seed=7, waves=rotating(5))])
    )
    assert uniform.repeat is not None
    assert not uniform.repeat.separates

    # Every wave repeats the same two places, four copies between them, never any other.
    fixed = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1, 1, 1, 2, 2, 2]
    preferred = spawnmap.build_encounter(
        payload([fight(waves=(fixed,) * 4), fight(code="BBB", seed=7, waves=(fixed,) * 4)])
    )
    assert preferred.repeat is not None
    assert preferred.repeat.separates


def test_a_sample_more_even_than_chance_is_not_a_preferred_spot():
    """The one-sided half, and the defect it replaces.

    Ten places taking exactly two second copies each over ten waves is the most
    uniform sample constructible; its chi-square is 0.0 and its z is -6.21. A
    two-sided `abs(z) >= 1.96` reported that as "some spot is detectably preferred",
    which is the opposite of what the sample says.
    """
    even = spawnmap.build_encounter(
        payload([fight(waves=rotating(10)), fight(code="BBB", seed=7, waves=rotating(10))])
    )
    assert even.repeat.chi_square == pytest.approx(0.0)
    assert even.repeat.z < -1.96
    assert not even.repeat.separates


def test_a_spot_carries_how_far_its_sightings_scattered():
    """The gap between "first damaged" and "spawned", published rather than argued."""
    result = spawnmap.build_encounter(payload())
    assert all(spot.spread > 0 for spot in result.spots)
    widest = max(spot.spread for spot in result.spots)
    assert widest < result.tolerance["closestTwoSpotsCame"]


# ------------------------------------------------------------------------- the document


def test_a_kill_with_no_start_time_leaves_the_dates_unknown_rather_than_at_the_epoch():
    """A payload written before the probe recorded `startedAt` states none.

    Reading that absence as a timestamp would put every such kill at 1970 and publish
    a span of fifty-six years as the sampling window.
    """
    block = spawnmap.encounter_block(
        payload([fight(started_at=None), fight(code="BBB", seed=7, started_at=None)])
    )
    assert block["killedBetween"] is None
    assert all(row["startedAt"] is None for row in block["sampledFrom"])


def test_the_dates_say_how_many_kills_are_stamped_and_how_many_are_not():
    half = spawnmap.encounter_block(payload([fight(), fight(code="BBB", seed=7, started_at=None)]))
    assert half["killedBetween"]["stamped"] == 1
    assert half["killedBetween"]["of"] == 2


def test_a_one_boss_run_keeps_every_other_boss_the_document_already_had():
    """Union semantics, the rule `merge_gear_shards` arrived at the hard way.

    A spawn run is one encounter and one npc by construction, so a document that
    replaced its input wholesale would delete every boss it did not read -- and the
    deletion would look exactly like a boss nobody has probed.
    """
    published = {
        "encounters": [
            {"encounterId": 3470, "difficulty": 5, "npc": {"gameId": 1}, "spots": [{"spot": 1}]},
            {"encounterId": 3421, "difficulty": 5, "npc": {"gameId": NPC}, "spots": []},
        ]
    }
    fresh = spawnmap.encounter_block(payload())
    merged = spawnmap.document([fresh], tier="MID2", published=[published])
    ids = sorted(b["encounterId"] for b in merged["encounters"])
    assert ids == [3421, 3470]
    # The boss this run read is replaced; the one it did not is untouched.
    by_id = {b["encounterId"]: b for b in merged["encounters"]}
    assert len(by_id[3421]["spots"]) == 10
    assert by_id[3470]["spots"] == [{"spot": 1}]


def test_heroic_sits_beside_mythic_rather_than_over_it():
    """`fights.json`'s `measurements[]` rule: one block per difficulty read.

    Pooling them would be the mislabelling `build_encounter` refuses one layer down,
    and replacing one with the other discards a pass somebody paid for.
    """
    mythic = spawnmap.encounter_block(payload())
    heroic = spawnmap.encounter_block(
        payload(
            [fight(difficulty=4), fight(code="BBB", seed=7, difficulty=4)],
            difficulty=4,
        )
    )
    merged = spawnmap.document([heroic], tier="MID2", published=[{"encounters": [mythic]}])
    assert sorted(b["difficulty"] for b in merged["encounters"]) == [4, 5]


def test_a_block_without_spots_is_named_rather_than_counted():
    """A count says a block published nothing and cannot say which one to go and read."""
    doc = spawnmap.document([spawnmap.encounter_block(payload([fight()]))], tier="MID2")
    assert doc["coverage"]["withoutSpots"]
    named = doc["coverage"]["withoutSpots"][0]
    assert named["encounterId"] == 3421
    assert "pooling needs" in named["why"]


def test_a_quiet_rerun_leaves_the_file_byte_identical(tmp_path):
    """The settle, which is what makes a re-run leave nothing to commit.

    `measurement.cost` is a reading of Warcraft Logs' hourly meter, so it differs on
    every run by construction -- left in the comparison the settle can never fire,
    which is exactly how `write_fights` restamped for weeks.
    """
    out = tmp_path / "MID2"
    block = spawnmap.encounter_block(payload())

    first = spawnmap.publish(
        out, [block], tier="MID2", measurement={"generatedAt": "A", "cost": {"points": 1}}
    )
    spawnmap.write_spawns(out, first)
    was = (out / "spawns.json").read_bytes()

    second = spawnmap.publish(
        out, [block], tier="MID2", measurement={"generatedAt": "B", "cost": {"points": 999}}
    )
    spawnmap.write_spawns(out, second)
    assert (out / "spawns.json").read_bytes() == was


def test_a_real_change_still_writes(tmp_path):
    """The control: without it the test above passes against a writer that never writes."""
    out = tmp_path / "MID2"
    block = spawnmap.encounter_block(payload())
    spawnmap.write_spawns(out, spawnmap.publish(out, [block], tier="MID2"))
    was = (out / "spawns.json").read_bytes()

    moved = payload()
    moved["fights"].append(fight(code="CCC", seed=13))
    spawnmap.write_spawns(
        out, spawnmap.publish(out, [spawnmap.encounter_block(moved)], tier="MID2")
    )
    assert (out / "spawns.json").read_bytes() != was


def test_a_write_that_would_discard_every_spot_is_refused(tmp_path):
    out = tmp_path / "MID2"
    block = spawnmap.encounter_block(payload())
    spawnmap.write_spawns(out, spawnmap.publish(out, [block], tier="MID2"))

    empty = {"encounters": [], "coverage": {"blocks": 0, "encounters": 0, "withoutSpots": []}}
    with pytest.raises(spawnmap.SpotsWouldBeLost):
        spawnmap.write_spawns(out, empty)
    # ...and `--force` is the way through, because sometimes it is what you mean.
    spawnmap.write_spawns(out, empty, force=True)
    assert json.loads((out / "spawns.json").read_text())["encounters"] == []


def test_every_caveat_is_computed_from_a_number_the_document_publishes():
    """A caveat cannot outlive the condition that produced it.

    A clean sample must not carry the truncation sentence, or the sentence stops
    being read -- which is what a caveat printed unconditionally costs.
    """
    clean = spawnmap.build_encounter(payload())
    assert not any("page limit" in c for c in spawnmap.caveats(clean))

    short = spawnmap.build_encounter(
        payload([fight(truncated=True), fight(code="BBB", seed=7, truncated=True)])
    )
    assert any("page limit" in c for c in spawnmap.caveats(short))
