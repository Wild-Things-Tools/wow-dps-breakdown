"""Comparing gear the way a loot council has to: against what the character wears.

The measurement is three steps, and each one is a choice that decides what the
number means. All three are spelled out here because a "trinket gain" figure with
the wrong baseline behind it is worse than no figure.

**1. Find the baseline.** The sockets are filled *every possible way* from the
Mythic+ pool and each combination is run, so the baseline is the pair that actually
won rather than the two items that ranked highest alone. Those are different
questions: standalone value was measured to be additive to about 3% on trinkets,
which ranks a clear winner and is not good enough at the cut, and the first
exhaustive ring sweep found the two methods naming a different pair on **13 of 26
builds**. ``MAX_BASELINE_COMBINATIONS`` is the ceiling; a pool too large for it
falls back to the additive rule and the result says which method produced it.

Every item is *also* run alone -- in the first socket, with the others empty --
against a run with every socket empty. That number is no longer what picks the
baseline, but it is what makes a close call at the cut legible to a reader, and an
empty partner is the neutral partner: it cannot share an on-use window or feed a
stat buff, so it measures the item rather than the pairing.

**2. Price the baseline.** Both baseline items are worn at the *lower* of the two
item levels. Mythic+ gear tops out below Mythic raid gear, and pricing the thing
being compared against at the raid's top level would flatter every raid drop.

**3. Judge each raid candidate.** Each is put in the socket holding the *second*
best baseline item, at each item level, and the whole set of results is one simc
run. Replacing the weaker of the two is the actual decision -- nobody asks whether a
drop beats their best trinket while keeping their worst.

**4. Name the ceiling.** Steps 1 and 3 together are a two-step search -- best
Mythic+ pair, then one raid drop into it -- and a two-step search can miss the
optimum. The best pair overall may hold a raid ring beside a Mythic+ ring that is
*not* in the best Mythic+ pair, and neither step would ever propose it. Where the
budget allows, the enumeration therefore covers **every item in the pool**, Mythic+
and raid alike, and it is ranked twice: over the Mythic+ subset it gives the
baseline of step 1, and over all of it the best set the slot can hold at each
candidate item level. One run, two answers, and the second is a destination rather
than a step -- "what should end up on these fingers", where step 3 answers "is this
particular drop an upgrade today".

Both readings are published, because they answer different questions and a loot
council needs both. Where the two disagree -- where the ceiling is not the baseline
plus its best single drop -- that disagreement is the finding.

Why profilesets and not separate sims
-------------------------------------
Measured, on MID2 Arcane Mage at 3000 deterministic iterations:

* Two profilesets with identical gear return **bit-identical** DPS, and a profileset
  returns the same number regardless of which other profilesets share its run. So
  gains are exact differences, not differences plus Monte Carlo noise, and results
  from the step-1 run and the step-3 run are directly comparable.
* The *base actor* is not that: it ran 2996 iterations where the profilesets ran
  3000, and landed 0.09% away from an identical profileset -- outside its own 0.088%
  error. So the baseline is itself a profileset. Never compare a profileset to the
  base actor.
* ``profileset_work_threads=1`` is mandatory, as everywhere else in this project:
  without it each variant silently runs at ``iterations / threads``.
* Cost is about 11 CPU-seconds per variant at 3000 iterations of a 300-second
  single-target fight, plus about 22 for the base actor that has to run anyway.

Precision: this sweep runs at fewer iterations than the rest of the project
--------------------------------------------------------------------------
1000 rather than 3000, set by ``cli.DEFAULT_GEAR_ITERATIONS`` and overridable. The
project default exists to resolve sub-percent gaps *between specs* in a dataset
committed on a schedule. This sweep answers a different question at a different
scale -- trinket differences run 1-5% of DPS -- and it has to be cheap enough to
re-run after every tuning pass, not once a season.

1000 deterministic iterations measure a single-target cell to roughly 0.15% standard
error where 3000 gives about 0.09%. Both are an order of magnitude inside the effect
being measured, and both stay deterministic, so a quiet re-run still produces
byte-identical output. The tie rule does the rest of the work: a margin inside
``hypot(errA, errB)`` is reported as a tie, and at 1000 iterations that band is
simply a little wider. It is not widened further on top of that.
"""

from __future__ import annotations

import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import simc_runner
from .equipment import (
    EquipmentSlot,
    GearItem,
    ItemLevel,
    SlotAdornment,
    SlotPool,
    adorn,
    primary_stat,
    read_adornments,
)
from .profiles import SpecProfile
from .scenarios import PATCHWERK, SimSettings

log = logging.getLogger(__name__)


#: How many baseline items make up the "Standard" set: one per socket. This was
#: hard-coded to 2 while trinkets were the only swept slot, which is correct for two
#: sockets and silently wrong for a neck, where it would have picked two items for
#: one socket.
def baseline_size(slot: EquipmentSlot) -> int:
    return len(slot.sockets)


