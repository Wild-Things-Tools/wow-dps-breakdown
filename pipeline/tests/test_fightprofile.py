"""Fight profiles: provenance, the simc options they produce, and what they refuse to.

Two things this file exists to keep true. First, that a fact and where it came from
travel together -- a value with no provenance cannot be constructed, and a missing
fact reads as a project default rather than as a measurement. Second, that a
scenario built from a profile never quietly drops the part of the encounter simc
cannot express.
"""

from __future__ import annotations

import json

import pytest

from wowdps import fightextract, fightprofile
from wowdps.fightprofile import (
    SOURCE_DEFAULT,
    SOURCE_HAND,
    SOURCE_LOGS,
    Amplification,
    Fact,
    FightProfile,
    FightProfileError,
    Provenance,
    load_profiles,
)


def profile(**facts) -> FightProfile:
    return FightProfile(
        tier="TEST",
        encounter_id=1,
        name="Test Boss",
        difficulty=5,
        facts={key: Fact.from_json(value) for key, value in facts.items()},
    )


def hand(value) -> dict:
    return {
        "value": value,
        "provenance": {"source": SOURCE_HAND, "detail": "stated", "statedBy": "owner"},
    }


def measured(value, sample: int = 5) -> dict:
    return {
        "value": value,
        "provenance": {
            "source": SOURCE_LOGS,
            "detail": "probe",
            "sample": sample,
            "reports": ["a", "b"],
        },
    }


# --------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------


def test_an_unknown_provenance_source_is_rejected_at_construction():
    """The whole point is that 'measured' and 'asserted' stay distinguishable, so a
    third unnamed kind must not slip in through a typo."""
    with pytest.raises(FightProfileError):
        Provenance(source="probably", detail="")


def test_a_missing_fact_is_a_default_and_says_so():
    """Not a None to be checked for: a scenario built on a fallback should be able
    to print that it was built on a fallback."""
    fact = profile().targets
    assert fact.provenance.source == SOURCE_DEFAULT
    assert "default" in fact.provenance.summary()


def test_hand_and_logs_facts_describe_themselves_differently():
    assert "asserted by hand" in Provenance(SOURCE_HAND, "", stated_by="owner").summary()
    assert "measured from logs" in Provenance(SOURCE_LOGS, "", sample=5).summary()


def test_provenance_survives_a_round_trip_through_json():
    original = Provenance(SOURCE_LOGS, "probe run", sample=3, reports=("aa", "bb"))
    assert Provenance.from_json(original.to_json()) == original


# --------------------------------------------------------------------------------
# Turning a profile into simc options
# --------------------------------------------------------------------------------


def test_a_permanent_multi_target_fight_becomes_a_target_count():
    plan = profile(targets=hand({"baseline": 3, "constant": True})).to_plan()
    assert plan.targets == 3
    assert plan.options == ()


def test_an_add_wave_becomes_an_adds_raid_event():
    plan = profile(
        targets=hand({"baseline": 1}),
        addWaves=hand(
            [{"name": "zealots", "count": 5, "first": 20, "duration": 20, "cadence": 60}]
        ),
    ).to_plan()
    assert plan.options == ("raid_events+=/adds,count=5,first=20,duration=20,cooldown=60",)


def test_a_one_off_wave_gets_a_cooldown_longer_than_any_fight():
    """simc reschedules on `cooldown` unconditionally; there is no 'once' switch,
    so a missing cadence has to become a cooldown nothing will reach."""
    plan = profile(addWaves=hand([{"count": 3, "first": 45, "duration": 15}])).to_plan()
    assert "cooldown=100000" in plan.options[0]


def test_an_amplification_on_the_priority_target_becomes_a_vulnerable_event():
    plan = profile(
        amplifications=hand(
            [
                {
                    "ability": "Empowered",
                    "multiplier": 1.2,
                    "first": 0,
                    "duration": 20,
                    "target": "priority",
                }
            ]
        )
    ).to_plan()
    assert plan.options == (
        "raid_events+=/vulnerable,first=0,duration=20,cooldown=100000,multiplier=1.2",
    )
    assert plan.unrepresented == ()


