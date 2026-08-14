"""Comparing a spec's builds with the gear held still.

The distinction this module exists for: the dataset's spec rows put each build on
the gear simc's authors picked for it, which answers "which build should I play".
Holding gear still answers "what do these talents do". Both are right and they are
not the same question -- measured on MID2 Arcane at 1000 deterministic iterations,
Sunfury leads Spellslinger by 7.1% on shipped gear and 6.5% on equal gear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wowdps.profiles import SpecProfile
from wowdps.simc_runner import Profileset, parse_profilesets, profileset_options
from wowdps.talentsweep import SweepResult, choose_base, sweep_spec, variants_for


def build(spec: str, hero: str | None, talents: str | None) -> SpecProfile:
    return SpecProfile(
        path=Path(f"/tmp/MID2_Mage_{spec}.simc"),
        tier="MID2",
        wow_class="Mage",
        spec=spec,
        hero_talent=hero,
        role="spell",
        talent_hash=talents,
    )


def test_a_build_with_no_talent_hash_cannot_be_a_variant():
    """There is nothing to swap, and dropping it silently would make a sweep look
    complete with a build missing from it."""
    profiles = [
        build("Arcane", "Sunfury", "AAA"),
        build("Arcane", "Spellslinger", "BBB"),
        build("Arcane", "Frostfire", None),
    ]
    assert [v.hero_talent for v in variants_for(profiles)] == ["Sunfury", "Spellslinger"]


def test_the_base_profile_is_stable_whatever_order_the_profiles_arrive_in():
    """Which character everybody wears matters to the absolute numbers and not to
    the comparison, so it only has to be the *same* one every run."""
    profiles = [build("Arcane", "Sunfury", "AAA"), build("Arcane", "Spellslinger", "BBB")]
    assert choose_base(profiles).id == choose_base(list(reversed(profiles))).id


def test_one_build_is_not_a_comparison(monkeypatch):
    called = False

    def never(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("wowdps.simc_runner.run_profilesets", never)
    from wowdps.scenarios import SimSettings

    assert sweep_spec(Path("simc"), [build("Arcane", "Sunfury", "AAA")], SimSettings()) is None
    assert called is False, "a spec with one build must not cost a simc run"


def test_every_variant_carries_only_its_talents():
    """Gear held still is the entire point: a variant that changed anything else
    would answer the question the dataset already answers."""
    sets = [
        Profileset(key="mage_arcane_sunfury", options=("talents=AAA",)),
        Profileset(key="mage_arcane_spellslinger", options=("talents=BBB",)),
    ]
    assert profileset_options(sets) == [
        "profileset.mage_arcane_sunfury=talents=AAA",
        "profileset.mage_arcane_spellslinger=talents=BBB",
    ]


def test_both_rankings_come_out_of_one_run():
    """`profileset_metric` takes a list, so the best build by damage and the best by
    damage to the priority target are one set of sims rather than two."""
    report = {
        "sim": {
            "profilesets": {
                "results": [
                    {
                        "name": "cleaver",
                        "mean": 500.0,
                        "mean_stddev": 1.0,
                        "iterations": 1000,
                        "additional_metrics": [
                            {"metric": "Damage per Second to Priority Target/Boss", "mean": 150.0}
                        ],
                    },
                    {
                        "name": "single_target",
                        "mean": 450.0,
                        "mean_stddev": 1.0,
                        "iterations": 1000,
                        "additional_metrics": [
                            {"metric": "Damage per Second to Priority Target/Boss", "mean": 300.0}
                        ],
                    },
                ]
            }
        }
    }
    parsed = parse_profilesets(report)
    assert parsed["cleaver"].priority_dps == 150.0

    from wowdps.talentsweep import BuildMeasurement

    result = SweepResult(
        spec_id="mage_arcane",
        spec_label="Arcane Mage",
        wow_class="Mage",
        base_profile_id="mage_arcane_spellslinger",
        targets=5,
        builds=[
            BuildMeasurement("cleaver", "C", "Cleaver", 500.0, 0.1, 150.0, 1000),
            BuildMeasurement("single_target", "S", "Single", 450.0, 0.1, 300.0, 1000),
        ],
    )
    document = result.to_json()
    assert document["bestByDps"] == "cleaver"
    assert document["bestByPriorityDps"] == "single_target"
    # The case worth naming: most damage overall is not most damage on the boss.
    assert document["rankingsDisagree"] is True


def test_a_run_with_no_priority_metric_still_ranks_by_damage():
    from wowdps.talentsweep import BuildMeasurement

    result = SweepResult(
        spec_id="mage_arcane",
        spec_label="Arcane Mage",
        wow_class="Mage",
        base_profile_id="base",
        targets=1,
        builds=[
            BuildMeasurement("a", "A", "A", 500.0, 0.1, None, 1000),
            BuildMeasurement("b", "B", "B", 450.0, 0.1, None, 1000),
        ],
    )
    document = result.to_json()
    assert document["bestByDps"] == "a"
    assert document["bestByPriorityDps"] is None
    assert document["rankingsDisagree"] is False


def test_a_profileset_that_returned_nothing_is_skipped_rather_than_zeroed():
    assert (
        parse_profilesets({"sim": {"profilesets": {"results": [{"name": "x", "mean": 0}]}}}) == {}
    )
    assert parse_profilesets({}) == {}


@pytest.mark.parametrize("metric", ["dps", "prioritydps"])
def test_ranking_never_invents_an_entry(metric):
    from wowdps.talentsweep import BuildMeasurement

    result = SweepResult(
        "s", "S", "Mage", "b", 1, [BuildMeasurement("a", "A", "A", 1.0, 0.1, None, 1)]
    )
    assert len(result.ranked_by(metric)) == (1 if metric == "dps" else 0)