#: How many runners-up to publish beside the chosen baseline, so a near-tie at the
#: cut is visible. Standalone value is not perfectly additive; see the module docstring.
RUNNERS_UP = 4


@dataclass(frozen=True)
class Equipped:
    """What one variant wears in the swept slot."""

    #: socket name -> (item, item level). A socket absent from the mapping is empty.
    sockets: tuple[tuple[str, GearItem | None, int | None], ...]

    def simc_options(self) -> list[str]:
        options = []
        for socket, item, ilevel in self.sockets:
            options.append(f"{socket}=" if item is None else item.simc_item(socket, ilevel or 0))
        return options


@dataclass(frozen=True)
class Offer:
    """One item at one item level -- a thing a socket can be filled with.

    Item level is part of the identity rather than a property of the run, because
    the same ring is a different offer at 334 and at 344 and the two have to be able
    to lose to each other. ``SlotPool.wearable_levels`` decides which levels an item
    is offered at: what the character farms comes at the one level it tops out at,
    what drops comes at every level it can drop at.
    """

    item: GearItem
    level: ItemLevel


@dataclass(frozen=True)
class Variant:
    """One profileset: a name, what it wears and what it is for."""

    key: str
    equipped: Equipped


@dataclass
class VariantResult:
    key: str
    dps: float
    dps_error: float  # standard error of the mean, in percent
    iterations: int
    priority_dps: float | None = None


def _run(
    simc: Path,
    profile: SpecProfile,
    targets: int,
    settings: SimSettings,
    variants: list[Variant],
    timeout: int,
) -> dict[str, VariantResult]:
    """One simc invocation covering every variant, returning results by key.

    The profileset mechanics live in ``simc_runner`` -- the measurements that
    justify them are in its comment, and the talent sweep needs the same machinery
    with different option strings.
    """
    request = simc_runner.SimRequest(profile=profile, scenario=PATCHWERK, targets=targets)
    sets = [
        simc_runner.Profileset(key=variant.key, options=tuple(variant.equipped.simc_options()))
        for variant in variants
    ]
    return {
        key: VariantResult(
            key=result.key,
            dps=result.dps,
            dps_error=result.dps_error,
            iterations=result.iterations,
            priority_dps=result.priority_dps,
        )
        for key, result in simc_runner.run_profilesets(
            simc, request, settings, sets, timeout=timeout
        ).items()
    }


# --------------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------------


@dataclass
class PoolEntry:
    """One baseline-pool item measured on its own."""

    item: GearItem
    ilevel: int
    dps: float
    dps_error: float
    #: DPS this item adds over wearing nothing in either socket.
    standalone_gain: float
    #: Best DPS any full combination containing this item reached, when the
    #: baseline was picked exhaustively. 0 when it was picked additively, in which
    #: case ``standalone_gain`` is the ranking figure.
    best_combination_dps: float = 0.0
    chosen: bool = False

    def to_json(self) -> dict:
        return {
            "id": self.item.item_id,
            "ilevel": self.ilevel,
            "dps": round(self.dps, 1),
            "dpsError": round(self.dps_error, 4),
            "standaloneGain": round(self.standalone_gain, 1),
            "bestCombinationDps": round(self.best_combination_dps, 1) or None,
            "chosen": self.chosen,
        }


@dataclass
class CandidateResult:
    """One candidate item at one item level, judged against the baseline."""

    item: GearItem
    item_level: ItemLevel
    replaces: GearItem
    dps: float
    dps_error: float
    priority_dps: float | None
    #: Fraction of baseline DPS gained. Negative means the baseline is better.
    gain: float
    #: The two runs' errors in quadrature. A gain inside this is a tie, not a lead.
    gain_error: float

    @property
    def is_tie(self) -> bool:
        return abs(self.gain) <= self.gain_error

    def to_json(self) -> dict:
        out = {
            "id": self.item.item_id,
            "level": self.item_level.id,
            "ilevel": self.item_level.ilevel,
            "replaces": self.replaces.item_id,
            "dps": round(self.dps, 1),
            "dpsError": round(self.dps_error, 4),
            "gain": round(self.gain, 5),
            "gainError": round(self.gain_error, 5),
        }
        if self.priority_dps is not None:
            out["priorityDps"] = round(self.priority_dps, 1)
        return out


@dataclass(frozen=True)
class WornSet:
    """One measured way of filling every socket in the slot."""

    offers: tuple[Offer, ...]
    dps: float
    dps_error: float

    @property
    def item_ids(self) -> tuple[int, ...]:
        return tuple(offer.item.item_id for offer in self.offers)

    def items_json(self) -> list[dict]:
        return [{"id": o.item.item_id, "ilevel": o.level.ilevel} for o in self.offers]