def test_an_amplification_on_an_add_is_reported_as_unmodelled_not_dropped():
    """simc's vulnerable event lands on the priority target unless it is given the
    name of a target, and adds in a raid event are generated rather than named. A
    scenario that silently models three quarters of a fight is worse than one that
    says which quarter is missing."""
    plan = profile(
        amplifications=hand(
            [
                {
                    "ability": "Empowered",
                    "multiplier": 1.2,
                    "first": 0,
                    "duration": 20,
                    "target": "add",
                }
            ]
        )
    ).to_plan()
    assert plan.options == ()
    assert len(plan.unrepresented) == 1 and "vulnerable" in plan.unrepresented[0]


def test_the_plan_names_which_of_its_facts_were_asserted():
    plan = profile(targets=hand({"baseline": 3}), fightLengthSeconds=measured(240)).to_plan()
    assert plan.asserted == ("targets",)


def test_a_scenario_built_from_a_profile_never_names_a_fight_style():
    """The trap this project has already hit once: ``sim_t::init_fight_style``
    clears ``raid_events_str`` for Patchwerk, so naming a style silently deletes
    the raid events the scenario is made of."""
    scenario = profile(
        targets=hand({"baseline": 3}),
        addWaves=hand([{"count": 5, "first": 20, "duration": 20, "cadence": 60}]),
    ).to_scenario()
    assert scenario.fight_style is None
    assert scenario.target_counts == (3,)
    assert scenario.command_options() == list(scenario.extra_options)


def test_a_scenario_borrows_its_funnel_baseline_from_patchwerk():
    """It has no add-free cell of its own, exactly like Add Waves."""
    assert profile(targets=hand({"baseline": 3})).to_scenario().funnel_baseline == "patchwerk"


def test_an_amplification_multiplier_is_never_marked_as_measured():
    """No field in the Warcraft Logs API says what an aura does, so a magnitude is
    somebody's word by construction."""
    amplification = Amplification.from_json(
        {"ability": "x", "multiplier": 1.2, "first": 0, "duration": 20, "target": "priority"}
    )
    assert amplification.magnitude_source == SOURCE_HAND


# --------------------------------------------------------------------------------
# The shipped data file
# --------------------------------------------------------------------------------


def test_the_shipped_file_carries_provenance_on_every_fact():
    profiles = load_profiles("MID2")
    assert profiles.profiles, "MID2 should list the tier's encounters"
    for entry in profiles.profiles.values():
        for key, fact in entry.facts.items():
            assert fact.provenance.source in fightprofile.VALID_SOURCES, (entry.name, key)
            assert fact.provenance.detail, f"{entry.name}/{key} has no detail"


def test_the_shipped_file_matches_what_the_owner_stated_about_lightblinded_vanguard():
    """The known-good case, pinned. Lightblinded Vanguard is a permanent three
    target fight, and one of the three takes about 20% extra damage for roughly
    the first twenty seconds."""
    vanguard = load_profiles("MID2").get(3180)
    assert vanguard is not None
    assert vanguard.baseline_targets == 3
    assert vanguard.targets.provenance.source == SOURCE_HAND
    assert vanguard.targets.provenance.stated_by == "owner"

    amplification = vanguard.amplifications[0]
    assert amplification.multiplier == pytest.approx(1.2)
    assert (amplification.first, amplification.duration) == (0, 20)
    # Which of the three carries it was not stated, so it is not guessed at.
    assert amplification.target == fightprofile.TARGET_UNKNOWN


def test_an_encounter_with_no_facts_falls_back_and_does_not_pretend_otherwise():
    """Eight of the nine MID2 bosses are unmeasured. That has to read as a gap."""
    profiles = load_profiles("MID2")
    unknown = profiles.get(3176)
    assert unknown is not None and unknown.facts == {}
    plan = unknown.to_plan()
    assert plan.targets == 1 and plan.asserted == ()
    assert unknown.targets.provenance.source == SOURCE_DEFAULT


def test_an_unlisted_tier_is_an_empty_profile_set_rather_than_an_error(tmp_path):
    """A profile file that has not caught up with a new raid should fall back to
    the sweep the site already publishes, not stop the run."""
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"tiers": {}}), encoding="utf-8")
    assert load_profiles("MID9", path).profiles == {}


