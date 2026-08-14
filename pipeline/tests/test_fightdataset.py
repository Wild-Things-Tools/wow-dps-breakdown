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

from wowdps import fightdataset
from wowdps.fightextract import EncounterObservation, observe_fight
from wowdps.fightprofile import Fact, FightProfile, Provenance, TierProfiles, load_profiles

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
        player_ids=frozenset({1}),
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
    document = fightdataset.build_document("MID2", load_profiles("MID2"))

    assert document["measurement"] is None
    assert document["coverage"] == {"encounters": 9, "asserted": 1, "measured": 0}
    assert all(entry["measured"] is None for entry in document["encounters"])


def test_a_boss_nobody_has_written_facts_for_says_so_rather_than_defaulting_to_one_target():
    """Eight of the nine bosses are in this state, and it is the point of the view.

    The scenario still says one target because something has to be simmed, but every
    fact key reports `default` with the reason, so nothing on the page can read as a
    measurement of a one-target fight.
    """
    document = fightdataset.build_document("MID2", load_profiles("MID2"))
    entry = find(document, 3176)

    assert entry["hasFacts"] is False
    assert {fact["source"] for fact in entry["facts"]} == {"default"}
    # Not False: a fallback that says "constant" must not publish as a finding that
    # the boss holds a constant target count.
    assert entry["profile"]["constantTargets"] is None


def test_an_asserted_boss_carries_its_provenance_per_fact():
    entry = find(fightdataset.build_document("MID2", load_profiles("MID2")), 3180)
    by_key = {fact["key"]: fact for fact in entry["facts"]}

    assert by_key["targets"]["source"] == "hand"
    assert by_key["targets"]["statedBy"] == "owner"
    assert by_key["fightLengthSeconds"]["source"] == "default"
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
    document = fightdataset.build_document("MID2", load_profiles("MID2"), vanguard_payload())
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
    document = fightdataset.build_document("MID2", load_profiles("MID2"), vanguard_payload())
    drawn = find(document, 3180)["measured"]["timeline"]["representative"]["auras"]

    assert [entry["ability"] for entry in drawn] == ["Blinding Fervor"]


def test_the_comparison_puts_both_claims_on_the_page_and_resolves_neither():
    document = fightdataset.build_document("MID2", load_profiles("MID2"), vanguard_payload())
    rows = {row["fact"]: row for row in find(document, 3180)["comparison"]}

    assert rows["baseline targets"]["profile"] == 3
    assert rows["baseline targets"]["measured"] == 3
    assert rows["baseline targets"]["delta"] == 0
    # 300 asserted against 288 measured is a disagreement and stays one: the file
    # reports its size and offers no verdict on it.
    assert rows["fight length (s)"]["delta"] == -12.0
    assert not any("agree" in key for row in rows.values() for key in row)


def test_the_caveats_travel_with_the_numbers():
    document = fightdataset.build_document("MID2", load_profiles("MID2"), vanguard_payload())
    caveats = " ".join(find(document, 3180)["measured"]["caveats"])

    assert "page 1" in caveats
    assert find(document, 3180)["measured"]["fightsSampled"] == 3
    assert document["measurement"]["rankingsPage"] == 1
    assert document["measurement"]["samplingBias"] == "speed-kill shaped"


def test_looking_and_finding_nothing_is_not_the_same_as_never_looking():
    empty = EncounterObservation(3176, "Imperator Averzian", 5)
    payload = vanguard_payload()
    payload["encounters"].append(empty.to_json())
    document = fightdataset.build_document("MID2", load_profiles("MID2"), payload)

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
    profiles = load_profiles("MID2")
    first = fightdataset.build_document("MID2", profiles, generated_at="2026-01-01T00:00:00+00:00")
    fightdataset.write_fights(tmp_path, first)

    second = fightdataset.build_document("MID2", profiles, generated_at="2026-06-06T00:00:00+00:00")
    path = fightdataset.write_fights(tmp_path, second)

    assert json.loads(path.read_text())["generatedAt"] == "2026-01-01T00:00:00+00:00"


def test_a_changed_dataset_takes_the_new_timestamp(tmp_path):
    profiles = load_profiles("MID2")
    fightdataset.write_fights(
        tmp_path,
        fightdataset.build_document("MID2", profiles, generated_at="2026-01-01T00:00:00+00:00"),
    )
    path = fightdataset.write_fights(
        tmp_path,
        fightdataset.build_document(
            "MID2", profiles, vanguard_payload(), generated_at="2026-06-06T00:00:00+00:00"
        ),
    )

    assert json.loads(path.read_text())["generatedAt"] == "2026-06-06T00:00:00+00:00"