def _runner_up_json(winner: WornSet | None, runner: WornSet | None) -> dict | None:
    """What the winning set beat, and by how much against the noise.

    Published because a winner that ties its runner-up is not a winner, and nothing
    else in the file can show it: the ``pool`` entries are per *item*, so two sets
    that differ by one ring and a tenth of a percent look like a settled answer from
    the outside. Same rule as everywhere else -- a margin inside ``hypot`` of the two
    errors is a tie.

    Both arguments are whole sets and **both must come from the same invocation**.
    This took a winner's DPS and error as two loose floats for a while, which let the
    baseline path hand it a number from the candidate run to compare against a
    runner-up from the combination run. Two things went wrong with that and only the
    second was visible: the gap was a cross-invocation difference, and it could come
    out *negative* -- at which point ``abs(gap)`` still called it a lead and the view,
    which prefixes the figure with a minus, rendered "--0.40%".
    """
    if winner is None or runner is None:
        return None
    gap = winner.dps / runner.dps - 1
    gap_error = math.hypot(winner.dps_error, runner.dps_error) / 100
    return {
        "items": runner.items_json(),
        "dps": round(runner.dps, 1),
        "gap": round(gap, 5),
        "gapError": round(gap_error, 5),
        "tie": abs(gap) <= gap_error,
    }


@dataclass
class BestSet:
    """The best the slot can hold at one candidate item level, from the whole pool.

    Not the same answer as "the baseline plus its best single drop": that is a
    two-step search -- fix the Mythic+ pair, then swap one socket -- and it cannot
    reach a set whose Mythic+ half is not in the best Mythic+ pair. This one is
    chosen from the same exhaustive enumeration the baseline comes out of, so the
    two are measured against each other exactly rather than across runs.
    """

    level: ItemLevel
    worn: WornSet
    #: The best set that is not this one, at the same item level.
    runner_up: WornSet | None
    #: The all-Mythic+ winner, from the same simc invocation. The reference for
    #: ``gain``: comparing across invocations would be sound (profilesets repeat
    #: bit-identically) but comparing inside one needs no argument at all.
    baseline: WornSet

    @property
    def is_baseline(self) -> bool:
        """Is the ceiling what the character already wears? A real answer."""
        return self.worn.offers == self.baseline.offers

    @property
    def gain(self) -> float:
        return self.worn.dps / self.baseline.dps - 1

    @property
    def gain_error(self) -> float:
        return math.hypot(self.worn.dps_error, self.baseline.dps_error) / 100

    @property
    def is_tie(self) -> bool:
        return abs(self.gain) <= self.gain_error

    def to_json(self) -> dict:
        return {
            "level": self.level.id,
            "ilevel": self.level.ilevel,
            "items": self.worn.items_json(),
            "dps": round(self.worn.dps, 1),
            "dpsError": round(self.worn.dps_error, 4),
            "gain": round(self.gain, 5),
            "gainError": round(self.gain_error, 5),
            # The denominator, spelled out. `gain` is *not* this set over
            # `baseline.dps`: that figure comes from the candidate invocation and
            # this one from the combination invocation, and a consumer who divides
            # the published numbers gets a third answer with no field explaining it.
            # `baseline.drift` measures how far apart the two are.
            "baselineDps": round(self.baseline.dps, 1),
            # Serialised rather than left to the reader. The tie rule is the project's
            # uncertainty convention and belongs in one implementation: the sibling
            # `runnerUp.tie` one field over is already published from here, and having
            # the view recompute this one put the same rule in two places with nothing
            # to catch a change to one of them.
            "isTie": self.is_tie,
            "isBaseline": self.is_baseline,
            "runnerUp": _runner_up_json(self.worn, self.runner_up),
        }


#: How the baseline was chosen. ``exhaustive`` means every combination was run;
#: ``additive`` means the pool was too large for the budget and the top items by
#: standalone value were taken instead. Recorded rather than assumed, because the
#: two disagree on half the tier and the numbers look identical either way.
EXHAUSTIVE = "exhaustive"
ADDITIVE = "additive"