# --------------------------------------------------------------------------------
# Profile against measurement
# --------------------------------------------------------------------------------


def observation_of(peak: int, size: int, duration: float) -> fightextract.EncounterObservation:
    start, end = 1_000_000, 1_000_000 + int(duration * 1000)
    events = [
        {"timestamp": start + 500, "type": "damage", "targetID": actor, "amount": 1000}
        for actor in range(10, 10 + peak)
    ] + [
        {"timestamp": end - 500, "type": "damage", "targetID": actor, "amount": 1000}
        for actor in range(10, 10 + peak)
    ]
    fight = {
        "id": 1,
        "encounterID": 3180,
        "name": "Lightblinded Vanguard",
        "size": size,
        "startTime": start,
        "endTime": end,
        "friendlyPlayers": list(range(size)),
        "kill": True,
    }
    observation = fightextract.EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [
        fightextract.observe_fight(
            report_code="r1",
            fight=fight,
            damage_events=events,
            death_events=[],
            aura_events=[],
            phase_metadata=[],
        )
    ]
    return observation


def test_a_disagreement_between_profile_and_probe_is_shown_rather_than_resolved():
    """If the probe says two targets on a fight the owner knows has three, the
    extraction is wrong -- and that is invisible if one overwrites the other."""
    vanguard = load_profiles("MID2").get(3180)
    rows = {row["fact"]: row for row in vanguard.compare_to_measurement(observation_of(2, 20, 280))}

    assert rows["baseline targets"]["profile"] == 3
    assert rows["baseline targets"]["measured"] == 2
    assert "asserted by hand" in rows["baseline targets"]["provenance"]


def aura_candidate(ability_id: int, name: str, start: float, duration: float) -> dict:
    return {
        "abilityId": ability_id,
        "ability": name,
        "seenInFights": 3,
        "start": {"median": start, "low": start, "high": start, "n": 3},
        "duration": {"median": duration, "low": duration, "high": duration, "n": 3},
    }


def test_an_amplification_with_no_ability_id_is_offered_a_candidate_not_given_one():
    """A hand-written profile starts with no ability id. The probe's job is to hand
    one back for somebody to write down, not to adopt one on its own."""
    amplification = Amplification.from_json(
        {"ability": "opening buff", "multiplier": 1.2, "first": 0, "duration": 20}
    )
    match, note = fightprofile._match_amplification(
        amplification,
        [
            aura_candidate(555001, "Blinding Fervor", 0.2, 20.4),
            aura_candidate(999, "Other", 180, 5),
        ],
    )
    assert match["abilityId"] == 555001
    assert "candidate only" in note


def test_no_aura_near_the_asserted_window_is_said_plainly():
    amplification = Amplification.from_json(
        {"ability": "opening buff", "multiplier": 1.2, "first": 0, "duration": 20}
    )
    match, note = fightprofile._match_amplification(
        amplification, [aura_candidate(999, "Something Else", 180, 5)]
    )
    assert match is None and "sits near this window" in note


def test_an_amplification_with_an_ability_id_is_matched_exactly():
    amplification = Amplification.from_json(
        {"ability": "x", "multiplier": 1.2, "first": 0, "duration": 20, "abilityId": 555001}
    )
    match, note = fightprofile._match_amplification(
        amplification, [aura_candidate(555001, "Blinding Fervor", 3.0, 25.0)]
    )
    # Matched on the id even though the window drifted: the id is the stronger
    # claim once somebody has confirmed it.
    assert match["abilityId"] == 555001 and "matched on ability id" in note


def test_the_probe_confirming_the_owner_leaves_both_numbers_standing():
    vanguard = load_profiles("MID2").get(3180)
    rows = {row["fact"]: row for row in vanguard.compare_to_measurement(observation_of(3, 20, 280))}
    assert rows["baseline targets"]["profile"] == rows["baseline targets"]["measured"] == 3
    assert rows["raid size"]["profile"] is None  # never asserted; the probe answers it
    assert rows["raid size"]["measured"] == 20
