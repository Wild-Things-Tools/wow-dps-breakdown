"""Comparing a spec's builds with the gear held still.

The dataset's spec rows answer "which build should I play": each hero build on the
gear SimulationCraft's authors picked for it, which is the right comparison for
somebody choosing what to play. It is *not* the right comparison for "what do these
talents do", because simc's shipped builds differ in gear as well as talents --
MID2's Arcane Spellslinger and Sunfury carry different rings and noticeably
different secondaries, and the gap between the shipped Sunfury profile and a
talents-only profileset built from it is 1.3%, far outside the 0.13% error. Neither
number is wrong; they answer different questions.

This module asks the second one. One base profile, one gear set, and one profileset
per build carrying only that build's talent hash. The APLs are identical across a
spec's hero builds -- they branch on ``talent.`` internally -- so swapping the hash
under one APL is legitimate, and was confirmed by diffing MID2's two Arcane profiles.

Two rankings from one run
-------------------------
``profileset_metric`` takes a list, so the same sweep reports each variant's total
damage *and* its damage to the priority target. That is the "funnel ceiling"
question and the "which build" question answered by one set of sims rather than two:

* ranked by ``dps``          -- which build does the most damage overall
* ranked by ``prioritydps``  -- which build puts the most damage on the boss

They disagree whenever a build buys its total with area damage, which is exactly
what somebody choosing a build for a specific fight needs to know.

What this is not
----------------
It is **not** an optimiser and simc is not one either: simc runs a fixed action list
and reports the result. ``profileset_metric=prioritydps`` selects which number is
reported and ranked by, nothing more. So this measures the builds simc ships, at the
target counts asked for -- not the best build that could exist.

It also does not sweep *talent permutations within* a build. That needs hashes
nobody has generated; the hero builds are the axis simc hands us for free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import simc_runner
from .profiles import SpecProfile
from .scenarios import PATCHWERK, SimSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildVariant:
    """One build's talents, to be worn by somebody else's character."""

    #: The profileset key: the build's dataset id, so results join to the rest.
    key: str
    label: str
    hero_talent: str
    talent_hash: str


@dataclass
class BuildMeasurement:
    key: str
    label: str
    hero_talent: str
    dps: float
    dps_error: float
    priority_dps: float | None
    iterations: int

    def to_json(self) -> dict:
        return {
            "id": self.key,
            "label": self.label,
            "heroTalent": self.hero_talent,
            "dps": round(self.dps, 1),
            "dpsError": round(self.dps_error, 4),
            "priorityDps": None if self.priority_dps is None else round(self.priority_dps, 1),
            "iterations": self.iterations,
        }


@dataclass
class SweepResult:
    """One spec's builds, on one character, at one target count."""

    spec_id: str
    spec_label: str
    #: The profile whose gear and action list every variant wore.
    base_profile_id: str
    targets: int
    builds: list[BuildMeasurement]

    def ranked_by(self, metric: str) -> list[BuildMeasurement]:
        if metric == "prioritydps":
            with_priority = [b for b in self.builds if b.priority_dps is not None]
            return sorted(with_priority, key=lambda b: -(b.priority_dps or 0.0))
        return sorted(self.builds, key=lambda b: -b.dps)

    def to_json(self) -> dict:
        by_dps = self.ranked_by("dps")
        by_priority = self.ranked_by("prioritydps")
        return {
            "specId": self.spec_id,
            "label": self.spec_label,
            "baseProfile": self.base_profile_id,
            "targets": self.targets,
            "builds": [build.to_json() for build in self.builds],
            "bestByDps": by_dps[0].key if by_dps else None,
            "bestByPriorityDps": by_priority[0].key if by_priority else None,
            # The interesting case: the build that does the most damage is not the
            # build that puts the most on the boss. Stated rather than left to a
            # reader comparing two lists.
            "rankingsDisagree": bool(
                by_dps and by_priority and by_dps[0].key != by_priority[0].key
            ),
            "note": (
                "One character's gear and action list, wearing each build's talents in "
                "turn. This is what the talents do, not which shipped profile is "
                "strongest -- simc's builds differ in gear too."
            ),
        }


def variants_for(profiles: list[SpecProfile]) -> list[BuildVariant]:
    """Build variants from a spec's profiles, skipping any with no talent hash.

    A profile with no hash cannot be expressed as a profileset -- there is nothing
    to swap -- and silently dropping it would make the sweep look complete when a
    build is missing from it. The caller reports the difference.
    """
    variants: list[BuildVariant] = []
    for profile in profiles:
        if not profile.talent_hash:
            continue
        variants.append(
            BuildVariant(
                key=profile.id,
                label=profile.display_name,
                hero_talent=profile.hero_label,
                talent_hash=profile.talent_hash,
            )
        )
    return variants


def choose_base(profiles: list[SpecProfile]) -> SpecProfile:
    """Whose gear everybody wears.

    The first profile in a stable order, and which one it is does not matter to the
    comparison as long as it is the same for every variant -- that is the whole
    point of holding gear still. It matters to the *absolute* numbers, so it is
    published alongside them.
    """
    return sorted(profiles, key=lambda p: p.id)[0]


def sweep_spec(
    simc: Path,
    profiles: list[SpecProfile],
    settings: SimSettings,
    targets: int = 1,
    timeout: int = 1800,
) -> SweepResult | None:
    """Run one spec's builds against each other on one character."""
    variants = variants_for(profiles)
    if len(variants) < 2:
        log.info(
            "%s has %d build(s) with a talent hash: nothing to compare",
            profiles[0].spec_id if profiles else "?",
            len(variants),
        )
        return None

    base = choose_base(profiles)
    request = simc_runner.SimRequest(profile=base, scenario=PATCHWERK, targets=targets)
    sets = [
        simc_runner.Profileset(key=variant.key, options=(f"talents={variant.talent_hash}",))
        for variant in variants
    ]
    log.info(
        "%s: %d builds on %s's gear at %d target(s)",
        base.spec_id,
        len(variants),
        base.id,
        targets,
    )
    measured = simc_runner.run_profilesets(simc, request, settings, sets, timeout=timeout)

    builds = [
        BuildMeasurement(
            key=variant.key,
            label=variant.label,
            hero_talent=variant.hero_talent,
            dps=result.dps,
            dps_error=result.dps_error,
            priority_dps=result.priority_dps,
            iterations=result.iterations,
        )
        for variant in variants
        if (result := measured.get(variant.key)) is not None
    ]
    if not builds:
        log.error("%s: no profileset returned a result", base.spec_id)
        return None

    return SweepResult(
        spec_id=base.spec_id,
        spec_label=f"{base.spec} {base.wow_class}",
        base_profile_id=base.id,
        targets=targets,
        builds=builds,
    )