@dataclass
class TargetResult:
    """One spec, one slot, one target count."""

    targets: int
    empty_dps: float
    baseline: list[GearItem]
    baseline_ilevel: int
    baseline_dps: float
    baseline_dps_error: float
    pool: list[PoolEntry] = field(default_factory=list)
    candidates: list[CandidateResult] = field(default_factory=list)
    #: Which of ``baseline`` the candidates displace: the measured weakest of the
    #: set. Published beside the items rather than left to be re-derived, because
    #: the obvious derivation -- "the last one" -- is the defect this used to have,
    #: and a reader who repeats it gets the same wrong answer the pipeline did.
    replaces: GearItem | None = None
    #: ``EXHAUSTIVE`` or ``ADDITIVE`` -- see the constants above.
    baseline_method: str = ADDITIVE
    #: How many farmed combinations produced a measurement, i.e. how many the
    #: choice actually rests on. 0 under the additive rule.
    baseline_combinations: int = 0
    #: The winning Mythic+ set as the *combination* run measured it. The same gear
    #: as ``baseline``, and a different number from ``baseline_dps``, which the
    #: candidate run measured. Kept because the runner-up gap and the ceiling gains
    #: both have to be taken against a reference from their own invocation.
    baseline_set: WornSet | None = None
    #: The best Mythic+ set that is not the baseline, when one was measured.
    baseline_runner_up: WornSet | None = None
    #: How far the baseline moved between the two invocations that both measured it,
    #: as a fraction, with the two errors in quadrature beside it.
    #:
    #: It should be exactly zero: profilesets with identical gear repeat
    #: bit-identically, which is what lets the ceiling be judged inside the
    #: combination run while candidates are judged inside the candidate run. Under
    #: ``--target-error`` the two runs converge separately and it stops being zero,
    #: and then the ceiling gains and the candidate gains on the same page are
    #: measured from references this far apart. Published rather than only logged,
    #: because a gap nobody can see from the file is one nobody can allow for.
    baseline_drift: float | None = None
    baseline_drift_error: float = 0.0
    #: The ceiling, one entry per candidate item level. Empty when the full
    #: enumeration was over budget -- which is not the same as "the baseline is the
    #: ceiling", so it is absent rather than equal.
    best_sets: list[BestSet] = field(default_factory=list)

    def to_json(self) -> dict:
        baseline: dict = {
            "items": [item.item_id for item in self.baseline],
            "ilevel": self.baseline_ilevel,
            "dps": round(self.baseline_dps, 1),
            "dpsError": round(self.baseline_dps_error, 4),
            "method": self.baseline_method,
            "combinations": self.baseline_combinations,
        }
        if self.replaces is not None:
            baseline["replaces"] = self.replaces.item_id
        if self.baseline_drift is not None:
            baseline["drift"] = round(self.baseline_drift, 6)
            baseline["driftError"] = round(self.baseline_drift_error, 6)
        runner_up = _runner_up_json(self.baseline_set, self.baseline_runner_up)
        if runner_up is not None:
            baseline["runnerUp"] = runner_up
        out = {
            "targets": self.targets,
            "emptyDps": round(self.empty_dps, 1),
            "baseline": baseline,
            "pool": [entry.to_json() for entry in self.pool],
            "candidates": [entry.to_json() for entry in self.candidates],
        }
        if self.best_sets:
            out["bestSets"] = [entry.to_json() for entry in self.best_sets]
        return out


