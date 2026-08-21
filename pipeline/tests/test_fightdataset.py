"""The published fight dataset: what it says, and what it refuses to say.

Every input here is synthetic. That is not a limitation of the tests -- the
Warcraft Logs credentials are Actions secrets, so a development checkout cannot
produce a real probe payload at all, and the whole publishing path is written as a
pure function over one so it can be exercised offline. The fixtures go through the
real extraction (``observe_fight`` -> ``EncounterObservation.to_json``) rather than
being hand-written JSON, so a change to the probe's output shape breaks these
instead of silently publishing a file the view cannot read.
"""

from __future__ import annotations

import json

import pytest

from wowdps import fightdataset
from wowdps.fightextract import EncounterObservation, observe_fight
from wowdps.fightprofile import Fact, FightProfile, Provenance, TierProfiles, load_profiles

# These nine encounters are The Voidspire, Midnight Season 1's raid. They were
# filed under MID2 until 2026-08-17, when Warcraft Logs' own zone list settled
# it -- see the "nine bosses filed under MID2" note in CLAUDE.md. MID2 now holds
# The Venomous Abyss, whose bosses nobody has facts for yet.
VOIDSPIRE_TIER = "MID1"


START = 1_000_000


def damage(second: float, actor: int, amount: float = 1000.0, instance: int = 0) -> dict:
    return {
        "timestamp": int(START + second * 1000),
        "type": "damage",
        "targetID": actor,
        "targetInstance": instance,
        "amount": amount,
    }


def aura(second: float, kind: str, ability: int, actor: int, source: int | None = None) -> dict:
    event = {
        "timestamp": int(START + second * 1000),
        "type": kind,
        "abilityGameID": ability,
        "targetID": actor,
        "targetInstance": 0,
    }
    if source is not None:
        event["sourceID"] = source
    return event


def vanguard_fight(code: str, duration: float, amp_end: float):
    """One three-target pull with an encounter buff on one of the three."""
    return observe_fight(
        report_code=code,
        fight={
            "id": 7,
            "encounterID": 3180,
            "name": "Lightblinded Vanguard",
            "difficulty": 5,
            "kill": True,
            "size": 20,
            "startTime": START,
            "endTime": int(START + duration * 1000),
            "friendlyPlayers": list(range(20)),
        },
        damage_events=[damage(s, a) for a in (10, 11, 12) for s in (0.4, duration - 1)],
        death_events=[],
        aura_events=[
            aura(0.9, "applybuff", 555001, 11, source=99),
            aura(amp_end, "removebuff", 555001, 11, source=99),
            # A player cooldown on an enemy: same stream, same shape, and it must
            # not end up drawn on the chart as a boss mechanic.
            aura(30.0, "applydebuff", 111111, 10, source=1),
            aura(45.0, "removedebuff", 111111, 10, source=1),
        ],
        phase_metadata=[],
        actor_names={10: "Zealot", 11: "Champion", 12: "Seer"},
        ability_names={555001: "Blinding Fervor", 111111: "Avenging Wrath"},
        friendly_ids=frozenset({1}),
    )


