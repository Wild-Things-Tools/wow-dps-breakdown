"""Distilling a SimulationCraft json2 report into the metrics the site shows.

Everything rests on ``prioritydps``, which simc accumulates in
``action_t::assess_damage``: every damage event whose target is the sim's primary
target adds to ``priority_iteration_dmg``. simc only emits the field when more than
one enemy is present, so single-target cells carry none (trivially, all of it lands
on the only target).

Two *different* questions come out of that number, and conflating them is a mistake:

**Concentration** -- ``prioritydps / dps x N``. How the damage is distributed across
the targets that are there. 1.0 means an even spread, N means everything lands on the
main target. A spec with no area damage at all scores N here, which says nothing
about whether extra targets helped it.

**Funnel gain** -- ``prioritydps(N) / dps(1)``. Whether the main target actually takes
*more* damage because the other targets exist. This is what "funnel" means in play:
damage-over-time effects on adds feeding resources or procs that get spent on the
priority target. Above 1.0 the adds are making the boss die faster; below 1.0 the
global cooldowns spent on area damage are costing the boss damage.

They come apart badly. Beast Mastery Hunter concentrates hard (about 3.1x an even
spread at five targets) while gaining nothing on the boss (0.99x its single-target
damage) -- it simply has little area damage to dilute with. Unholy Death Knight
concentrates far less (1.8-2.0x) but genuinely funnels (1.05-1.06x). Only the second
number answers "do I want adds up when I need the boss dead".

Funnel gain needs the single-target baseline, which one report does not contain, so it
is computed in ``dataset.py`` once a spec's whole target sweep is in hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How many abilities to keep per cell. The tail past ~20 is rounding noise and
# would dominate the payload size.
MAX_ABILITIES = 20

# Width of the rolling window used for the burst metric, in seconds. Twenty seconds
# is roughly the length of a major damage cooldown window.
BURST_WINDOW = 20


def _sample(node: dict | None, key: str = "mean") -> float:
    """Pull a value out of a simc sample-data node, tolerating absent fields.

    simc omits zero-valued fields entirely (``add_non_zero``), so a missing node
    genuinely means zero rather than a parse failure.
    """
    if not node:
        return 0.0
    value = node.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


@dataclass
class Ability:
    name: str
    spell_id: int | None
    amount: float
    share: float
    executes: float

    def to_json(self) -> dict:
        out: dict = {
            "name": self.name,
            "share": round(self.share, 5),
            "executes": round(self.executes, 2),
        }
        if self.spell_id:
            out["id"] = self.spell_id
        return out


@dataclass
class Cell:
    """One (spec, scenario, target count) measurement."""

    targets: int
    dps: float
    dps_error: float
    dps_stddev: float
    priority_dps: float | None
    #: Fraction of total damage landing on the main target.
    priority_share: float | None
    #: priority_share x targets. 1.0 = even spread, N = everything on the main target.
    concentration: float | None
    iterations: int
    fight_length_mean: float
    abilities: list[Ability] = field(default_factory=list)
    timeline: list[float] | None = None
    timeline_bin: float = 1.0
    burst_ratio: float | None = None
    #: priority_dps / single-target dps. Filled in by dataset.py, which is the first
    #: place that has the whole target sweep and therefore the baseline.
    funnel_gain: float | None = None

    def to_json(self) -> dict:
        out: dict = {
            "targets": self.targets,
            "dps": round(self.dps, 1),
            "dpsError": round(self.dps_error, 4),
            "dpsStddev": round(self.dps_stddev, 1),
            "iterations": self.iterations,
            "fightLength": round(self.fight_length_mean, 1),
            "abilities": [a.to_json() for a in self.abilities],
        }
        if self.priority_dps is not None:
            out["priorityDps"] = round(self.priority_dps, 1)
            out["priorityShare"] = round(self.priority_share or 0.0, 5)
        # Concentration is absent, not zero, when the target count is not fixed --
        # writing 0.0 there would render as "0.00x an even spread", which is false.
        if self.concentration is not None:
            out["concentration"] = round(self.concentration, 4)
        if self.funnel_gain is not None:
            out["funnelGain"] = round(self.funnel_gain, 4)
        if self.timeline is not None:
            out["timeline"] = [round(v, 1) for v in self.timeline]
            out["timelineBin"] = self.timeline_bin
        if self.burst_ratio is not None:
            out["burstRatio"] = round(self.burst_ratio, 4)
        return out


def extract_abilities(player: dict) -> list[Ability]:
    """Top damaging abilities as a share of total damage.

    Uses each stat entry's own ``actual_amount`` rather than ``compound_amount``:
    compound amounts roll child actions into their parent, so summing them would
    double-count and the shares would not add to 1.
    """
    entries: list[tuple[str, int | None, float, float]] = []

    def collect(stats: list[dict]) -> None:
        for stat in stats:
            if stat.get("type") != "damage":
                continue
            amount = _sample(stat.get("actual_amount"))
            if amount <= 0:
                continue
            name = stat.get("spell_name") or stat.get("name") or "unknown"
            spell_id = stat.get("id")
            executes = _sample(stat.get("num_executes"))
            entries.append((str(name), spell_id, amount, executes))

    collect(player.get("stats") or [])

    # Pet damage is reported separately but is unambiguously part of the spec's
    # output. simc keys it by pet name, and the value is either the stat list itself
    # or a node wrapping one, depending on the pet type.
    pets = player.get("stats_pets")
    pet_groups = pets.values() if isinstance(pets, dict) else (pets or [])
    for group in pet_groups:
        if isinstance(group, list):
            collect(group)
        elif isinstance(group, dict):
            collect(group.get("stats") or [])

    total = sum(e[2] for e in entries)
    if total <= 0:
        return []

    # simc splits one player-visible spell across several stat entries (a cast and
    # its cleave or proc component each get their own). Players think in spells, so
    # merge by name -- otherwise the breakdown lists "Arcane Barrage" twice and the
    # reader has to add the rows up themselves.
    merged: dict[str, tuple[int | None, float, float]] = {}
    for name, spell_id, amount, executes in entries:
        previous = merged.get(name)
        if previous:
            merged[name] = (
                previous[0] or spell_id,
                previous[1] + amount,
                previous[2] + executes,
            )
        else:
            merged[name] = (spell_id, amount, executes)

    ordered = sorted(
        ((name, *values) for name, values in merged.items()),
        key=lambda e: -e[2],
    )
    top = ordered[:MAX_ABILITIES]

    abilities = [
        Ability(name=name, spell_id=spell_id, amount=amount, share=amount / total, executes=ex)
        for name, spell_id, amount, ex in top
    ]

    remainder = total - sum(a.amount for a in abilities)
    if remainder > total * 0.001:
        abilities.append(
            Ability(
                name="Other",
                spell_id=None,
                amount=remainder,
                share=remainder / total,
                executes=0.0,
            )
        )
    return abilities


def extract_timeline(collected: dict, bin_size: float = 1.0) -> tuple[list[float], float] | None:
    """Damage-per-second curve, truncated to where every iteration is still alive.

    simc varies fight length per iteration (default +/-20%), and each timeline bucket
    is the mean over the iterations that actually reached it. Past the shortest fight
    that mean is drawn from a biased subset (only the long fights), so we cut the
    curve at ``fight_length.min``. No distributional assumption needed -- before that
    point every iteration contributes to every bucket.
    """
    timeline = collected.get("timeline_dmg")
    if not timeline:
        return None
    data = timeline.get("data")
    if not isinstance(data, list) or not data:
        return None

    cutoff = int(_sample(collected.get("fight_length"), "min"))
    if cutoff <= 0:
        cutoff = len(data)
    series = [float(v) for v in data[:cutoff]]
    if not series:
        return None

    if bin_size > 1:
        width = int(bin_size)
        binned = [
            sum(series[i : i + width]) / len(series[i : i + width])
            for i in range(0, len(series), width)
        ]
        return binned, float(width)

    return series, 1.0


def burst_ratio(series: list[float], window: int = BURST_WINDOW) -> float | None:
    """Peak sustained damage over ``window`` seconds, relative to the fight average.

    1.0 means perfectly flat output; 2.0 means the best 20-second stretch does twice
    the spec's average damage. This is the number that separates "big cooldown
    window" specs from steady ones.
    """
    if len(series) < window:
        return None
    average = sum(series) / len(series)
    if average <= 0:
        return None

    running = sum(series[:window])
    best = running
    for i in range(window, len(series)):
        running += series[i] - series[i - window]
        best = max(best, running)
    return (best / window) / average


def parse_cell(
    report: dict,
    targets: int,
    *,
    supports_funnel: bool = True,
    with_timeline: bool = False,
    timeline_bin: float = 1.0,
) -> Cell:
    """Turn one json2 report into one dataset cell."""
    sim = report.get("sim") or {}
    players = sim.get("players") or []
    if not players:
        raise ValueError("simc report contains no players")
    player = players[0]
    collected = player.get("collected_data") or {}

    dps = _sample(collected.get("dps"))
    if dps <= 0:
        raise ValueError("simc report has zero DPS -- profile or scenario is broken")

    priority_dps: float | None = None
    priority_share: float | None = None
    concentration: float | None = None

    if supports_funnel:
        # simc emits prioritydps whenever enemy_targets > 1, which includes targets
        # that arrive from raid events rather than from desired_targets. Gating on
        # the configured count would silently drop those scenarios, so trust the
        # field's presence instead.
        raw_priority = collected.get("prioritydps")
        if raw_priority:
            priority_dps = _sample(raw_priority)
            priority_share = priority_dps / dps
            if targets > 1:
                # 1.0 = damage spread evenly across targets; N = everything on the
                # main target. Only meaningful when the target count is fixed and
                # known -- with adds arriving and leaving there is no N to divide by.
                # Distribution only; see the module docstring for why that is not
                # the same thing as funnelling.
                concentration = priority_share * targets

    timeline: list[float] | None = None
    bin_size = 1.0
    burst: float | None = None
    if with_timeline:
        extracted = extract_timeline(collected, timeline_bin)
        if extracted:
            timeline, bin_size = extracted
            burst = burst_ratio(timeline) if bin_size == 1.0 else None

    return Cell(
        targets=targets,
        dps=dps,
        dps_error=_sample(collected.get("dps"), "mean_std_dev") / dps * 100 if dps else 0.0,
        dps_stddev=_sample(collected.get("dps"), "std_dev"),
        priority_dps=priority_dps,
        priority_share=priority_share,
        concentration=concentration,
        # dps.count is how many iterations actually ran, which differs from the
        # configured ceiling whenever target_error converges early.
        iterations=int(_sample(collected.get("dps"), "count")),
        fight_length_mean=_sample(collected.get("fight_length")),
        abilities=extract_abilities(player),
        timeline=timeline,
        timeline_bin=bin_size,
        burst_ratio=burst,
    )