@dataclass
class SpecSlotResult:
    """Everything the sweep learned about one spec in one slot."""

    profile: SpecProfile
    slot: EquipmentSlot
    primary_stat: str
    targets: list[TargetResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        out: dict = {
            "id": self.profile.id,
            "class": self.profile.wow_class,
            "spec": self.profile.spec,
            "heroTalent": self.profile.hero_label,
            "specId": self.profile.spec_id,
            "displayName": self.profile.display_name,
            "primaryStat": self.primary_stat,
            "targets": [entry.to_json() for entry in self.targets],
        }
        if self.errors:
            out["errors"] = self.errors
        return out


# --------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------

_EMPTY_KEY = "empty"
_BASELINE_KEY = "standard"


def _probe_key(item: GearItem) -> str:
    return f"solo_{item.slug}"


def _candidate_key(item: GearItem, level: ItemLevel) -> str:
    return f"cand_{item.slug}_{level.id}"


#: Above this many combinations the exhaustive search is not worth its runtime and
#: the additive approximation is used instead, with the dataset saying which.
#:
#: **It counts combinations, and a sweep runs more variants than that.** Each also
#: runs the both-sockets-empty reference, one solo variant per farmed item, and a
#: second invocation carrying the baseline and every candidate at every item level.
#: Finger at 82 combinations runs 98 variants; the gap is the farmed pool plus the
#: candidate grid, so it widens as a pool grows. Read as a variant budget this
#: number under-buys by about 20% at the finger's shape and more at a larger one.
#:
#: Sized from the measured ~11 CPU-seconds a caster's variant costs at 3000
#: iterations, about a third of that at the 1000 this sweep runs at: the ~98
#: variants a 82-combination slot actually runs came in at 125-193 seconds of wall
#: clock on four cores, which fits a dispatched run comfortably.
#:
#: Counted against MID2's derived pools -- and the counts are *not* the pool sizes,
#: which is what an earlier note here got wrong. The Mythic+ half is what forms a
#: baseline; the whole pool is what forms a ceiling:
#:
#:   slot     pool         baseline (M+ only)   full (M+ and raid, per level)
#:   neck     4 M+, 3 raid            4                  4 + 2x3  =    10
#:   finger   8 M+, 3 raid           28                 28 + 2x27 =    82
#:   trinket  27 M+, 15 raid        351                          =  1371
#:
#: So rings and necks get both answers and trinkets get neither, staying on the
#: additive rule until somebody decides to pay ~28 CPU-hours across the tier for the
#: baseline alone. The two budgets are checked separately so that a pool which can
#: afford a measured baseline but not a measured ceiling still gets the baseline --
#: losing one to the cost of the other would be a silent regression.
MAX_BASELINE_COMBINATIONS = 120


def _combination_variants(
    slot: EquipmentSlot,
    worn: list[Offer],
    drops: list[Offer],
) -> dict[str, tuple[Offer, ...]]:
    """Every way of filling the slot's sockets, keyed by variant id.

    Two things this is doing at once, which is the whole point of doing it here:

    *The baseline.* Combinations drawn only from ``worn`` -- what the character can
    already farm -- are the difference between *the best pair* and *the two best
    items*, and those are not the same thing. Standalone value was measured to be
    additive to within about 3% on trinkets, close enough to rank a clear winner and
    not close enough at the cut: two items that individually rank third and fourth
    beat the top two together when the top two overlap (two procs competing for the
    same global cooldowns, two on-use effects that cannot both be pressed).

    *The ceiling.* Combinations that also draw from ``drops`` answer the question
    the baseline-then-one-swap search cannot reach: the best set overall may pair a
    drop with a farmed item that is **not** in the best farmed pair, and no step of
    that search would ever propose it.

    A drop is offered at each item level it can drop at, but one variant carries a
    single such level: a set with one ring at 334 and another at 344 is a third
    question, and the published comparison has one item-level control, not two.
    Combinations of farmed items carry no level of their own -- they are priced at
    the baseline level whatever the reader picks -- so they are enumerated once
    rather than once per level.
    """
    size = len(slot.sockets)
    farmed = set(worn)
    chosen: dict[str, tuple[Offer, ...]] = {}

    for combo in itertools.combinations(worn, size):
        if not _fills_every_socket(combo, size):
            continue
        chosen[_combo_key(combo)] = combo

    by_level: dict[str, list[Offer]] = {}
    for offer in drops:
        by_level.setdefault(offer.level.id, []).append(offer)
    for level_offers in by_level.values():
        for combo in itertools.combinations([*worn, *level_offers], size):
            if all(offer in farmed for offer in combo):
                continue  # already enumerated above, and level-independent
            if not _fills_every_socket(combo, size):
                continue
            chosen[_combo_key(combo)] = combo
    return chosen


def _fills_every_socket(combo: tuple[Offer, ...], size: int) -> bool:
    """Does this combination put a *different* item in each socket?

    One item cannot fill two sockets, and simc answers a duplicate by leaving one
    empty -- a plausible number for a set nobody could wear, which could then win the
    enumeration and be published as the baseline. The guard used to sit on the
    drop-bearing loop alone, on the reasoning that the two sources are disjoint and
    one level is offered at a time. That covers a duplicate *across* the two halves
    and not one *within* either: `gear_pools.json` is written by `gearpool.py` from
    the journal's loot tables, where one item placed by two encounters is an ordinary
    thing, and a repeated id would be paired with itself here.
    """
    return len({offer.item.item_id for offer in combo}) == size


def _combo_key(combo: tuple[Offer, ...]) -> str:
    return "combo__" + "__".join(f"{o.item.item_id}_{o.level.id}" for o in combo)


def _set_variant(
    key: str,
    slot: EquipmentSlot,
    offers: tuple[Offer, ...],
    adornments: dict[str, SlotAdornment],
) -> Variant:
    return _pair_variant(
        key,
        slot,
        [offer.item for offer in offers],
        [offer.level.ilevel for offer in offers],
        adornments,
    )


def _solo_variants(
    slot: EquipmentSlot,
    items: list[GearItem],
    ilevel: int,
    adornments: dict[str, SlotAdornment],
) -> list[Variant]:
    """One variant per item, alone in the first socket, every other socket empty."""
    empties = tuple((socket, None, None) for socket in slot.sockets[1:])
    first = slot.sockets[0]
    return [
        Variant(
            key=_probe_key(item),
            equipped=Equipped(
                sockets=((first, adorn(item, adornments.get(first)), ilevel), *empties)
            ),
        )
        for item in items
    ]


def _pair_variant(
    key: str,
    slot: EquipmentSlot,
    items: list[GearItem],
    ilevels: list[int],
    adornments: dict[str, SlotAdornment],
) -> Variant:
    return Variant(
        key=key,
        equipped=Equipped(
            sockets=tuple(
                (socket, adorn(item, adornments.get(socket)), ilevel)
                for socket, item, ilevel in zip(slot.sockets, items, ilevels, strict=False)
            )
        ),
    )


def _published_pool(entries: list[PoolEntry], limit: int) -> list[PoolEntry]:
    """The pool rows worth publishing, under *both* rankings rather than one.

    ``entries`` arrives sorted by whichever figure chose the baseline -- best
    combination under the exhaustive rule, standalone value under the additive one --
    and truncating there quietly answers only that ranking's question. The view has
    two columns over these rows: what an item is worth in the winning company, and
    what it is worth alone. Measured on MID2's finger sweep, the head of the
    combination ranking is not the head of the standalone ranking on four builds of
    six, and on one of them neither of the two best-alone items survived the cut at
    all -- so a column labelled "alone" was printing standalone numbers for items
    picked by a different figure entirely.

    Keeping the union of both heads costs a couple of rows and makes each column
    complete over what is published. Order is left as the ranking left it, since that
    is what the tables read down.
    """
    keep = {id(entry) for entry in entries[:limit]}
    keep |= {id(entry) for entry in sorted(entries, key=lambda e: -e.standalone_gain)[:limit]}
    return [entry for entry in entries if id(entry) in keep]


def _measured_sets(
    running: dict[str, tuple[Offer, ...]],
    results: dict[str, VariantResult],
) -> dict[str, WornSet]:
    """The combinations simc actually returned a number for, by variant key."""
    return {
        key: WornSet(offers=combo, dps=result.dps, dps_error=result.dps_error)
        for key, combo in running.items()
        if (result := results.get(key)) is not None
    }


def _rank(measured: dict[str, WornSet], among: dict[str, tuple[Offer, ...]]) -> list[WornSet]:
    """The measured sets drawn from ``among``, best first.

    Ties break on enumeration order, which is fixed, so a re-run picks the same set
    -- the whole dataset is deterministic and a coin toss here would undo that for
    every number downstream. Whether a lead is real is a separate question, and it
    is answered by publishing the runner-up rather than by moving this sort.
    """
    found = [measured[key] for key in among if key in measured]
    return sorted(found, key=lambda worn: -worn.dps)


def sweep_spec(
    simc: Path,
    profile: SpecProfile,
    pool: SlotPool,
    settings: SimSettings,
    targets: list[int],
    timeout: int = 3600,
) -> SpecSlotResult:
    """Run the whole three-step comparison for one spec, at each target count."""
    slot = pool.slot
    primary = primary_stat(profile.path)
    result = SpecSlotResult(profile=profile, slot=slot, primary_stat=primary)

    baseline_pool = pool.baseline_candidates(primary)
    candidates = pool.candidates(primary)
    baseline_level = pool.baseline_ilevel()

    if len(baseline_pool) < baseline_size(pool.slot):
        result.errors.append(
            f"only {len(baseline_pool)} eligible {pool.baseline_source} items for a "
            f"{primary} spec; need {baseline_size(pool.slot)} to form a baseline"
        )
        return result

    for count in targets:
        try:
            result.targets.append(
                _sweep_one(
                    simc,
                    profile,
                    pool,
                    settings,
                    count,
                    primary,
                    baseline_pool,
                    candidates,
                    baseline_level,
                    timeout,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one target count must not kill the spec
            message = f"{count}T: {exc}"
            log.error("  FAILED %s", message)
            result.errors.append(message)

    return result


def _sweep_one(
    simc: Path,
    profile: SpecProfile,
    pool: SlotPool,
    settings: SimSettings,
    targets: int,
    primary: str,
    baseline_pool: list[GearItem],
    candidates: list[GearItem],
    baseline_level: ItemLevel,
    timeout: int,
) -> TargetResult:
    slot = pool.slot

    # How *this* profile wears the slot. Read per spec rather than fixed per tier,
    # because a Mage's ring gem is not a Rogue's, and both sides of every comparison
    # below wear the same one -- against an unenchanted baseline every candidate
    # would "win" by the enchant.
    adornments = read_adornments(profile.path, slot)

    # Step 1: find what the character actually wears out of the farmable pool, and
    # -- where the budget allows -- what the slot could hold once the raid opens.
    #
    # Two ways to pick a baseline, and the first is the honest one. *Exhaustive*:
    # fill the sockets every possible way and run each, so the answer is a
    # combination that was measured. *Additive*: run every item alone and take the
    # top few, which assumes an item's value does not depend on what sits beside it.
    # That assumption was measured on trinkets and holds to about 3%, enough to rank
    # a clear winner and not enough at the cut -- and on the first exhaustive ring
    # sweep the two methods named a different pair on 13 of 26 builds.
    #
    # The enumeration covers the drops too, and is then read twice: over the farmed
    # subset it is the baseline, over all of it the ceiling. Two budgets rather than
    # one, so a pool that can afford a measured baseline but not a measured ceiling
    # keeps the baseline instead of losing both.
    started = time.monotonic()
    worn = [Offer(item, baseline_level) for item in baseline_pool]
    drops = [Offer(item, level) for item in candidates for level in pool.wearable_levels(item)]
    farmed = set(worn)
    combos = _combination_variants(slot, worn, drops)
    baseline_combos = {
        key: combo for key, combo in combos.items() if all(offer in farmed for offer in combo)
    }
    full_exhaustive = len(combos) <= MAX_BASELINE_COMBINATIONS
    baseline_exhaustive = len(baseline_combos) <= MAX_BASELINE_COMBINATIONS
    if full_exhaustive:
        running = combos
    elif baseline_exhaustive:
        running = baseline_combos
    else:
        running = {}

    empty = Variant(
        key=_EMPTY_KEY,
        equipped=Equipped(sockets=tuple((socket, None, None) for socket in slot.sockets)),
    )
    # The solo run happens either way: it is what the exhaustive path is measured
    # against for display, and the per-item standalone number is what makes a close
    # call at the cut visible to a reader.
    solo = _solo_variants(slot, baseline_pool, baseline_level.ilevel, adornments)
    variants = [empty, *solo]
    variants += [_set_variant(key, slot, combo, adornments) for key, combo in running.items()]
    log.info(
        "  %s: %d farmed + %d drop offer(s) | baseline %d/%d, ceiling %d/%d combination(s)",
        slot.id,
        len(worn),
        len(drops),
        len(baseline_combos) if baseline_exhaustive else 0,
        len(baseline_combos),
        len(combos) if full_exhaustive else 0,
        len(combos),
    )
    step_one = _run(simc, profile, targets, settings, variants, timeout)

    if _EMPTY_KEY not in step_one:
        raise RuntimeError("simc returned no result for the empty-slot reference")
    empty_dps = step_one[_EMPTY_KEY].dps

    entries: list[PoolEntry] = []
    for item in baseline_pool:
        measured = step_one.get(_probe_key(item))
        if not measured:
            continue
        entries.append(
            PoolEntry(
                item=item,
                ilevel=baseline_level.ilevel,
                dps=measured.dps,
                dps_error=measured.dps_error,
                standalone_gain=measured.dps - empty_dps,
            )
        )
    entries.sort(key=lambda entry: -entry.standalone_gain)
    if len(entries) < baseline_size(slot):
        raise RuntimeError(
            f"only {len(entries)} of {len(baseline_pool)} pool items returned a result"
        )

    by_id = {entry.item.item_id: entry for entry in entries}
    measured_sets = _measured_sets(running, step_one)
    ranked_baseline = _rank(measured_sets, baseline_combos)
    best_baseline = ranked_baseline[0] if ranked_baseline else None

    if best_baseline is not None:
        # A per-item figure that survives the change of method: the best the farmed
        # pool can do *with this item in it*. Ranking runners-up by standalone value
        # would contradict the baseline the run just picked.
        for worn_set in ranked_baseline:
            for offer in worn_set.offers:
                entry = by_id.get(offer.item.item_id)
                if entry and worn_set.dps > entry.best_combination_dps:
                    entry.best_combination_dps = worn_set.dps
        entries.sort(key=lambda entry: -entry.best_combination_dps)
        baseline_items = [offer.item for offer in best_baseline.offers]
    else:
        baseline_items = [entry.item for entry in entries[: baseline_size(slot)]]

    kept_ids = {item.item_id for item in baseline_items}
    for entry in entries:
        entry.chosen = entry.item.item_id in kept_ids

    # The ceiling, one answer per item level a drop can arrive at. Only when the
    # *full* enumeration ran: with the baseline-only one there is nothing to say
    # about drops, and publishing the baseline as the ceiling would assert that no
    # drop improves on it, which nothing measured.
    best_sets: list[BestSet] = []
    if full_exhaustive and best_baseline is not None:
        for level in pool.item_levels:
            eligible = {
                key: combo
                for key, combo in combos.items()
                if all(offer in farmed or offer.level.id == level.id for offer in combo)
            }
            ranked = _rank(measured_sets, eligible)
            if not ranked:
                continue
            best_sets.append(
                BestSet(
                    level=level,
                    worn=ranked[0],
                    runner_up=ranked[1] if len(ranked) > 1 else None,
                    baseline=best_baseline,
                )
            )

    # Step 2 and 3 share one run so the baseline and every candidate are measured
    # under identical conditions. The candidate replaces the *weakest* item of the
    # baseline set, because "does this drop beat the worse of the two I wear" is the
    # decision a loot council has.
    #
    # Which one that is has to be *measured*, and this took the wrong answer for the
    # whole life of the exhaustive method. `baseline_items` is the enumeration's
    # order, which follows `gear_pools.json`; under the additive rule the entries
    # were already sorted by standalone value so `[-1]` happened to be the weakest,
    # and under the exhaustive rule it is whichever member of the winning pair sorts
    # later in the pool file. On MID2's published finger sweep that named the
    # *stronger* ring on 10 of 26 builds, so every candidate on those builds was
    # priced against throwing away the better of the two.
    #
    # For two sockets the measured answer is exactly the solo run: dropping B leaves
    # A alone and dropping A leaves B alone, so the one to drop is the one whose
    # partner scores higher by itself -- i.e. the lower standalone value. Ties break
    # on position so a re-run picks the same socket.
    solo_gain = {entry.item.item_id: entry.standalone_gain for entry in entries}
    replaced_index = min(
        range(len(baseline_items)),
        key=lambda index: (solo_gain.get(baseline_items[index].item_id, 0.0), index),
    )
    replaced = baseline_items[replaced_index]

    ilevels = [baseline_level.ilevel] * len(baseline_items)
    variants = [_pair_variant(_BASELINE_KEY, slot, baseline_items, ilevels, adornments)]
    for item in candidates:
        for level in pool.item_levels:
            # Substituted *in place* rather than appended after the survivors. The
            # baseline and the candidate then differ in exactly one socket, which is
            # what "swap one ring" means -- and each surviving item keeps the socket
            # it was measured in, which matters because a profile's two ring sockets
            # carry different gems.
            worn_items = list(baseline_items)
            worn_ilevels = list(ilevels)
            worn_items[replaced_index] = item
            worn_ilevels[replaced_index] = level.ilevel
            variants.append(
                _pair_variant(
                    _candidate_key(item, level), slot, worn_items, worn_ilevels, adornments
                )
            )
    step_two = _run(simc, profile, targets, settings, variants, timeout)

    baseline = step_two.get(_BASELINE_KEY)
    if not baseline:
        raise RuntimeError("simc returned no result for the baseline pair")

    # The same gear, run twice in two invocations: once as a combination in step one
    # and once as `standard` here. It is what lets the ceiling's gain be measured
    # inside step one while the candidates' gains are measured inside step two, and
    # the measurement it rests on is that profilesets with identical gear repeat
    # bit-identically. If that ever stops being true both halves of this file are
    # comparing across a gap, so it is checked rather than assumed -- reported as a
    # finding, not raised, because the numbers are still each internally consistent.
    baseline_drift: float | None = None
    baseline_drift_error = 0.0
    if best_baseline is not None:
        baseline_drift = baseline.dps / best_baseline.dps - 1
        baseline_drift_error = math.hypot(baseline.dps_error, best_baseline.dps_error) / 100
        if abs(baseline_drift) > baseline_drift_error:
            log.warning(
                "  the baseline set measured %.1f in the combination run and %.1f in the "
                "candidate run, a %.3f%% gap outside their combined error -- profilesets "
                "with identical gear are supposed to repeat exactly",
                best_baseline.dps,
                baseline.dps,
                baseline_drift * 100,
            )

    results: list[CandidateResult] = []
    for item in candidates:
        for level in pool.item_levels:
            measured = step_two.get(_candidate_key(item, level))
            if not measured:
                continue
            results.append(
                CandidateResult(
                    item=item,
                    item_level=level,
                    replaces=replaced,
                    dps=measured.dps,
                    dps_error=measured.dps_error,
                    priority_dps=measured.priority_dps,
                    gain=measured.dps / baseline.dps - 1,
                    # Both errors are relative (percent of their own mean), so adding
                    # them in quadrature gives the noise floor of the ratio.
                    gain_error=math.hypot(measured.dps_error, baseline.dps_error) / 100,
                )
            )
    results.sort(key=lambda entry: -entry.gain)

    log.info(
        "  %2dT  baseline %s = %.0f dps  |  %d pool, %d candidates%s  (%.0fs)",
        targets,
        # One name per socket. This read `baseline_items[0] + baseline_items[1]`,
        # which is the last thing in the sweep that assumed two of them -- and it is a
        # progress line, so a correct one-socket run died in its own logging with
        # "list index out of range" and looked like a modelling failure.
        " + ".join(item.slug for item in baseline_items),
        baseline.dps,
        len(entries),
        len(results),
        "".join(
            f"  |  ceiling@{entry.level.ilevel} "
            + " + ".join(offer.item.slug for offer in entry.worn.offers)
            + (" (= baseline)" if entry.is_baseline else f" {entry.gain:+.2%}")
            for entry in best_sets
        ),
        time.monotonic() - started,
    )

    return TargetResult(
        targets=targets,
        empty_dps=empty_dps,
        baseline=baseline_items,
        baseline_ilevel=baseline_level.ilevel,
        baseline_dps=baseline.dps,
        baseline_dps_error=baseline.dps_error,
        pool=_published_pool(entries, baseline_size(slot) + RUNNERS_UP),
        candidates=results,
        replaces=replaced,
        baseline_method=EXHAUSTIVE if best_baseline is not None else ADDITIVE,
        baseline_combinations=len(ranked_baseline),
        baseline_set=best_baseline,
        baseline_runner_up=ranked_baseline[1] if len(ranked_baseline) > 1 else None,
        baseline_drift=baseline_drift,
        baseline_drift_error=baseline_drift_error,
        best_sets=best_sets,
    )
