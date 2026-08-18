"""Running a boss's own fight profile instead of a fight style.

The profiles have always produced a `Scenario`; nothing could select one. These
tests pin the selection, and pin the one thing that would make the feature worse
than useless: quietly publishing a target-sweep cell under a boss's name.
"""

from __future__ import annotations

import json

import pytest

from wowdps.cli import _resolve_scenarios
from wowdps.fightprofile import boss_scenarios, load_profiles

FIXTURE = {
    "note": "test",
    "tiers": {
        "MID2": {
            "difficulty": 5,
            "encounters": [
                {
                    "encounterId": 100,
                    "name": "Asserted Boss",
                    "facts": {
                        "targets": {
                            "value": {"baseline": 3, "constant": True},
                            "provenance": {"source": "hand", "detail": "the owner plays it"},
                        },
                        "addWaves": {
                            "value": [
                                {
                                    "name": "wave",
                                    "count": 4,
                                    "first": 30,
                                    "duration": 20,
                                    "cadence": 60,
                                }
                            ],
                            "provenance": {"source": "logs", "detail": "measured"},
                        },
                    },
                },
                {
                    "encounterId": 200,
                    "name": "Unknown Boss",
                    "facts": {},
                },
                {
                    "encounterId": 300,
                    "name": "Static Boss",
                    "facts": {
                        "targets": {
                            "value": {"baseline": 3, "constant": True},
                            "provenance": {"source": "hand", "detail": "three, permanently"},
                        }
                    },
                },
            ],
        }
    },
}


@pytest.fixture
def profiles_file(tmp_path):
    path = tmp_path / "fight_profiles.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


def test_a_boss_nobody_knows_anything_about_gets_no_scenario(profiles_file):
    """A profile of pure fallbacks sims as one target for 300s -- Patchwerk at one,
    wearing a boss's name. The number would be right and the label unearned."""
    available = boss_scenarios(load_profiles("MID2", profiles_file))

    assert set(available) == {"boss_100", "boss_300"}
    assert "boss_200" not in available


def test_a_boss_scenario_carries_the_profile_shape(profiles_file):
    available = boss_scenarios(load_profiles("MID2", profiles_file))
    scenario = available["boss_100"]

    assert scenario.label == "Asserted Boss"
    assert scenario.target_counts == (3,)
    # A fight style would clear the raid events this scenario is built out of.
    assert scenario.fight_style is None
    assert any("adds,count=4" in option for option in scenario.extra_options)


def test_a_profile_with_nothing_simc_can_express_says_so(profiles_file):
    """The check that stops the feature being a relabelled sweep."""
    profiles = load_profiles("MID2", profiles_file)

    assert profiles.get(300).to_plan().restates_a_static_sweep() is True
    assert profiles.get(100).to_plan().restates_a_static_sweep() is False


def test_scenarios_resolve_by_name_and_by_the_whole_tier(profiles_file):
    names = [s.id for s in _resolve_scenarios(["patchwerk", "bosses"], "MID2", str(profiles_file))]
    assert names == ["patchwerk", "boss_100", "boss_300"]

    only = _resolve_scenarios(["boss_300"], "MID2", str(profiles_file))
    assert [s.id for s in only] == ["boss_300"]


def test_a_repeated_name_is_a_typo_not_a_request_to_sim_it_twice(profiles_file):
    names = [
        s.id
        for s in _resolve_scenarios(
            ["bosses", "boss_100", "patchwerk", "patchwerk"], "MID2", str(profiles_file)
        )
    ]
    assert names == ["boss_100", "boss_300", "patchwerk"]


def test_no_scenario_named_means_the_built_in_set(profiles_file):
    assert [s.id for s in _resolve_scenarios(None, "MID2", str(profiles_file))] == [
        "patchwerk",
        "addwaves",
        "hecticaddcleave",
        "dungeonslice",
    ]


def test_an_unknown_boss_names_the_ones_that_exist(profiles_file):
    with pytest.raises(KeyError) as caught:
        _resolve_scenarios(["boss_999"], "MID2", str(profiles_file))
    assert "boss_100" in caught.value.args[0]


def test_a_tier_with_no_asserted_boss_explains_what_to_run(profiles_file):
    with pytest.raises(KeyError) as caught:
        _resolve_scenarios(["bosses"], "MID9", str(profiles_file))
    message = caught.value.args[0]
    assert "fight-probe" in message and "fight-promote" in message


def test_an_empty_boss_list_is_fatal_alone_and_a_warning_alongside_others(tmp_path):
    """A season whose raid has not opened has no boss to sim. That is not a failure.

    It was treated as one, and on 2026-08-18 it took down all twelve shards of a
    nightly run that had four other scenarios to do -- one day after the re-file
    moved MID2's asserted bosses to MID1 and left MID2 with eight factless
    encounters. Asking for bosses and getting none is still an error when the
    bosses are the whole request.
    """
    import json

    import pytest

    from wowdps.cli import _resolve_scenarios

    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "tiers": {
                    "MID2": {
                        "difficulty": 5,
                        "encounters": [{"encounterId": 1, "name": "A boss", "facts": {}}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(KeyError, match="no boss in MID2"):
        _resolve_scenarios(["bosses"], "MID2", str(path))

    resolved = _resolve_scenarios(["patchwerk", "bosses"], "MID2", str(path))
    assert [scenario.id for scenario in resolved] == ["patchwerk"]
