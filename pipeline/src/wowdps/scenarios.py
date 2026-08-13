"""Simulation scenarios.

A *scenario* is one SimulationCraft fight style plus the sweep of target counts we
run it at. Every (spec, scenario, target_count) triple becomes one simc invocation
and one cell in the dataset.

The three scenarios are chosen so each answers a different question:

``patchwerk``
    Static targets that all live for the whole fight. This is the clean laboratory
    measurement -- with N identical targets standing still, ``prioritydps / dps``
    is an unpolluted read on how much of a spec's throughput it can aim at one
    target. This is the scenario the funnel view is built on.

``hecticaddcleave``
    A boss plus adds that spawn and despawn (driven by simc's own raid events).
    Realistic raid cleave: the funnel number here reflects how well a spec keeps
    priority damage up while adds come and go, including ramp/refresh cost.

``dungeonslice``
    simc's Mythic+ approximation -- roughly a six-minute slice of a M+ dungeon with
    interleaved trash packs, averaging about four targets over its duration. Target
    count is inherent to the fight style, so it is not swept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Target counts swept for the scenarios that support a sweep.
#
# 1..10 covers the range that actually matters in play: 1 (raid single target),
# 2-3 (raid cleave), 4-6 (typical M+ pull), 7-10 (big pull / burst AoE). Beyond 10
# most specs have flat-lined into pure AoE and the extra sims buy nothing.
DEFAULT_TARGET_COUNTS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


@dataclass(frozen=True)
class Scenario:
    """One fight style plus the target counts it is swept over."""

    id: str
    label: str
    description: str
    fight_style: str
    target_counts: tuple[int, ...] = DEFAULT_TARGET_COUNTS
    max_time: int = 300
    #: Extra ``key=value`` simc options appended to every sim of this scenario.
    extra_options: tuple[str, ...] = ()
    #: False when simc's priority-damage accounting does not produce a number we
    #: can interpret as "share landing on the main target".
    supports_funnel: bool = True
    #: Target counts for which we keep the full damage timeline. Timelines are the
    #: bulkiest part of the payload, so we only keep a representative few.
    timeline_at: tuple[int, ...] = (1, 5, 10)

    def sims(self) -> list[int]:
        return list(self.target_counts)


PATCHWERK = Scenario(
    id="patchwerk",
    label="Patchwerk",
    description=(
        "Static targets, all alive for the full fight. The clean laboratory read on "
        "sustained throughput and on how much damage a spec can aim at one target."
    ),
    fight_style="Patchwerk",
    target_counts=DEFAULT_TARGET_COUNTS,
    max_time=300,
)

HECTIC_ADD_CLEAVE = Scenario(
    id="hecticaddcleave",
    label="Hectic Add Cleave",
    description=(
        "A boss plus adds that spawn and despawn throughout the fight. Realistic raid "
        "cleave, including the ramp and refresh cost of moving damage between targets."
    ),
    fight_style="HecticAddCleave",
    # The fight style spawns its own adds via raid events, so desired_targets is the
    # *baseline* count standing there before adds arrive. Sweeping it would stack two
    # independent add sources on top of each other and make the number unreadable.
    target_counts=(1,),
    max_time=300,
    timeline_at=(1,),
)

DUNGEON_SLICE = Scenario(
    id="dungeonslice",
    label="Dungeon Slice (M+)",
    description=(
        "SimulationCraft's Mythic+ approximation: a boss followed by interleaved trash "
        "packs, averaging about four targets over roughly six minutes."
    ),
    fight_style="DungeonSlice",
    target_counts=(1,),
    max_time=360,
    # In DungeonSlice, simc counts damage as "priority" when the target is a *boss*
    # rather than when it is the primary target (see action_t::assess_damage). The
    # number is still meaningful, but it answers "how much lands on bosses" rather
    # than "how much lands on the main target", so we do not surface it as funnel.
    supports_funnel=False,
    timeline_at=(1,),
)

ALL_SCENARIOS: tuple[Scenario, ...] = (PATCHWERK, HECTIC_ADD_CLEAVE, DUNGEON_SLICE)

BY_ID: dict[str, Scenario] = {s.id: s for s in ALL_SCENARIOS}


def get(scenario_id: str) -> Scenario:
    try:
        return BY_ID[scenario_id]
    except KeyError:
        known = ", ".join(sorted(BY_ID))
        raise KeyError(f"unknown scenario {scenario_id!r}; known scenarios: {known}") from None


@dataclass(frozen=True)
class SimSettings:
    """Statistical quality knobs shared by every sim in a run."""

    #: simc stops iterating once the DPS standard error falls below this percentage.
    #: 0.2 keeps spec-to-spec differences of ~0.5% meaningful without runaway runtime.
    #: Set to 0 to run a fixed ``max_iterations`` instead.
    target_error: float = 0.2
    #: Hard ceiling so a badly-converging profile cannot stall the whole matrix.
    max_iterations: int = 30000
    threads: int = 0  # 0 = use every available core
    extra_options: tuple[str, ...] = field(default_factory=tuple)

    def as_simc_options(self) -> list[str]:
        options = [
            f"iterations={self.max_iterations}",
            f"threads={self.threads}",
            # We only consume json2; skip the (very large) HTML report entirely.
            "html=",
        ]
        if self.target_error > 0:
            # Adaptive convergence: iterate until the standard error is small enough.
            # simc rejects deterministic=1 alongside this, so runs carry Monte Carlo
            # noise bounded by target_error -- which we publish as dpsError per cell.
            options.append(f"target_error={self.target_error}")
        else:
            # Fixed iteration count, so seeding can be made reproducible and
            # day-to-day dataset diffs reflect only game and profile changes.
            options.append("deterministic=1")
        options.extend(self.extra_options)
        return options
