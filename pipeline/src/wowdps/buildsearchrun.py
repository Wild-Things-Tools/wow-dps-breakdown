"""Wiring the search, the repair and the anchor into one pass over a tier.

Everything decidable is in ``buildsearch``, ``talentrepair`` and ``computedbuilds``,
which is why they are testable without simc. This module is the part that needs a
binary and a checkout: it decides which builds to search, where each one's seed comes
from, and what the run may publish.

Where a seed comes from, in the order they are preferred
--------------------------------------------------------
Three sources, and the differences between them are differences of *claim*, so they are
labelled rather than pooled:

1. **simc's own build** (``origin: simc``) -- the tier's shipped hash. Used as the seed
   for a normal run and **withheld entirely from a blind calibration run**, which is
   what makes the calibration mean anything.
2. **A repaired hash** (``origin: simc``, with the repair's caveats) -- for a build simc
   ships and simc's own parser refuses. It is still simc's build; it is simc's build
   with the correction the current trait table forces.
3. **A harvested build** (``origin: harvest``) -- a hash a real player killed a boss
   with, read from ``harvested-builds.json``. Evidence that a build was *played*, never
   that it is optimal, and the display contract keeps the two apart. **No such document
   exists in this repository today**, so this path is wired and unexercised, and a run
   says which of those it was.

**A fourth source is not built, and is described here rather than half-shipped.** Six of
MID2's uncovered (spec, hero tree) cells are specs simc ships a build for on one of
their two trees -- Dark Ranger for both Hunters, Pack Leader for Survival, Fatebound for
Outlaw, Trickster for Subtlety, Diabolist for Demonology. ``talentedit.swap_hero_tree``
moves a build onto another tree, so a transplant looks like a few lines.

It is not, for two measured reasons. It needs a **donor** -- another build of the same
class already playing the target tree -- because ``swap_hero_tree`` without one leaves
the hero tree empty and fourteen points unspent, and filling it is a search over node
*sets*, which is exactly what this project cannot check for legality. Measured against
MID2: donors exist for four of the six cells and **none at all for Dark Ranger**, which
no shipped build plays. And a transplant changes the node set, so ``buildsearch``'s
legality proof does not cover it -- the honest claim would be narrower (the hero nodes
come from a build simc's authors wrote, so they are as legal as that build; the join
with the new class and spec trees is unverified) and would need its own label.

An earlier revision of this module shipped a ``transplant_seeds`` function that nothing
called, while this docstring described transplants as one of four seed sources. That is
the defect this file is otherwise careful about -- a described behaviour the code never
grew into -- so the function is gone and the gap is stated instead.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from . import buildsearch, computedbuilds, gearanchor, simc_runner, talentedit, talentrepair
from . import talenttree as tt
from .buildsearch import Candidate, Measurement, SearchOutcome
from .profiles import SpecProfile
from .scenarios import PATCHWERK, SimSettings

log = logging.getLogger(__name__)


@dataclass
class BuildContext:
    """One build, everything needed to search it, and why it might not be searchable."""

    profile: SpecProfile
    nodes: dict[int, list[tt.Trait]]
    anchor: gearanchor.GearAnchor
    seeds: list[Candidate]
    caveats: list[str]
    #: Set when no search can be run, with the reason a person needs.
    blocked: str | None = None
    repair: talentrepair.Repair | None = None

    @property
    def base_talents(self) -> str | None:
        """A hash simc will load, for the base actor every invocation builds.

        None when the profile's own hash is fine -- simc then reads it from the file, as
        it always has. Set to the repaired hash when it is not, because simc builds the
        base actor before it generates any profileset and exits 81 if it cannot.
        """
        return self.repair.repaired_hash if self.repair and self.repair.ok else None


def _key(prefix: str, index: int) -> str:
    """A profileset name simc's option parser will not choke on."""
    return f"{prefix}{index:02d}"


