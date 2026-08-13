"""Simulation scenarios.

A *scenario* is one SimulationCraft fight style plus the sweep of target counts we
run it at. Every (spec, scenario, target_count) triple becomes one simc invocation
and one cell in the dataset.

The three scenarios are chosen so each answers a different question:

``patchwerk``
    Static targets that all live for the whole fight. This is the clean laboratory
    measurement: with N identical targets standing still, comparing ``prioritydps``
    against the same profile's single-target DPS is an unpolluted read on whether the
    extra targets help or hurt the main target. This is the scenario the funnel view
    is built on, and the only one swept across target counts -- funnel gain needs the
    single-target baseline to divide by.

``addwaves``
    A boss that stands for the whole fight plus waves of adds that arrive, soak
    damage and leave. This is the scenario funnelling is actually about: the boss is
    the target that matters, the adds are optional damage, and the question is
    whether having them up makes the boss die faster or slower.

    Built here rather than taken from a simc fight style, because the built-in add
    styles bundle movement events -- which would mix add pressure together with
    movement downtime and make the comparison against single target meaningless.
    Its funnel baseline is Patchwerk at one target: same profile, same fight length,
    same (default) fight style, no adds. That is a controlled comparison.

``hecticaddcleave``
    simc's own boss-plus-adds style. Kept for reference, but it layers movement
    events on top of the adds, so it reports the main-target share without a funnel
    gain -- there is no add-free run with the same movement to divide by.

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
    #: simc fight style, or None to leave simc on its default (Patchwerk).
    #:
    #: Passing a fight style is NOT harmless when the scenario supplies its own
    #: raid events: ``sim_t::init_fight_style`` calls ``raid_events_str.clear()``
    #: for Patchwerk, which wipes them -- with ``raid_events=`` and ``+=`` alike.
    #: Scenarios that build their own encounter must leave this None.
    fight_style: str | None
    target_counts: tuple[int, ...] = DEFAULT_TARGET_COUNTS
    max_time: int = 300
    #: Extra ``key=value`` simc options appended to every sim of this scenario.
    extra_options: tuple[str, ...] = ()
    #: False when simc's priority-damage accounting does not produce a number we
    #: can interpret as "share landing on the main target".
    supports_funnel: bool = True
    #: Which scenario's single-target cell funnel gain divides by.
    #:
    #: "self" uses this scenario's own 1-target run, which is right when the sweep
    #: itself varies the target count. Naming another scenario borrows its baseline,
    #: which is what a scenario whose extra targets come from raid events needs --
    #: it has no add-free cell of its own. None means gain is not computable here.
    funnel_baseline: str | None = "self"
    #: Target counts for which we keep the full damage timeline. Timelines are the
    #: bulkiest part of the payload, so we only keep a representative few.
    timeline_at: tuple[int, ...] = (1, 5, 10)

    def sims(self) -> list[int]:
        return list(self.target_counts)

    def command_options(self) -> list[str]:
        """simc options that define this scenario, fight style included if set."""
        options = []
        if self.fight_style:
            options.append(f"fight_style={self.fight_style}")
        options.extend(self.extra_options)
        return options


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

# Roughly a third of the fight has adds up: five adds arrive every 60 seconds and
# stay for 20. Long enough to be worth global cooldowns, transient enough that the
# boss is still the target that matters.
ADD_WAVES = Scenario(
    id="addwaves",
    label="Add Waves",
    description=(
        "A boss for the whole fight plus waves of five adds arriving every minute and "
        "staying twenty seconds. The scenario funnelling is actually about: does having "
        "adds up make the boss die faster, or does the area damage cost it?"
    ),
    # Deliberately no fight style: naming one makes simc clear the raid events below.
    fight_style=None,
    target_counts=(1,),
    max_time=300,
    extra_options=("raid_events+=/adds,count=5,first=20,cooldown=60,duration=20",),
    # No add-free cell of its own, so gain divides by Patchwerk at one target --
    # identical settings apart from the adds.
    funnel_baseline="patchwerk",
    timeline_at=(1,),
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
    # The style also injects movement events (25 yards on the add cycle, 8 yards
    # throughout). Dividing its main-target damage by a stationary single-target run
    # would attribute movement downtime to the adds, so no gain is reported here --
    # addwaves exists to answer that question cleanly.
    funnel_baseline=None,
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
    # than "how much lands on the main target", so neither funnel gain nor
    # concentration would mean what they mean elsewhere.
    supports_funnel=False,
    timeline_at=(1,),
)

ALL_SCENARIOS: tuple[Scenario, ...] = (PATCHWERK, ADD_WAVES, HECTIC_ADD_CLEAVE, DUNGEON_SLICE)

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

    #: Adaptive mode: simc stops once the DPS standard error falls below this
    #: percentage. Zero -- the default -- switches to a fixed iteration count with
    #: deterministic seeding instead.
    #:
    #: Determinism is worth more here than adaptive convergence. Every published run
    #: is committed, so with adaptive sampling each nightly run differs by Monte Carlo
    #: noise even when nothing changed in the game, and real changes drown in it. With
    #: fixed seeding a quiet night produces byte-identical output and no commit at all,
    #: so a diff in the history always means something actually moved. Verified: two
    #: separate runs of the same profile return bit-identical DPS.
    target_error: float = 0.0
    #: Fixed iteration count in deterministic mode; a ceiling in adaptive mode.
    #:
    #: 3000 measures at roughly 0.05% standard error, which is about six times tighter
    #: than the 0.3% adaptive runs this replaced, for around nine seconds per cell.
    #: 10000 buys 0.03% for three times the runtime -- not worth it.
    max_iterations: int = 3000
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
