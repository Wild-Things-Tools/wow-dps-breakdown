"""Comparing gear the way a loot council has to: against what the character wears.

The measurement is three steps, and each one is a choice that decides what the
number means. All three are spelled out here because a "trinket gain" figure with
the wrong baseline behind it is worse than no figure.

**1. Find the baseline.** Every Mythic+ item eligible for the spec is run alone --
in the first socket, with the second socket *empty* -- against a run with both
sockets empty. The difference is that item's standalone value. An empty partner is
the neutral partner: it cannot share an on-use window with a candidate, and it
cannot feed one a stat buff, so the ranking is a property of the item rather than of
the pairing it happened to be tested in.

The two highest become the baseline pair, "Standard". Measured on Arcane Mage, the
pair is worth 2.9% more than the sum of its two standalone values (28,804 DPS
against 27,988), so the standalone ranking is a good but not exact predictor: two
items whose standalone values are within ~3% of each other could swap once paired.
The full ranking is published so a near-tie at the cut is visible rather than
hidden. The alternative -- every pair -- is N(N-1)/2 runs where this is N, roughly
120 variants per spec instead of 16, and it is not worth an order of magnitude for a
correction of that size.

**2. Price the baseline.** Both baseline items are worn at the *lower* of the two
item levels. Mythic+ gear tops out below Mythic raid gear, and pricing the thing
being compared against at the raid's top level would flatter every raid drop.

**3. Judge each raid candidate.** Each is put in the socket holding the *second*
best baseline item, at each item level, and the whole set of results is one simc
run. Replacing the weaker of the two is the actual decision -- nobody asks whether a
drop beats their best trinket while keeping their worst.

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

    def to_json(self) -> dict:
        return {
            "targets": self.targets,
            "emptyDps": round(self.empty_dps, 1),
            "baseline": {
                "items": [item.item_id for item in self.baseline],
                "ilevel": self.baseline_ilevel,
                "dps": round(self.baseline_dps, 1),
                "dpsError": round(self.baseline_dps_error, 4),
            },
            "pool": [entry.to_json() for entry in self.pool],
            "candidates": [entry.to_json() for entry in self.candidates],
        }


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


#: Above this many combinations the exhaustive baseline is not worth its runtime
#: and the additive approximation is used instead, with the dataset saying which.
#: Sized from the measured ~11 CPU-seconds a caster's variant costs: 120 variants
#: is about 22 CPU-minutes per spec per target count, which fits a dispatched run.
#: It covers neck (7 items, 7 combinations) and finger (11 items, 55) comfortably
#: and deliberately excludes trinkets (27 Mythic+ items, 351 combinations, ~28
#: CPU-hours across the tier) -- that one stays additive until somebody decides to
#: pay for it.
MAX_BASELINE_COMBINATIONS = 120


def _combination_variants(
    slot: EquipmentSlot,
    items: list[GearItem],
    ilevel: int,
    adornments: dict[str, SlotAdornment],
) -> dict[str, tuple[GearItem, ...]]:
    """Every way of filling the slot's sockets from ``items``, keyed by variant id.

    This is the difference between *the best pair* and *the two best items*, and
    they are not the same thing. Standalone value was measured to be additive to
    within about 3% on trinkets, which is close enough to rank a clear winner and
    not close enough to trust at the cut: two items that individually rank third
    and fourth can beat the top two together if the top two overlap -- two haste
    procs competing for the same global cooldowns, two on-use effects that cannot
    both be pressed. Filling the sockets and running that is the only way to see it.
    """
    chosen: dict[str, tuple[GearItem, ...]] = {}
    for combo in itertools.combinations(items, len(slot.sockets)):
        chosen[_combo_key(combo)] = combo
    return chosen


def _combo_key(combo: tuple[GearItem, ...]) -> str:
    return "combo__" + "__".join(str(item.item_id) for item in combo)


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

    # Step 1: find what the character actually wears out of the farmable pool.
    #
    # Two ways to do that, and the first is the honest one. *Exhaustive*: fill the
    # sockets every possible way and run each -- the answer is then the best
    # combination, measured. *Additive*: run every item alone and take the top few,
    # which assumes an item's value does not depend on what sits beside it. That
    # assumption was measured on trinkets and holds to about 3%, which ranks a clear
    # winner correctly and is not good enough at the cut, where two items that
    # individually place third and fourth can beat the top two together.
    started = time.monotonic()
    combos = _combination_variants(slot, baseline_pool, baseline_level.ilevel, adornments)
    exhaustive = len(combos) <= MAX_BASELINE_COMBINATIONS

    empty = Variant(
        key=_EMPTY_KEY,
        equipped=Equipped(sockets=tuple((socket, None, None) for socket in slot.sockets)),
    )
    # The solo run happens either way: it is what the exhaustive path is measured
    # against for display, and the per-item standalone number is what makes a close
    # call at the cut visible to a reader.
    solo = _solo_variants(slot, baseline_pool, baseline_level.ilevel, adornments)
    variants = [empty, *solo]
    if exhaustive:
        variants += [
            _pair_variant(
                key,
                slot,
                list(items),
                [baseline_level.ilevel] * len(items),
                adornments,
            )
            for key, items in combos.items()
        ]
    log.info(
        "  %s baseline: %d pool item(s), %d combination(s), %s",
        slot.id,
        len(baseline_pool),
        len(combos),
        "exhaustive" if exhaustive else f"additive (over {MAX_BASELINE_COMBINATIONS})",
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
    best_combo: tuple[GearItem, ...] | None = None
    best_combo_dps = 0.0
    if exhaustive:
        for key, items in combos.items():
            measured = step_one.get(key)
            if measured and measured.dps > best_combo_dps:
                best_combo_dps = measured.dps
                best_combo = items
        # A per-item figure that survives the change of method: the best the pool
        # can do *with this item in it*. Ranking runners-up by standalone value
        # would contradict the baseline the run just picked.
        for key, items in combos.items():
            measured = step_one.get(key)
            if not measured:
                continue
            for item in items:
                entry = by_id.get(item.item_id)
                if entry and measured.dps > entry.best_combination_dps:
                    entry.best_combination_dps = measured.dps
        entries.sort(key=lambda entry: -entry.best_combination_dps)

    if best_combo is not None:
        baseline_items = list(best_combo)
    else:
        baseline_items = [entry.item for entry in entries[: baseline_size(slot)]]

    kept_ids = {item.item_id for item in baseline_items}
    for entry in entries:
        entry.chosen = entry.item.item_id in kept_ids

    # Step 2 and 3 share one run so the baseline and every candidate are measured
    # under identical conditions. The candidate always replaces the *second* socket,
    # which holds the weaker of the two baseline items.
    ilevels = [baseline_level.ilevel] * len(baseline_items)
    variants = [_pair_variant(_BASELINE_KEY, slot, baseline_items, ilevels, adornments)]
    replaced = baseline_items[-1]
    kept = baseline_items[:-1]
    for item in candidates:
        for level in pool.item_levels:
            variants.append(
                _pair_variant(
                    _candidate_key(item, level),
                    slot,
                    [*kept, item],
                    [*([baseline_level.ilevel] * len(kept)), level.ilevel],
                    adornments,
                )
            )
    step_two = _run(simc, profile, targets, settings, variants, timeout)

    baseline = step_two.get(_BASELINE_KEY)
    if not baseline:
        raise RuntimeError("simc returned no result for the baseline pair")

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
        "  %2dT  baseline %s = %.0f dps  |  %d pool, %d candidates  (%.0fs)",
        targets,
        # One name per socket. This read `baseline_items[0] + baseline_items[1]`,
        # which is the last thing in the sweep that assumed two of them -- and it is a
        # progress line, so a correct one-socket run died in its own logging with
        # "list index out of range" and looked like a modelling failure.
        " + ".join(item.slug for item in baseline_items),
        baseline.dps,
        len(entries),
        len(results),
        time.monotonic() - started,
    )

    return TargetResult(
        targets=targets,
        empty_dps=empty_dps,
        baseline=baseline_items,
        baseline_ilevel=baseline_level.ilevel,
        baseline_dps=baseline.dps,
        baseline_dps_error=baseline.dps_error,
        pool=entries[: baseline_size(slot) + RUNNERS_UP],
        candidates=results,
    )