def prepare(
    profile: SpecProfile,
    *,
    traits: list[tt.Trait],
    target: gearanchor.AnchorTarget,
    budget: talentedit.PointBudget,
    framing: tuple[int, int],
    blind: bool,
    seed_value: int,
    harvested: dict[str, list[dict]] | None = None,
) -> BuildContext:
    """Everything one build needs before a single simulation is run.

    Deliberately does no I/O beyond reading the profile's own gear lines, so a run can
    report what it *would* do -- which seeds, which repairs, which blocked builds --
    without a binary. ``wowdps build-search --plan`` is that report.
    """
    nodes = tt.nodes_for_class(traits, tt.CLASS_IDS[profile.wow_class])
    anchor = gearanchor.apply(target, gearanchor.read_kit(profile.path))
    caveats: list[str] = []
    seeds: list[Candidate] = []
    repair: talentrepair.Repair | None = None

    if not profile.talent_hash:
        return BuildContext(
            profile=profile,
            nodes=nodes,
            anchor=anchor,
            seeds=[],
            caveats=caveats,
            blocked="the profile carries no talent hash, so there is nothing to search from",
        )

    try:
        loadout = tt.decode_loadout(profile.talent_hash, nodes)
    except tt.TalentDecodeError:
        repair = talentrepair.repair(profile.id, profile.talent_hash, nodes, budget, framing)
        if not repair.ok or repair.loadout is None:
            return BuildContext(
                profile=profile,
                nodes=nodes,
                anchor=anchor,
                seeds=[],
                caveats=caveats,
                blocked=repair.refused or "the talent hash cannot be read",
                repair=repair,
            )
        loadout = repair.loadout
        caveats.extend(repair.caveats())
    else:
        # It decoded -- but simc may still refuse it on the spec rule, which is a
        # different failure with the same consequence: no number anywhere.
        if tt.spec_rule_violation(loadout, nodes):
            repair = talentrepair.repair(profile.id, profile.talent_hash, nodes, budget, framing)
            if not repair.ok or repair.loadout is None:
                return BuildContext(
                    profile=profile,
                    nodes=nodes,
                    anchor=anchor,
                    seeds=[],
                    caveats=caveats,
                    blocked=repair.refused or "simc refuses the talent hash",
                    repair=repair,
                )
            loadout = repair.loadout
            caveats.extend(repair.caveats())

    rng = random.Random(seed_value) if blind else None
    seeds.append(
        buildsearch.seed_from(
            key=_key("s", 0),
            label=("scrambled start" if blind else profile.display_name),
            # A blind seed is no longer simc's build -- it is a build with simc's node
            # set and none of simc's choices -- so calling its origin `simc` would
            # attribute a scramble to simc. The evidence block says where it came from.
            origin=buildsearch.ORIGIN_SEARCH if blind else buildsearch.ORIGIN_SIMC,
            loadout=loadout,
            nodes=nodes,
            rng=rng,
        )
    )

    for index, entry in enumerate(_harvested_for(harvested, profile), start=1):
        seeds.append(entry_to_seed(entry, nodes, key=_key("h", index)))

    return BuildContext(
        profile=profile,
        nodes=nodes,
        anchor=anchor,
        seeds=[seed for seed in seeds if seed is not None],
        caveats=caveats,
        repair=repair,
    )


def _harvested_for(harvested: dict[str, list[dict]] | None, profile: SpecProfile) -> list[dict]:
    if not harvested:
        return []
    return harvested.get(profile.spec_id, [])


def entry_to_seed(entry: dict, nodes: dict[int, list[tt.Trait]], *, key: str) -> Candidate | None:
    """One harvested build as a seed, or None when its hash will not read.

    A harvested hash that does not decode is dropped **here** rather than at the
    simulation, and dropped loudly: it is the most valuable thing a harvest can turn up
    -- either the loadout format moved or the bit reader is out of step -- and a silent
    skip presents a thin harvest as a complete one.
    """
    talent_hash = entry.get("talentHash")
    if not talent_hash:
        return None
    try:
        loadout = tt.decode_loadout(talent_hash, nodes)
    except tt.TalentDecodeError as error:
        log.warning("harvested build %s will not decode: %s", entry.get("buildKey"), error)
        return None
    if tt.spec_rule_violation(loadout, nodes):
        log.warning("harvested build %s breaks simc's spec rule", entry.get("buildKey"))
        return None
    seen = entry.get("seenInKills")
    return Candidate(
        key=key,
        label=f"harvested, seen in {seen} kill(s)" if seen else "harvested",
        origin=buildsearch.ORIGIN_HARVEST,
        loadout=loadout,
        talent_hash=talent_hash,
    )