def vanguard_payload(**overrides) -> dict:
    """A probe payload with three pulls of one boss, shaped like the real artifact."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [
        vanguard_fight("FIXTURE1", 285.0, 20.4),
        vanguard_fight("FIXTURE2", 288.0, 21.0),
        vanguard_fight("FIXTURE3", 334.0, 19.8),
    ]
    payload = {
        "generatedAt": "2026-08-14T00:00:00+00:00",
        "tier": "MID2",
        "difficulty": 5,
        "metric": "dps",
        "reportsPerEncounter": 3,
        "rankingsPage": 1,
        "eventStreams": ["damage", "deaths", "buffs", "debuffs"],
        "significantDamageShare": 0.01,
        "sampling": "speed-kill shaped",
        "cost": {"pointsSpentThisRun": 40.0},
        "abortedBecause": None,
        "encounters": [observation.to_json()],
    }
    payload.update(overrides)
    return payload


def find(document: dict, encounter_id: int) -> dict:
    return next(e for e in document["encounters"] if e["encounterId"] == encounter_id)


# --------------------------------------------------------------------------------
# Nothing measured: the state a checkout is in, and a publishable one
# --------------------------------------------------------------------------------


def test_a_dataset_with_no_probe_run_publishes_every_boss_with_a_null_measurement():
    document = fightdataset.build_document(VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER))

    assert document["measurement"] is None
    # Every boss carries facts now: `fight-promote --from-fights` wrote a measured
    # fight length and raid size into all nine on 2026-08-16. `asserted` counts
    # bosses something is known about, not bosses a *person* asserted -- the per-fact
    # provenance is where hand and logs are told apart.
    assert document["coverage"] == {"encounters": 9, "asserted": 9, "measured": 0}
    assert all(entry["measured"] is None for entry in document["encounters"])


def test_a_boss_nobody_has_written_facts_for_says_so_rather_than_defaulting_to_one_target(
    tmp_path,
):
    """The scenario still says one target, but no fact may read as a measurement.

    Driven from a synthetic profile file rather than the shipped one. Every MID2
    boss now carries promoted facts, so the shipped data no longer contains this
    state -- and a test that quietly stopped exercising its own case would be worse
    than one that fails.
    """
    path = tmp_path / "fight_profiles.json"
    path.write_text(
        json.dumps(
            {
                "note": "n",
                "tiers": {
                    "MID2": {
                        "difficulty": 5,
                        "encounters": [{"encounterId": 3176, "name": "A boss", "facts": {}}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    document = fightdataset.build_document(VOIDSPIRE_TIER, load_profiles("MID2", path))
    entry = find(document, 3176)

    assert entry["hasFacts"] is False
    assert {fact["source"] for fact in entry["facts"]} == {"default"}
    # Not False: a fallback that says "constant" must not publish as a finding that
    # the boss holds a constant target count.
    assert entry["profile"]["constantTargets"] is None


def test_an_asserted_boss_carries_its_provenance_per_fact():
    entry = find(fightdataset.build_document(VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER)), 3180)
    by_key = {fact["key"]: fact for fact in entry["facts"]}

    assert by_key["targets"]["source"] == "hand"
    assert by_key["targets"]["statedBy"] == "owner"
    # Promoted from the logs on 2026-08-16; it was `default` before that, and the
    # point of the assertion is that hand and logs sit side by side per fact.
    assert by_key["fightLengthSeconds"]["source"] == "logs"
    # The magnitude of an amplification is the one number the API cannot ever
    # supply, and the file says so where a reader will meet it.
    assert entry["profile"]["amplifications"][0]["magnitudeMeasurable"] is False
    assert entry["profile"]["amplifications"][0]["representable"] is False
    assert entry["scenario"]["unrepresented"]


# --------------------------------------------------------------------------------
# The scenario's own step function
# --------------------------------------------------------------------------------


def profile_with(facts: dict) -> FightProfile:
    return FightProfile(
        tier="MID2",
        encounter_id=1,
        name="Test",
        difficulty=5,
        facts={
            key: Fact(value=value, provenance=Provenance(source="hand", detail="test"))
            for key, value in facts.items()
        },
    )


def test_the_scenario_timeline_is_the_target_count_simc_would_actually_have():
    profile = profile_with(
        {
            "targets": {"baseline": 1, "constant": False},
            "fightLengthSeconds": 200,
            "addWaves": [{"name": "wave", "count": 3, "first": 30, "duration": 20, "cadence": 60}],
        }
    )
    assert fightdataset.scenario_target_steps(profile) == [
        [0.0, 1],
        [30.0, 4],
        [50.0, 1],
        [90.0, 4],
        [110.0, 1],
        [150.0, 4],
        [170.0, 1],
    ]


def test_a_one_shot_add_wave_fires_once_rather_than_forever():
    """simc has no fire-once switch, so a one-shot is a cooldown longer than any
    fight. Projecting that naively is an infinite loop."""
    profile = profile_with(
        {
            "targets": {"baseline": 1},
            "fightLengthSeconds": 300,
            "addWaves": [{"name": "wave", "count": 2, "first": 60, "duration": 30}],
        }
    )
    assert fightdataset.scenario_target_steps(profile) == [[0.0, 1], [60.0, 3], [90.0, 1]]


# --------------------------------------------------------------------------------
# With a probe payload
# --------------------------------------------------------------------------------


def test_the_timeline_is_one_real_pull_and_never_an_average_of_pulls():
    """A per-second median across pulls of different lengths is a shape no pull had.

    The published curve therefore belongs to a named report and fight, and the
    other sampled pulls are carried whole beside it.
    """
    document = fightdataset.build_document(
        VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), vanguard_payload()
    )
    timeline = find(document, 3180)["measured"]["timeline"]

    assert timeline["pooling"] == "representative"
    # 288s is the median of 285/288/334, and all three have the same mean target
    # count, so the tie breaks on length.
    assert timeline["representative"]["reportCode"] == "FIXTURE2"
    assert timeline["representative"]["durationSeconds"] == 288.0
    assert [entry["reportCode"] for entry in timeline["others"]] == ["FIXTURE1", "FIXTURE3"]
    assert timeline["representative"]["steps"][0] == [0.0, 0]
    assert timeline["representative"]["peak"] == 3


def test_a_player_cooldown_on_an_enemy_is_not_drawn_as_a_boss_mechanic():
    """The per-fight aura list has no source, so it carries player debuffs too.

    Filtering to the pooled shortlist -- which already dropped player-applied auras
    -- is what keeps Avenging Wrath off the chart. This exact mistake shipped once.
    """
    document = fightdataset.build_document(
        VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), vanguard_payload()
    )
    drawn = find(document, 3180)["measured"]["timeline"]["representative"]["auras"]

    assert [entry["ability"] for entry in drawn] == ["Blinding Fervor"]


def hand_only_profiles(tmp_path):
    """Lightblinded Vanguard as the owner asserted it, with nothing promoted yet.

    The comparison tests are about the *mechanism* -- an assertion and a measurement
    side by side, resolved by neither -- so they must not move every time a
    promotion writes a measured fight length into the shipped file. The hand facts
    below are exactly what `fight_profiles.json` carried before the first
    promotion run.
    """
    path = tmp_path / "fight_profiles.json"
    path.write_text(
        json.dumps(
            {
                "note": "n",
                "tiers": {
                    "MID2": {
                        "difficulty": 5,
                        "encounters": [
                            {
                                "encounterId": 3180,
                                "name": "Lightblinded Vanguard",
                                "facts": {
                                    "targets": {
                                        "value": {"baseline": 3, "constant": True},
                                        "provenance": {
                                            "source": "hand",
                                            "statedBy": "owner",
                                            "detail": "permanent three-target fight",
                                        },
                                    },
                                    "amplifications": {
                                        "value": [
                                            {
                                                "ability": "opening damage-taken buff",
                                                "multiplier": 1.2,
                                                "first": 0,
                                                "duration": 20,
                                                "target": "unknown",
                                                "abilityId": None,
                                                "magnitudeSource": "hand",
                                            }
                                        ],
                                        "provenance": {
                                            "source": "hand",
                                            "statedBy": "owner",
                                            "detail": "roughly 20% for roughly 20s",
                                        },
                                    },
                                },
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return load_profiles("MID2", path)


def test_the_comparison_puts_both_claims_on_the_page_and_resolves_neither(tmp_path):
    document = fightdataset.build_document(
        VOIDSPIRE_TIER, hand_only_profiles(tmp_path), vanguard_payload()
    )
    rows = {row["fact"]: row for row in find(document, 3180)["comparison"]}

    assert rows["baseline targets"]["profile"] == 3
    assert rows["baseline targets"]["measured"] == 3
    assert rows["baseline targets"]["delta"] == 0
    # 300 asserted against 288 measured is a disagreement and stays one: the file
    # reports its size and offers no verdict on it.
    assert rows["fight length (s)"]["delta"] == -12.0
    assert not any("agree" in key for row in rows.values() for key in row)


def test_the_caveats_travel_with_the_numbers():
    document = fightdataset.build_document(
        VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), vanguard_payload()
    )
    caveats = " ".join(find(document, 3180)["measured"]["caveats"])

    assert "page 1" in caveats
    assert find(document, 3180)["measured"]["fightsSampled"] == 3
    assert document["measurement"]["rankingsPage"] == 1
    assert document["measurement"]["samplingBias"] == "speed-kill shaped"


def test_looking_and_finding_nothing_is_not_the_same_as_never_looking():
    empty = EncounterObservation(3176, "Imperator Averzian", 5)
    payload = vanguard_payload()
    payload["encounters"].append(empty.to_json())
    document = fightdataset.build_document(VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), payload)

    assert find(document, 3176)["measured"]["fightsSampled"] == 0
    assert find(document, 3177)["measured"] is None
    assert document["coverage"]["measured"] == 1


def test_a_probe_of_an_encounter_the_profiles_have_never_heard_of_still_publishes():
    """A new raid arrives as measurements before anybody writes facts down."""
    payload = vanguard_payload()
    document = fightdataset.build_document(
        "MID2", TierProfiles(tier="MID2", note="", profiles={}), payload
    )
    entry = find(document, 3180)

    assert entry["name"] == "Lightblinded Vanguard"
    assert entry["hasFacts"] is False
    assert entry["measured"]["fightsSampled"] == 3


# --------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------


def test_an_unchanged_dataset_keeps_its_timestamp_so_a_quiet_run_commits_nothing(tmp_path):
    profiles = load_profiles(VOIDSPIRE_TIER)
    first = fightdataset.build_document(
        VOIDSPIRE_TIER, profiles, generated_at="2026-01-01T00:00:00+00:00"
    )
    fightdataset.write_fights(tmp_path, first)

    second = fightdataset.build_document(
        VOIDSPIRE_TIER, profiles, generated_at="2026-06-06T00:00:00+00:00"
    )
    path = fightdataset.write_fights(tmp_path, second)

    assert json.loads(path.read_text())["generatedAt"] == "2026-01-01T00:00:00+00:00"


def test_a_changed_dataset_takes_the_new_timestamp(tmp_path):
    profiles = load_profiles(VOIDSPIRE_TIER)
    fightdataset.write_fights(
        tmp_path,
        fightdataset.build_document(
            VOIDSPIRE_TIER, profiles, generated_at="2026-01-01T00:00:00+00:00"
        ),
    )
    path = fightdataset.write_fights(
        tmp_path,
        fightdataset.build_document(
            "MID2", profiles, vanguard_payload(), generated_at="2026-06-06T00:00:00+00:00"
        ),
    )

    assert json.loads(path.read_text())["generatedAt"] == "2026-06-06T00:00:00+00:00"


# --------------------------------------------------------------------------------
# What the logs could contribute, published rather than applied
# --------------------------------------------------------------------------------


def test_promotions_are_published_so_the_decision_can_be_looked_at():
    entry = find(
        fightdataset.build_document(
            VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), vanguard_payload()
        ),
        3180,
    )
    plan = {promotion["key"]: promotion for promotion in entry["promotions"]}

    # A gap the logs fill, offered.
    assert plan["fightLengthSeconds"]["eligible"] is True
    # A person's statement, held back with the reason on the page rather than in a
    # commit message.
    assert plan["targets"]["eligible"] is False
    assert plan["targets"]["blockedBy"] == "hand"
    # And the command that would apply them, spelled out.
    assert "fight-promote" in entry["promoteCommand"]
    assert "--write" in entry["promoteCommand"]


def test_a_boss_nobody_probed_offers_no_promotions():
    entry = find(fightdataset.build_document(VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER)), 3180)
    assert entry["promotions"] == []


def test_a_drawn_aura_window_names_the_enemy_that_carried_it():
    """A band labelled only with an ability, over a three-target chart, does not
    answer the question the band exists to raise."""
    entry = find(
        fightdataset.build_document(
            VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), vanguard_payload()
        ),
        3180,
    )
    drawn = entry["measured"]["timeline"]["representative"]["auras"]

    assert [window["actorName"] for window in drawn] == ["Champion"]
    assert drawn[0]["role"] == "unknown"
    # And the pooled block names it too, with the reasoning attached.
    pooled = entry["measured"]["auras"][0]
    assert pooled["carriedBy"][0]["name"] == "Champion"
    assert "nominates one as the priority target" in pooled["roleEvidence"]


def test_the_comparison_asks_which_enemy_carries_the_amplification(tmp_path):
    entry = find(
        fightdataset.build_document(
            VOIDSPIRE_TIER, hand_only_profiles(tmp_path), vanguard_payload()
        ),
        3180,
    )
    row = next(row for row in entry["comparison"] if "carried by" in row["fact"])

    assert row["profile"] == "unknown"
    assert row["measured"] == "Champion (unknown)"


# --------------------------------------------------------------------------------
# Kill patterns: several shapes, or one
# --------------------------------------------------------------------------------


def waved_fight(code: str, duration: float, wave_at: float | None, length: float = 90.0):
    """A single-target pull, optionally with two extra enemies for a while.

    The wave is long by default -- a third of a five-minute pull -- because the
    threshold is a *share of the fight*, and a wave shorter than it is by design not
    a different pattern.
    """
    events = [damage(second, 10) for second in (0.4, duration - 1)]
    if wave_at is not None:
        events += [
            damage(wave_at + step * length / 4, actor) for actor in (11, 12) for step in range(5)
        ]
    return observe_fight(
        report_code=code,
        fight={
            "id": 7,
            "encounterID": 3180,
            "name": "Lightblinded Vanguard",
            "difficulty": 5,
            "kill": True,
            "size": 20,
            "startTime": START,
            "endTime": int(START + duration * 1000),
            "friendlyPlayers": list(range(20)),
        },
        damage_events=events,
        death_events=[],
        aura_events=[],
        phase_metadata=[],
        actor_names={10: "Zealot", 11: "Champion", 12: "Seer"},
        friendly_ids=frozenset({1}),
    )


def payload_of(fights) -> dict:
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = list(fights)
    return vanguard_payload(encounters=[observation.to_json()])


def timeline_of(payload) -> dict:
    document = fightdataset.build_document(VOIDSPIRE_TIER, load_profiles(VOIDSPIRE_TIER), payload)
    return find(document, 3180)["measured"]["timeline"]


def test_pulls_that_agree_yield_one_pattern_and_nothing_to_choose_between():
    timeline = timeline_of(payload_of([waved_fight(f"F{i}", 300.0, 60.0) for i in range(4)]))

    assert len(timeline["patterns"]) == 1
    assert timeline["patterns"][0]["pulls"] == 4
    assert timeline["patterns"][0]["share"] == 1.0
    # The flattened fields still describe the pattern the view opens on.
    assert timeline["representative"] == timeline["patterns"][0]["representative"]


def test_two_shapes_are_two_presets_and_the_popular_one_comes_first():
    """Three pulls where the wave never arrived and two where it did. That is two
    patterns, not one pattern and two outliers, and the view must be able to say so."""
    fights = [waved_fight(f"NONE{i}", 300.0, None) for i in range(3)]
    fights += [waved_fight(f"WAVE{i}", 300.0, 60.0) for i in range(2)]
    timeline = timeline_of(payload_of(fights))

    patterns = timeline["patterns"]
    assert [entry["pulls"] for entry in patterns] == [3, 2]
    assert patterns[0]["share"] == 0.6
    assert all(code.startswith("NONE") for code in patterns[0]["reportCodes"])
    assert all(code.startswith("WAVE") for code in patterns[1]["reportCodes"])
    # Each preset draws its own pattern's pulls behind it, never the other's.
    assert [pull["reportCode"] for pull in patterns[1]["alsoInThisPattern"]][0].startswith("WAVE")
    assert {pull["reportCode"] for pull in patterns[1]["unmatched"]} == {
        f"NONE{i}" for i in range(3)
    }


def test_the_same_shape_at_two_lengths_is_one_pattern():
    """Comparison is on normalised fight time: a longer pull of the same shape is a
    longer pull, and its length is already a published fact of its own."""
    fights = [
        waved_fight("SHORT", 200.0, 40.0, length=60.0),
        waved_fight("LONG", 300.0, 60.0, length=90.0),
    ]
    timeline = timeline_of(payload_of(fights))

    assert len(timeline["patterns"]) == 1
    assert timeline["patterns"][0]["pulls"] == 2


def test_a_pull_nobody_else_matched_is_context_and_never_a_preset_of_one():
    """With a handful of pulls sampled, "one log did this" is not a pattern."""
    fights = [waved_fight(f"NONE{i}", 300.0, None) for i in range(3)]
    fights += [waved_fight("ODD", 300.0, 60.0)]
    timeline = timeline_of(payload_of(fights))

    assert len(timeline["patterns"]) == 1
    assert timeline["patterns"][0]["pulls"] == 3
    assert [pull["reportCode"] for pull in timeline["patterns"][0]["unmatched"]] == ["ODD"]


def test_when_no_two_pulls_agree_a_chart_still_has_something_to_draw():
    fights = [
        waved_fight("A", 300.0, None),
        waved_fight("B", 300.0, 20.0),
        waved_fight("C", 300.0, 190.0),
    ]
    timeline = timeline_of(payload_of(fights))

    # One entry, holding one pull, with the other two carried as context. A chart
    # still has something to draw; the caveats are where "no two of these agreed"
    # belongs, not a preset list of three.
    assert len(timeline["patterns"]) == 1
    assert timeline["patterns"][0]["pulls"] == 1
    assert len(timeline["others"]) == 2


def test_patterns_are_stable_across_runs_whatever_order_the_pulls_arrive_in():
    """Determinism is the whole point of committing this file."""
    fights = [waved_fight(f"NONE{i}", 300.0, None) for i in range(3)]
    fights += [waved_fight(f"WAVE{i}", 300.0, 60.0) for i in range(2)]
    forward = timeline_of(payload_of(fights))
    backward = timeline_of(payload_of(list(reversed(fights))))

    assert [entry["reportCodes"] for entry in forward["patterns"]] == [
        entry["reportCodes"] for entry in backward["patterns"]
    ]
    assert forward["representative"]["reportCode"] == backward["representative"]["reportCode"]


def test_a_pattern_label_says_how_often_the_peak_is_reached():
    """Peak and mean alone can label two different shapes identically.

    Measured on the real MID2 probe: Vaelgor & Ezzorak's six pulls split into a
    237s kill that reaches three targets once and a 337s kill that reaches three
    twice. Both come out as "peaks at 3, 2.1 on average" without the visit count,
    and a chooser offering two identical-looking options is worse than none.
    """
    once = {
        "peak": 3,
        "mean": 2.096,
        "constant": False,
        "steps": [[0.0, 1], [129.6, 3], [155.4, 2], [237.5, 0]],
    }
    twice = {
        "peak": 3,
        "mean": 2.18,
        "constant": False,
        "steps": [[0.0, 1], [129.2, 3], [159.1, 2], [299.4, 3], [336.8, 0]],
    }
    assert fightdataset._pattern_label(once) == "peaks at 3 once, 2.1 on average"
    assert fightdataset._pattern_label(twice) == "peaks at 3 twice, 2.2 on average"

    # A fight that never leaves its target count says so instead, and in the
    # singular where that is what one target is.
    assert (
        fightdataset._pattern_label({"peak": 1, "mean": 1.0, "constant": True, "steps": []})
        == "1 target throughout"
    )
    assert (
        fightdataset._pattern_label({"peak": 3, "mean": 3.0, "constant": True, "steps": []})
        == "3 targets throughout"
    )


def test_a_shape_with_no_measured_peak_is_not_given_a_label_that_sounds_measured():
    assert fightdataset._pattern_label({"mean": 2.0, "steps": []}) == "unmeasured shape"


def test_an_aura_up_for_the_whole_pull_is_reported_rather_than_shaded():
    """A band marks a stretch that differs from the rest of the fight.

    Lightblinded Vanguard's Light Infused runs the whole pull, so shading it shades
    the plot. Six of those turned the chart into a solid block the moment the probe
    sampled six pulls and more of the encounter's own long buffs survived the aura
    filter -- the same wash the per-ability cap exists to prevent, by another route.
    """
    fight = {
        "durationSeconds": 300.0,
        "auras": [
            {"abilityId": 1, "ability": "Light Infused", "start": 0.0, "duration": 299.0},
            {"abilityId": 2, "ability": "Zealous Spirit", "start": 84.0, "duration": 20.0},
        ],
    }
    pooled = [{"abilityId": 1}, {"abilityId": 2}]
    drawn = {aura["ability"]: aura for aura in fightdataset._drawable_auras(fight, pooled)}

    assert drawn["Light Infused"]["permanent"] is True
    assert drawn["Zealous Spirit"]["permanent"] is False
    # Still published: the view names it under the chart rather than dropping it.
    assert len(drawn) == 2


def test_the_target_band_is_the_distribution_across_kills_not_one_pull():
    """ "How many are normally up, when" answered from every kill at once: the median
    and the spread across kills at each point of the fight."""
    # Six kills, all single-target with a three-target wave, the wave arriving at
    # slightly different times so the kills disagree around it and agree elsewhere.
    fights = [waved_fight(f"F{i}", 300.0, 60.0 + i * 4) for i in range(6)]
    band = fightdataset._target_band(payload_of(fights)["encounters"][0])

    assert band is not None
    assert band["fights"] == 6
    assert band["medianLengthSeconds"] == 300.0

    def at(second):
        return min(band["band"], key=lambda b: abs(b["second"] - second))

    # Everyone agrees at the very start: one target, zero spread.
    assert at(5)["median"] == 1.0 and at(5)["min"] == at(5)["max"]
    # Deep in the wave every kill has three up.
    assert at(120)["median"] == 3.0
    # Around the wave's arrival the kills disagree, so the range is wide there.
    assert at(64)["max"] - at(64)["min"] >= 1.0


def test_the_band_leaves_out_partly_read_kills():
    """A partial read does not report the unread tail as zero -- `_resample` carries
    the last known value forward, so it freezes the target count at the cut point and
    asserts it for the rest of the fight. Since the end of a kill is where adds die
    off, that *overstates* how many were up at the end."""
    good = [waved_fight(f"G{i}", 300.0, 60.0) for i in range(4)]
    enc = payload_of(good)["encounters"][0]
    # Force one fight to look partly read.
    enc["fights"][0]["truncated"] = True
    band = fightdataset._target_band(enc)
    assert band["fights"] == 3  # the truncated one dropped


def test_nearly_read_is_still_not_read():
    """The rule this replaced admitted anything over 95%, beside a docstring that
    already claimed only fully-read fights. A twentieth of a five-minute kill is
    fifteen seconds of held-flat target count at the moment the fight empties out."""
    good = [waved_fight(f"G{i}", 300.0, 60.0) for i in range(4)]
    enc = payload_of(good)["encounters"][0]
    enc["fights"][0]["truncated"] = True
    enc["fights"][0]["eventCoverage"] = 0.95
    enc["fights"][1]["truncated"] = True
    enc["fights"][1]["eventCoverage"] = 0.98
    band = fightdataset._target_band(enc)
    assert band["fights"] == 2, "95% and 98% reads are still cut-short reads"


def test_a_complete_fetch_is_kept_even_when_its_coverage_is_low():
    """Coverage is not completeness. A raid that stops damaging an add halfway
    through leaves a genuine flat tail -- that is the encounter, not missing data,
    and dropping it would throw away a real observation."""
    good = [waved_fight(f"G{i}", 300.0, 60.0) for i in range(3)]
    enc = payload_of(good)["encounters"][0]
    for fight in enc["fights"]:
        fight["truncated"] = False
        fight["eventCoverage"] = 0.4
    assert fightdataset._target_band(enc)["fights"] == 3


def test_the_band_needs_at_least_two_kills():
    one = payload_of([waved_fight("F0", 300.0, 60.0)])["encounters"][0]
    assert fightdataset._target_band(one) is None


def test_a_rerun_that_changed_nothing_leaves_the_file_untouched(tmp_path):
    """The property the whole determinism discipline rests on: a diff means
    something moved.

    The first version of the settle listed only the top-level `generatedAt`, so the
    nested `measurement.generatedAt` still differed on every run, the comparison
    never matched, and neither stamp settled. Two probe re-runs that read identical
    fights each rewrote and committed the file -- observed live on 2026-08-15,
    where the only difference between two commits was the two timestamps.
    """
    from wowdps.fightdataset import write_fights

    first = {
        "schemaVersion": 1,
        "generatedAt": "2026-08-15T11:40:58+00:00",
        "tier": "MID2",
        "measurement": {"generatedAt": "2026-08-15T11:40:51+00:00", "reportsPerEncounter": 30},
        "encounters": [{"encounterId": 3176, "name": "A boss"}],
    }
    path = write_fights(tmp_path, first)
    before = path.read_text(encoding="utf-8")

    # Same fights, a later run: both stamps move and nothing else does.
    second = json.loads(json.dumps(first))
    second["generatedAt"] = "2026-08-15T13:05:02+00:00"
    second["measurement"]["generatedAt"] = "2026-08-15T13:04:58+00:00"
    write_fights(tmp_path, second)

    assert path.read_text(encoding="utf-8") == before


def test_a_real_change_still_moves_both_stamps(tmp_path):
    """The settle must not become a freeze: when the fights actually change, the
    stamps are the new run's."""
    from wowdps.fightdataset import write_fights

    first = {
        "generatedAt": "2026-08-15T11:40:58+00:00",
        "measurement": {"generatedAt": "2026-08-15T11:40:51+00:00"},
        "encounters": [{"encounterId": 3176, "fightsSampled": 30}],
    }
    write_fights(tmp_path, first)

    second = {
        "generatedAt": "2026-08-15T13:05:02+00:00",
        "measurement": {"generatedAt": "2026-08-15T13:04:58+00:00"},
        "encounters": [{"encounterId": 3176, "fightsSampled": 40}],
    }
    path = write_fights(tmp_path, second)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["generatedAt"] == "2026-08-15T13:05:02+00:00"
    assert written["measurement"]["generatedAt"] == "2026-08-15T13:04:58+00:00"
    assert written["encounters"][0]["fightsSampled"] == 40


