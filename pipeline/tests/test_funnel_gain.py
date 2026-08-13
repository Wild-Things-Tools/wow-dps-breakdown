"""Funnel gain versus concentration.

These two numbers are easy to confuse and mean different things, so the cases here
are written to pin the distinction rather than just the arithmetic.

Concentration asks "how is the damage spread across the targets present".
Funnel gain asks "does the main target take more damage *because* the others exist" --
which is what funnelling means in play: damage-over-time effects on adds feeding
resources or procs that get spent on the priority target.
"""

from __future__ import annotations

import pytest

from wowdps.dataset import SpecResult
from wowdps.parse import Cell
from wowdps.profiles import SpecProfile


def profile() -> SpecProfile:
    from pathlib import Path

    return SpecProfile(
        path=Path("/tmp/x.simc"),
        tier="MID2",
        wow_class="Warlock",
        spec="Affliction",
        hero_talent="Hellcaller",
        role="spell",
        talent_hash=None,
    )


def cell(targets: int, dps: float, priority_dps: float | None = None) -> Cell:
    share = priority_dps / dps if priority_dps is not None else None
    return Cell(
        targets=targets,
        dps=dps,
        dps_error=0.2,
        dps_stddev=0.0,
        priority_dps=priority_dps,
        priority_share=share,
        concentration=share * targets if share is not None else None,
        iterations=1000,
        fight_length_mean=300.0,
    )


def result_with(cells: list[Cell]) -> SpecResult:
    result = SpecResult(profile=profile())
    for entry in cells:
        result.add("patchwerk", entry)
    result.compute_funnel_gain()
    return result


def gains(result: SpecResult) -> dict[int, float | None]:
    return {c.targets: c.funnel_gain for c in result.cells["patchwerk"].values()}


def test_adds_feeding_the_main_target_gives_gain_above_one():
    """The real funnel case: boss takes more damage than it would alone."""
    result = result_with([cell(1, 200_000), cell(5, 430_000, priority_dps=212_000)])

    assert gains(result)[5] == pytest.approx(1.06)


def test_area_damage_diluting_the_main_target_gives_gain_below_one():
    """Global cooldowns spent on area damage cost the boss damage."""
    result = result_with([cell(1, 240_000), cell(5, 550_000, priority_dps=130_000)])

    assert gains(result)[5] == pytest.approx(0.5417, rel=1e-3)


def test_high_concentration_does_not_imply_funnelling():
    """A spec with almost no area damage concentrates hard but gains nothing.

    This is the Beast Mastery Hunter shape, and the reason the two numbers are
    reported separately: reading concentration as "funnel" would call this the most
    funnelling build in the game when the boss gains nothing at all from the adds.
    """
    single = cell(1, 230_000)
    five = cell(5, 370_000, priority_dps=228_000)
    result = result_with([single, five])

    assert five.concentration == pytest.approx(3.08, rel=1e-2)  # very concentrated
    assert gains(result)[5] == pytest.approx(0.99, rel=1e-2)  # but no gain


def test_low_concentration_can_still_funnel():
    """The Unholy Death Knight shape: spread damage, but the boss still gains."""
    single = cell(1, 223_000)
    five = cell(5, 650_000, priority_dps=236_000)
    result = result_with([single, five])

    assert five.concentration == pytest.approx(1.82, rel=1e-2)  # far less concentrated
    assert gains(result)[5] == pytest.approx(1.06, rel=1e-2)  # yet it funnels


def test_single_target_cell_has_no_gain():
    """At one target there is no priority damage to compare, so no gain either."""
    result = result_with([cell(1, 200_000)])

    assert gains(result)[1] is None


def test_gain_is_skipped_without_a_single_target_baseline():
    """A scenario that was never run at one target cannot produce a ratio."""
    result = result_with([cell(5, 430_000, priority_dps=212_000)])

    assert gains(result)[5] is None


def test_gain_survives_a_zero_dps_baseline():
    result = result_with([cell(1, 0.0), cell(5, 430_000, priority_dps=212_000)])

    assert gains(result)[5] is None


def test_gain_reaches_the_json_payload():
    result = result_with([cell(1, 200_000), cell(5, 430_000, priority_dps=212_000)])
    payload = result.to_json()["scenarios"]["patchwerk"]["targets"]

    five = next(entry for entry in payload if entry["targets"] == 5)
    assert five["funnelGain"] == pytest.approx(1.06)
    assert five["concentration"] == pytest.approx(2.465, rel=1e-3)
    assert "funnelGain" not in payload[0]  # the single-target cell