def read_harvested(path: Path) -> dict[str, list[dict]]:
    """``harvested-builds.json`` grouped by the build ids this pipeline joins on.

    The document is ``harvest.py``'s and is not touched here. Only ``specId`` and
    ``builds[].talentHash`` are read, both of which that module already emits.
    """
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict]] = {}
    for row in document.get("specs") or []:
        spec_id = row.get("specId")
        if not spec_id:
            continue
        grouped.setdefault(spec_id, []).extend(row.get("builds") or [])
    return grouped


def measure(
    simc: Path,
    context: BuildContext,
    settings: SimSettings,
    candidates: list[Candidate],
    iterations: int,
    targets: int,
    timeout: int,
    gear: tuple[str, ...] | None = None,
) -> dict[str, Measurement]:
    """One field, measured once. Used for the head-to-head that follows a search.

    ``gear`` defaults to the build's anchored kit, which is what a search must use:
    within one spec's search the gear has to be byte-identical or a talent difference
    worth 2-3% sits underneath a kit difference of the same size. Passing ``()``
    measures on **simc's own shipped kit** instead, with only ``talents=`` varying --
    a different question, and the one `projectioncheck` asks. Nothing else should
    pass it: an anchored number and a shipped-gear number are not comparable, which
    is the whole reason that module exists.
    """
    request = simc_runner.SimRequest(profile=context.profile, scenario=PATCHWERK, targets=targets)
    runner = buildsearch.simc_runner(
        simc,
        request,
        settings,
        context.anchor.options() if gear is None else gear,
        timeout=timeout,
        base_talents=context.base_talents,
    )
    return runner(candidates, iterations)


def run_build(
    simc: Path,
    context: BuildContext,
    settings: SimSettings,
    *,
    targets: int,
    breadth: int,
    seed_value: int,
    blind: bool,
    timeout: int,
    rounds: tuple[buildsearch.Round, ...] | None = None,
    climb_steps: int = buildsearch.CLIMB_STEPS,
) -> SearchOutcome:
    """Search one build, on its own anchored kit, at one target count."""
    request = simc_runner.SimRequest(profile=context.profile, scenario=PATCHWERK, targets=targets)
    runner = buildsearch.simc_runner(
        simc,
        request,
        settings,
        context.anchor.options(),
        timeout=timeout,
        base_talents=context.base_talents,
    )
    return buildsearch.search(
        spec_id=context.profile.spec_id,
        build_id=context.profile.id,
        seeds=context.seeds,
        nodes=context.nodes,
        runner=runner,
        breadth=breadth,
        seed_value=seed_value,
        blind=blind,
        rounds=rounds,
        climb_steps=climb_steps,
    )


def calibration_row(
    context: BuildContext,
    outcome: SearchOutcome,
    measured: dict[str, Measurement],
    simc_key: str,
) -> computedbuilds.CalibrationRow | None:
    """One head-to-head, both sides from the *same* invocation.

    Both numbers have to come out of one run of one field, because that is what makes
    the difference exact: two profilesets with identical options return bit-identical
    DPS, and a profileset returns the same number whichever others share its run -- but
    only a shared *iteration count* makes the two errors comparable, and only comparable
    errors make the tie band mean anything.
    """
    reference = measured.get(simc_key)
    if reference is None:
        return None
    best = outcome.best
    found = measured.get(best[0].key) if best else None
    recovered = bool(
        best and reference is not None and best[0].nodes_taken and _same_build(best[0], context)
    )
    return computedbuilds.CalibrationRow(
        build_id=context.profile.id,
        label=context.profile.display_name,
        simc=reference,
        found=found,
        variants_evaluated=outcome.variants_evaluated,
        recovered_simc_build=recovered,
    )


def _same_build(candidate: Candidate, context: BuildContext) -> bool:
    """Whether the search's winner is simc's own build, selection for selection."""
    if not context.profile.talent_hash:
        return False
    try:
        original = tt.decode_loadout(context.profile.talent_hash, context.nodes)
    except tt.TalentDecodeError:
        return False
    return buildsearch.fingerprint(candidate.loadout) == buildsearch.fingerprint(original)
