"""Wiring the search, the repair and the anchor into one pass over a tier.

Everything decidable is in ``buildsearch``, ``talentrepair`` and ``computedbuilds``,
which is why they are testable without simc. This module is the part that needs a
binary and a checkout: it decides which builds to search, where each one's seed comes
from, and what the run may publish.

Where a seed comes from, in the order they are preferred
--------------------------------------------------------
Four sources, and the differences between them are differences of *claim*, so they are
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
4. **A hero-tree transplant** (``origin: search``) -- for a (spec, hero tree) cell simc
   ships no build for, the spec's own shipped build moved onto the missing tree with the
   hero spend of a *donor*: another build of the same class that plays that tree. Only
   attempted where a donor exists in the tier, because ``swap_hero_tree`` without one
   leaves the hero tree empty and fourteen points unspent, and filling it is a search
   over node sets that this project cannot check for legality.

The legality argument is weakest here and it is stated where it is weakest: a
transplant changes the node set, so the module docstring's proof in ``buildsearch`` does
**not** cover it. What can be said is narrower and is said instead -- the hero nodes come
from a build simc's authors wrote for that tree, so they are as internally legal as that
build, and the join between them and the new class and spec trees is unverified.
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


def transplant_seeds(
    profile: SpecProfile,
    nodes: dict[int, list[tt.Trait]],
    sub_tree: int,
    donors: list[tt.Loadout],
    *,
    key: str,
) -> Candidate | None:
    """The spec's build moved onto a hero tree it ships no build for.

    Refuses without a donor rather than producing a build with fourteen hero points
    unspent: ``swap_hero_tree``'s own docstring says filling the new tree is a search
    and not an edit, and a search over which hero nodes to take is a search over node
    sets -- the one thing this project cannot check for legality.
    """
    if not donors:
        return None
    if not profile.talent_hash:
        return None
    try:
        loadout = tt.decode_loadout(profile.talent_hash, nodes)
        moved = talentedit.swap_hero_tree(loadout, nodes, sub_tree, donor=donors[0])
        talent_hash = tt.encode_loadout(moved, nodes, preserve_framing=False)
    except (tt.TalentDecodeError, tt.TalentEncodeError, talentedit.TalentEditError) as error:
        log.warning("%s cannot be moved onto sub tree %d: %s", profile.id, sub_tree, error)
        return None
    return Candidate(
        key=key,
        label=f"{profile.display_name} moved onto sub tree {sub_tree}",
        origin=buildsearch.ORIGIN_SEARCH,
        loadout=moved,
        talent_hash=talent_hash,
        lineage=(f"hero tree -> {sub_tree}",),
    )


def measure(
    simc: Path,
    context: BuildContext,
    settings: SimSettings,
    candidates: list[Candidate],
    iterations: int,
    targets: int,
    timeout: int,
) -> dict[str, Measurement]:
    """One field, measured once. Used for the head-to-head that follows a search."""
    request = simc_runner.SimRequest(profile=context.profile, scenario=PATCHWERK, targets=targets)
    runner = buildsearch.simc_runner(
        simc, request, settings, context.anchor.options(), timeout=timeout
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
        simc, request, settings, context.anchor.options(), timeout=timeout
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
