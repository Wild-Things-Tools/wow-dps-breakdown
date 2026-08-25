"""``wowdps fight-promote``: the only route a measurement has into a profile.

The one behaviour worth more than all the others here: **a hand-asserted fact is
never overwritten.** Two guards enforce it -- the planner refuses to mark such a
promotion eligible, and the writer refuses again at the point of mutation -- and
both are tested, because a single guard is one refactor away from being the wrong
one.

Everything is exercised through the argparse namespace the CLI builds, against a
temporary copy of the profile file, so a test can never edit the shipped one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wowdps import fightpromote
from wowdps.fightextract import EncounterObservation, observe_fight
from wowdps.fightprofile import SOURCE_HAND, SOURCE_LOGS, load_profiles

START = 1_000_000


def pull(code: str, duration: float, *, targets: int = 3, amp_on: int | None = 11) -> dict:
    """One three-target pull with an encounter buff on one of the three."""
    end = int(START + duration * 1000)
    actors = list(range(10, 10 + targets))
    auras: list[dict] = []
    if amp_on is not None:
        auras = [
            {
                "timestamp": START + 900,
                "type": "applybuff",
                "abilityGameID": 555_001,
                "targetID": amp_on,
            },
            {
                "timestamp": START + 20_900,
                "type": "removebuff",
                "abilityGameID": 555_001,
                "targetID": amp_on,
            },
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
            "endTime": end,
            "friendlyPlayers": list(range(20)),
        },
        damage_events=[
            {"timestamp": when, "type": "damage", "targetID": actor, "amount": 1000}
            for actor in actors
            for when in (START + 400, end - 1000)
        ],
        death_events=[],
        aura_events=auras,
        phase_metadata=[],
        actor_names={10: "Zealot", 11: "Champion", 12: "Seer"},
        ability_names={555_001: "Blinding Fervor"},
    )


def probe_payload(**overrides) -> dict:
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [pull("AAA", 285.0), pull("BBB", 288.0), pull("CCC", 334.0)]
    payload = {
        "generatedAt": "2026-08-14T00:00:00+00:00",
        "tier": "MID2",
        "encounters": [observation.to_json()],
    }
    payload.update(overrides)
    return payload


def setup(tmp_path: Path, **args) -> argparse.Namespace:
    """A profile file of our own plus a probe artifact, wired as the CLI wires them."""
    # A file of our own rather than a copy of the shipped one. It used to be a copy,
    # which was fine while the shipped file held only the owner's assertions and
    # broke the moment a promotion run filled the blanks in it: the case under test
    # is "a blank gets filled", and against an already-filled file that case cannot
    # occur. The facts below are what MID2's Lightblinded Vanguard carried before
    # the first promotion.
    profiles = tmp_path / "fight_profiles.json"
    profiles.write_text(
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

    probe = tmp_path / "fight-probe-MID2.json"
    probe.write_text(json.dumps(probe_payload()), encoding="utf-8")

    namespace = argparse.Namespace(
        probe=str(probe),
        tier="MID2",
        encounter=None,
        min_fights=3,
        write=False,
        profiles_file=str(profiles),
    )
    for key, value in args.items():
        setattr(namespace, key, value)
    return namespace


def facts_of(path: Path, encounter_id: int = 3180) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    encounter = next(
        entry
        for entry in document["tiers"]["MID2"]["encounters"]
        if entry["encounterId"] == encounter_id
    )
    return encounter.get("facts") or {}


def test_a_dry_run_writes_nothing_at_all(tmp_path, capsys):
    args = setup(tmp_path)
    before = Path(args.profiles_file).read_text(encoding="utf-8")

    assert fightpromote.cmd_fight_promote(args) == 0

    assert Path(args.profiles_file).read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert "--write" in out


def test_writing_records_the_source_the_sample_and_the_reports(tmp_path):
    args = setup(tmp_path, write=True)
    assert fightpromote.cmd_fight_promote(args) == 0

    fact = facts_of(Path(args.profiles_file))["fightLengthSeconds"]
    assert fact["value"] == 288
    assert fact["provenance"]["source"] == SOURCE_LOGS
    assert fact["provenance"]["sample"] == 3
    assert fact["provenance"]["reports"] == ["AAA", "BBB", "CCC"]
    assert fact["provenance"]["observedAt"] == "2026-08-14T00:00:00+00:00"
    # The number a reader would otherwise take as exact, with its range attached.
    assert "285" in fact["provenance"]["detail"] and "334" in fact["provenance"]["detail"]


def test_the_owners_target_count_survives_a_write(tmp_path):
    """The rule, end to end. Nothing about running the command may touch it."""
    args = setup(tmp_path, write=True)
    before = facts_of(Path(args.profiles_file))["targets"]

    fightpromote.cmd_fight_promote(args)

    after = facts_of(Path(args.profiles_file))["targets"]
    assert after == before
    assert after["provenance"]["source"] == SOURCE_HAND


def test_the_writer_refuses_a_hand_fact_even_if_something_marks_it_eligible(tmp_path):
    """The second guard, tested on its own.

    The planner would never produce this promotion. The writer is the last thing
    standing between a bug upstream and a person's statement being deleted, so it
    is checked without the planner in the way.
    """
    from wowdps.fightprofile import Promotion

    args = setup(tmp_path)
    document = json.loads(Path(args.profiles_file).read_text(encoding="utf-8"))
    forged = Promotion(
        key="targets",
        label="Targets at the pull",
        value={"baseline": 99, "constant": False},
        summary="99 targets",
        evidence="forged",
        sample=3,
        reports=("AAA",),
        eligible=True,
        reason="forged",
    )

    assert fightpromote.apply_promotion(document, "MID2", 3180, forged) is False
    encounter = next(
        entry for entry in document["tiers"]["MID2"]["encounters"] if entry["encounterId"] == 3180
    )
    assert encounter["facts"]["targets"]["value"] == {"baseline": 3, "constant": True}


def test_a_blank_inside_a_hand_fact_is_filled_and_the_hand_prose_is_left_alone(tmp_path):
    args = setup(tmp_path, write=True)
    before = facts_of(Path(args.profiles_file))["amplifications"]["provenance"]

    fightpromote.cmd_fight_promote(args)

    fact = facts_of(Path(args.profiles_file))["amplifications"]
    amplification = fact["value"][0]
    # The blank the person left, filled from the measurement.
    assert amplification["abilityId"] == 555_001
    # The numbers the person stated, untouched.
    assert amplification["multiplier"] == 1.2
    assert amplification["magnitudeSource"] == SOURCE_HAND
    # And the fact stays theirs, prose and all: it records what was said, not what
    # the value is now.
    assert fact["provenance"] == before
    assert fact["provenance"]["source"] == SOURCE_HAND


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    args = setup(tmp_path, write=True)
    fightpromote.cmd_fight_promote(args)
    once = Path(args.profiles_file).read_text(encoding="utf-8")

    fightpromote.cmd_fight_promote(args)
    assert Path(args.profiles_file).read_text(encoding="utf-8") == once


def test_the_file_it_writes_is_still_a_file_the_loader_reads(tmp_path):
    args = setup(tmp_path, write=True)
    fightpromote.cmd_fight_promote(args)

    profiles = load_profiles("MID2", Path(args.profiles_file))
    vanguard = profiles.get(3180)
    assert vanguard.fight_length.value == 288
    assert vanguard.fight_length.provenance.source == SOURCE_LOGS
    assert vanguard.amplifications[0].ability_id == 555_001


def test_promoting_from_a_published_fights_file_needs_no_probe_artifact(tmp_path):
    """The probe payload is ~160 MB and lives in a CI run's attachments.

    fights.json carries `Promotion.to_json()` verbatim, so the manual step -- which
    stays manual, because a disagreement between an assertion and the log reader is
    the most valuable thing this subsystem produces -- is reachable from a plain
    checkout.
    """
    args = setup(tmp_path, write=True)
    fights = tmp_path / "fights.json"
    fights.write_text(
        json.dumps(
            {
                "measurement": {"generatedAt": "2026-08-16T15:42:20+00:00"},
                "encounters": [
                    {
                        "encounterId": 3180,
                        "promotions": [
                            {
                                "key": "raidSize",
                                "label": "Raid size",
                                "value": 20,
                                "summary": "20 players",
                                "evidence": "the log's own group size",
                                "sample": 30,
                                "reports": ["AAA"],
                                "eligible": True,
                                "reason": "nothing is recorded for this fact",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    args.probe = None
    args.from_fights = str(fights)

    assert fightpromote.cmd_fight_promote(args) == 0

    fact = facts_of(Path(args.profiles_file))["raidSize"]
    assert fact["value"] == 20
    assert fact["provenance"]["source"] == "logs"
    # The measurement's own timestamp travels with it, not this run's clock.
    assert fact["provenance"]["observedAt"] == "2026-08-16T15:42:20+00:00"


def test_neither_source_named_is_an_error_rather_than_a_silent_no_op(tmp_path):
    args = setup(tmp_path)
    args.probe = None
    args.from_fights = None
    assert fightpromote.cmd_fight_promote(args) == 1
