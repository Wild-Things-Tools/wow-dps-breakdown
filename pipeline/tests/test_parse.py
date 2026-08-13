"""Tests for the metric extraction, especially the funnel maths."""

from __future__ import annotations

import pytest

from wowdps.parse import burst_ratio, extract_abilities, extract_timeline, parse_cell


def sample(mean: float, **extra: float) -> dict:
    return {"mean": mean, **extra}


def make_report(
    *,
    dps: float,
    priority_dps: float | None = None,
    stats: list[dict] | None = None,
    timeline: list[float] | None = None,
    fight_length_min: float = 240.0,
    iterations: int = 1000,
) -> dict:
    collected: dict = {
        "dps": sample(dps, mean_std_dev=dps * 0.002, std_dev=dps * 0.1, count=iterations),
        "fight_length": sample(300.0, min=fight_length_min, max=360.0),
    }
    if priority_dps is not None:
        collected["prioritydps"] = sample(priority_dps)
    if timeline is not None:
        collected["timeline_dmg"] = {"mean": 0.0, "data": timeline}

    return {
        "sim": {
            "options": {"iterations": iterations},
            "players": [{"stats": stats or [], "collected_data": collected}],
        }
    }


def test_funnel_share_and_index_at_five_targets():
    # 28.3% of damage on the main target when an even split would be 20%.
    report = make_report(dps=500_000, priority_dps=141_500)
    cell = parse_cell(report, targets=5)

    assert cell.priority_dps == pytest.approx(141_500)
    assert cell.funnel_share == pytest.approx(0.283)
    assert cell.funnel_index == pytest.approx(1.415)


def test_even_spread_yields_index_of_one():
    report = make_report(dps=400_000, priority_dps=100_000)
    cell = parse_cell(report, targets=4)

    assert cell.funnel_index == pytest.approx(1.0)


def test_pure_single_target_damage_yields_index_of_n():
    """A spec with no AoE at all puts everything on the main target."""
    report = make_report(dps=300_000, priority_dps=300_000)
    cell = parse_cell(report, targets=8)

    assert cell.funnel_share == pytest.approx(1.0)
    assert cell.funnel_index == pytest.approx(8.0)


def test_single_target_cell_has_no_funnel_data():
    """simc omits prioritydps at one target, and the number would be meaningless."""
    report = make_report(dps=300_000)
    cell = parse_cell(report, targets=1)

    assert cell.funnel_share is None
    assert "funnelIndex" not in cell.to_json()


def test_funnel_suppressed_when_scenario_does_not_support_it():
    report = make_report(dps=500_000, priority_dps=200_000)
    cell = parse_cell(report, targets=5, supports_funnel=False)

    assert cell.funnel_index is None


def test_zero_dps_report_is_rejected():
    with pytest.raises(ValueError, match="zero DPS"):
        parse_cell(make_report(dps=0), targets=1)


def test_report_without_players_is_rejected():
    with pytest.raises(ValueError, match="no players"):
        parse_cell({"sim": {"players": []}}, targets=1)


def test_ability_shares_sum_to_one():
    stats = [
        {
            "type": "damage",
            "name": "a",
            "spell_name": "Fireball",
            "id": 1,
            "actual_amount": sample(600.0),
            "num_executes": sample(10),
        },
        {
            "type": "damage",
            "name": "b",
            "spell_name": "Ignite",
            "id": 2,
            "actual_amount": sample(400.0),
            "num_executes": sample(20),
        },
        # Non-damage entries must not dilute the shares.
        {
            "type": "heal",
            "name": "c",
            "spell_name": "Renew",
            "id": 3,
            "actual_amount": sample(999.0),
            "num_executes": sample(1),
        },
    ]
    abilities = extract_abilities({"stats": stats})

    assert [a.name for a in abilities] == ["Fireball", "Ignite"]
    assert sum(a.share for a in abilities) == pytest.approx(1.0)
    assert abilities[0].share == pytest.approx(0.6)


def test_abilities_beyond_the_cap_collapse_into_other():
    stats = [
        {
            "type": "damage",
            "name": f"s{i}",
            "spell_name": f"Spell {i}",
            "id": i,
            "actual_amount": sample(float(100 - i)),
            "num_executes": sample(1),
        }
        for i in range(40)
    ]
    abilities = extract_abilities({"stats": stats})

    assert abilities[-1].name == "Other"
    assert sum(a.share for a in abilities) == pytest.approx(1.0)


def test_pet_damage_is_included():
    player = {
        "stats": [
            {
                "type": "damage",
                "name": "a",
                "spell_name": "Melee",
                "id": 1,
                "actual_amount": sample(500.0),
                "num_executes": sample(10),
            }
        ],
        "stats_pets": {
            "wolf": {
                "stats": [
                    {
                        "type": "damage",
                        "name": "b",
                        "spell_name": "Bite",
                        "id": 2,
                        "actual_amount": sample(500.0),
                        "num_executes": sample(10),
                    }
                ]
            }
        },
    }
    abilities = extract_abilities(player)

    assert {a.name for a in abilities} == {"Melee", "Bite"}
    assert abilities[0].share == pytest.approx(0.5)


def test_timeline_is_truncated_at_shortest_fight():
    """Past the shortest fight, buckets average only the long iterations."""
    collected = {
        "timeline_dmg": {"data": [1000.0] * 360},
        "fight_length": sample(300.0, min=240.0, max=360.0),
    }
    series, bin_size = extract_timeline(collected)

    assert len(series) == 240
    assert bin_size == 1.0


def test_burst_ratio_flags_a_cooldown_window():
    # 240s of steady output with a 20s window doing triple damage.
    series = [1000.0] * 240
    series[30:50] = [3000.0] * 20
    ratio = burst_ratio(series, window=20)

    average = sum(series) / len(series)
    assert ratio == pytest.approx(3000.0 / average)
    assert ratio > 2.0


def test_burst_ratio_of_flat_output_is_one():
    assert burst_ratio([1000.0] * 240, window=20) == pytest.approx(1.0)


def test_burst_ratio_needs_a_full_window():
    assert burst_ratio([1000.0] * 10, window=20) is None


def test_same_spell_split_across_stat_entries_is_merged():
    """simc reports a cast and its cleave component separately; players see one spell."""
    stats = [
        {
            "type": "damage",
            "name": "arcane_barrage",
            "spell_name": "Arcane Barrage",
            "id": 44425,
            "actual_amount": sample(600.0),
            "num_executes": sample(10),
        },
        {
            "type": "damage",
            "name": "arcane_barrage_clearcasting",
            "spell_name": "Arcane Barrage",
            "id": 44426,
            "actual_amount": sample(200.0),
            "num_executes": sample(4),
        },
        {
            "type": "damage",
            "name": "arcane_blast",
            "spell_name": "Arcane Blast",
            "id": 30451,
            "actual_amount": sample(200.0),
            "num_executes": sample(5),
        },
    ]
    abilities = extract_abilities({"stats": stats})

    assert [a.name for a in abilities] == ["Arcane Barrage", "Arcane Blast"]
    assert abilities[0].share == pytest.approx(0.8)
    assert abilities[0].executes == pytest.approx(14)
    assert sum(a.share for a in abilities) == pytest.approx(1.0)