def test_a_document_with_no_measurement_block_still_settles(tmp_path):
    """A tier published from profiles alone carries `measurement: null`."""
    from wowdps.fightdataset import write_fights

    first = {"generatedAt": "2026-08-15T11:40:58+00:00", "measurement": None, "encounters": []}
    path = write_fights(tmp_path, first)
    before = path.read_text(encoding="utf-8")
    write_fights(tmp_path, {**first, "generatedAt": "2026-08-15T13:05:02+00:00"})
    assert path.read_text(encoding="utf-8") == before


def test_writing_without_a_probe_refuses_to_discard_published_measurements(tmp_path):
    """`wowdps fights` with no probe over a probed directory is silent data loss.

    The command is deliberately usable with no probe at all -- that is the honest
    state of a checkout that has never reached Warcraft Logs. Pointed at a
    directory that already holds a probe's results it is something else: 30 sampled
    kills per boss replaced by nulls, and the run reports success. Done exactly
    that once, by hand, one command after promoting the facts those measurements
    produced.
    """
    from wowdps.fightdataset import MeasurementWouldBeLost, write_fights

    measured = {"coverage": {"encounters": 9, "asserted": 1, "measured": 9}, "encounters": []}
    write_fights(tmp_path, measured)

    bare = {"coverage": {"encounters": 9, "asserted": 9, "measured": 0}, "encounters": []}
    with pytest.raises(MeasurementWouldBeLost, match="would discard"):
        write_fights(tmp_path, bare)

    # The published file is untouched by the refusal.
    still = json.loads((tmp_path / "fights.json").read_text(encoding="utf-8"))
    assert still["coverage"]["measured"] == 9

    # And somebody who means it can say so.
    write_fights(tmp_path, bare, force=True)
    assert json.loads((tmp_path / "fights.json").read_text())["coverage"]["measured"] == 0


def test_a_first_publish_is_not_a_loss(tmp_path):
    """Nothing published yet means nothing to lose -- the guard must not block that."""
    from wowdps.fightdataset import write_fights

    bare = {"coverage": {"encounters": 9, "asserted": 9, "measured": 0}, "encounters": []}
    assert write_fights(tmp_path, bare).is_file()


def test_an_empty_encounter_says_whether_the_kills_exist_elsewhere():
    """"No fights" and "no fights at the difficulty asked for" are different answers.

    Only the second names its own fix, and on a page that shows a count they look
    the same. The counts come from the report search, which is deliberately
    unfiltered by difficulty, so it sees the kills the probe then declines to open.
    """
    from wowdps.fightdataset import _no_fights_caveats

    plain = _no_fights_caveats({"difficulty": 5})
    assert len(plain) == 1

    told = _no_fights_caveats({"difficulty": 5, "difficultiesSeen": {"4": 54, "None": 3}})
    assert len(told) == 2
    assert "54 at Heroic" in told[1]
    assert "3 with no difficulty recorded" in told[1]
    assert "asked for Mythic" in told[1]
