"""Building the static JSON dataset the SPA consumes.

Layout written under ``<out>/``::

    tiers.json               which tiers exist and which one is current
    <tier>/index.json        manifest: metadata, scenarios, a summary row per spec
    <tier>/specs/<id>.json   full detail for one spec (every scenario x target count)

The manifest carries enough to render the ranking and comparison views without
fetching anything else; per-spec files are loaded lazily when a spec is opened.

Everything is namespaced by tier because a tier is a different *game state*, not a
different filter: season 1 and season 2 profiles carry different gear, different
talents and a different spec list. ``tiers.json`` is regenerated from whatever tier
directories are present, so adding a tier is a build with ``--tier``, not a code
change.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import equipment, gearanchor, gearsweep, simc_runner
from .buffsweep import TierSet
from .parse import Cell, parse_cell
from .profiles import SpecProfile, tier_label
from .scenarios import Scenario, SimSettings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Target counts summarised in the manifest so the overview can rank without
#: loading per-spec files.
SUMMARY_TARGETS = (1, 3, 5, 10)

#: Target count whose funnel number represents the spec in the overview.
SUMMARY_FUNNEL_TARGETS = 5


@dataclass
class SpecResult:
    profile: SpecProfile
    #: scenario id -> target count -> cell
    cells: dict[str, dict[int, Cell]] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    #: Set when this build's gear could not be matched to the tier's. Kept beside the
    #: caveat text rather than derived from it: the ranking reads the manifest and the
    #: caveat lives in the spec file, so without a flag here the one place the gear
    #: gap actually misleads -- a bar chart of absolute DPS -- is the one place that
    #: cannot see it.
    gear_comparable: bool = True
    #: Set when this build's tier-set state could not be matched to the tier's. A
    #: second flag rather than a second meaning for ``gear_comparable``, because the
    #: two are different claims and a build can carry either without the other:
    #: MID2's Arcane builds sit squarely inside the item-level band and wear none of
    #: the tier set, while its disabled profiles carry both gaps at once. One boolean
    #: would leave the sentence beside it guessing which it meant.
    tier_set_comparable: bool = True
    errors: list[str] = field(default_factory=list)

    def add(self, scenario_id: str, cell: Cell) -> None:
        self.cells.setdefault(scenario_id, {})[cell.targets] = cell

    def compute_funnel_gain(self, scenarios: list[Scenario]) -> None:
        """Fill in each cell's funnel gain against its scenario's single-target run.

        Funnel gain is main-target damage at N targets divided by damage at one
        target: above 1.0 the extra targets are actively feeding the priority target
        (resources from damage-over-time effects, procs), below 1.0 the global
        cooldowns spent on area damage are costing it.

        This cannot live in ``parse_cell`` because one simc report has no idea what
        the same profile does at a single target.
        """
        for scenario in scenarios:
            if scenario.funnel_baseline is None:
                continue
            by_target = self.cells.get(scenario.id)
            if not by_target:
                continue

            source = scenario.id if scenario.funnel_baseline == "self" else scenario.funnel_baseline
            baseline = (self.cells.get(source) or {}).get(1)
            if not baseline or baseline.dps <= 0:
                continue

            for cell in by_target.values():
                if cell.priority_dps is not None:
                    cell.funnel_gain = cell.priority_dps / baseline.dps

    def to_json(self) -> dict:
        return {
            "id": self.profile.id,
            "class": self.profile.wow_class,
            "spec": self.profile.spec,
            "heroTalent": self.profile.hero_label,
            "specId": self.profile.spec_id,
            "displayName": self.profile.display_name,
            "role": self.profile.role,
            "talentHash": self.profile.talent_hash,
            **({"unvalidated": True} if self.profile.unvalidated else {}),
            **({"origin": self.profile.origin} if self.profile.origin else {}),
            "caveats": self.caveats,
            "errors": self.errors,
            "scenarios": {
                scenario_id: {"targets": [cell.to_json() for _, cell in sorted(by_target.items())]}
                for scenario_id, by_target in sorted(self.cells.items())
            },
        }

    def summary(self) -> dict:
        """Condensed row for the manifest."""
        out: dict = {
            "id": self.profile.id,
            "class": self.profile.wow_class,
            "spec": self.profile.spec,
            "heroTalent": self.profile.hero_label,
            "specId": self.profile.spec_id,
            "displayName": self.profile.display_name,
            "role": self.profile.role,
            "scenarios": {},
        }
        if not self.gear_comparable:
            # Only when false, so a tier whose builds all wear the tier's gear
            # produces the bytes it did before this existed.
            out["gearComparable"] = False
        if not self.tier_set_comparable:
            # Same rule, same reason. MID1's 41 shipped profiles all wear four
            # pieces, so that tier emits this key nowhere and its published bytes do
            # not move at all.
            out["tierSetComparable"] = False
        if self.profile.unvalidated:
            # Emitted only when true, so a tier of shipped profiles produces the
            # same bytes it did before this existed and a quiet night still has
            # nothing to commit.
            out["unvalidated"] = True
        if self.profile.origin:
            # Same emitted-only-when-set rule. Where the build's *talents* come
            # from, for a profile this project materialised: "repaired",
            # "harvested" or "computed". The evidence sentence is the build's
            # first caveat in its own spec file; the flag has to travel in the
            # summary too, because the ranking reads the manifest and a build
            # this project computed must not be drawn as one simc shipped.
            out["origin"] = self.profile.origin
        if self.errors:
            out["errors"] = self.errors

        for scenario_id, by_target in self.cells.items():
            entry: dict = {"dps": {}}
            for targets in SUMMARY_TARGETS:
                cell = by_target.get(targets)
                if cell:
                    entry["dps"][str(targets)] = round(cell.dps, 1)
            funnel_cell = by_target.get(SUMMARY_FUNNEL_TARGETS)
            if funnel_cell and funnel_cell.concentration is not None:
                entry["concentration"] = round(funnel_cell.concentration, 4)
                entry["priorityShare"] = round(funnel_cell.priority_share or 0.0, 5)
            if funnel_cell and funnel_cell.funnel_gain is not None:
                entry["funnelGain"] = round(funnel_cell.funnel_gain, 4)
            single = by_target.get(1)
            if single and single.burst_ratio is not None:
                entry["burstRatio"] = round(single.burst_ratio, 4)
            if entry["dps"]:
                out["scenarios"][scenario_id] = entry
        return out


def shipped_item_levels(profiles: list[SpecProfile]) -> tuple[int, int] | None:
    """The band of item levels the tier's *shipped* profiles wear, low to high.

    A band rather than a single figure, because the tier genuinely spans one: MID2's
    shipped profiles sit at 334 and 344, so comparing against the mode alone flags
    seven of them as incomparable with themselves. The band is derived from the data
    for the same reason the coverage reference list is -- a fixed tolerance is a
    magic number that goes wrong in exactly the season nobody re-checks it.

    Shard-safe, and it has to be: it reads what simc publishes for the tier, which
    every shard sees identically, rather than the slice this run simulated. Disabled
    profiles are excluded so one cannot drag the anchor toward itself and quietly
    excuse its own gap.
    """
    levels = sorted(p.item_level for p in profiles if not p.unvalidated and p.item_level)
    if not levels:
        return None
    return levels[0], levels[-1]


def gear_caveat(profile: SpecProfile, band: tuple[int, int] | None) -> str | None:
    """Say so when a build's gear, not its spec, is what puts it where it is.

    **Absolute DPS does not survive an item-level difference.** This project already
    states that for the tier axis -- a season-over-season comparison has to be
    restricted to within-run ratios -- and the same thing happens *inside* one tier
    the moment a profile simc did not ship is drawn beside ones it did.

    Measured on the first run that included them: MID2's disabled generator profiles
    wear item level 289 where its shipped profiles wear 334-344, and all eight
    resulting builds landed below all twenty-eight shipped ones with no overlap. A
    clean separation like that is the signature of a systematic difference, not of
    eight underperforming specs, and without this caveat the ranking presents a gear
    gap as a balance finding. One of them wears **723**, which is not a Midnight item
    level at all, so the gap is not always downward either.
    """
    if band is None:
        return None
    low, high = band
    written = f"{low}" if low == high else f"{low}-{high}"

    if profile.item_level is None:
        # Absence is not comparability. Five of MID2's disabled profiles state no
        # item level on any gear line, so their gear sits at whatever base level the
        # item ids carry -- which this cannot read, and which there is no reason to
        # assume matches the tier. Saying nothing would let exactly the builds that
        # *cannot* be checked pass as checked. Shipped profiles omit it routinely
        # (eight of MID2's do), so this only fires for the ones simc did not ship.
        if not profile.unvalidated:
            return None
        return (
            "This profile states no item level on any gear line, so its gear could not "
            f"be compared against the {written} the tier's shipped profiles wear. Its "
            "position against those builds may be gear rather than spec."
        )

    if low <= profile.item_level <= high:
        return None
    gap = profile.item_level - (low if profile.item_level < low else high)
    direction = "below" if gap < 0 else "above"
    return (
        f"This profile wears item level {profile.item_level}, {abs(gap)} {direction} the "
        f"{written} the tier's shipped profiles wear. Absolute damage does not survive "
        f"that difference, so its position against those builds is mostly gear."
    )


@dataclass(frozen=True)
class TierSetReference:
    """The tier-set state the tier's own shipped profiles are in, plus every build's.

    Built once per run by ``shipped_set_states`` and then read, so the 26 MB item
    table is parsed once and each profile's gear lines are read once. ``gearanchor``
    already paid for that lesson in the other direction -- ``derive_target`` was
    called per profile and each call ran a whole tier tally -- and the cost here is
    the same shape: the table is what is expensive, not the comparison.

    ``state`` is a *state* (no bonus / the two-piece / the four-piece), never a piece
    count, and the difference is not cosmetic. MID2's shipped profiles split 12 at
    four pieces and 14 at five, which is a coin flip between two numbers that mean
    exactly the same thing to the simulation; reduced to states it is 26 to 2 and
    there is nothing to flip. ``gearanchor.set_state`` is the reduction, over the
    thresholds simc's own table carries rather than an assumed 2 and 4.
    """

    tier: str
    #: The state a strict majority of the tier's shipped profiles are in, or ``None``
    #: when no state holds one.
    state: int | None
    #: state -> how many shipped profiles are in it. Published in the log so a split
    #: tier reads as split rather than as a verdict.
    tally: tuple[tuple[int, int], ...]
    #: profile id -> that build's own state. ``None`` means the tier ships no set for
    #: its class, which is not the same answer as "wears none of one".
    states: dict[str, int | None]

    @property
    def voters(self) -> int:
        return sum(count for _state, count in self.tally)

    @property
    def majority(self) -> int:
        """How many shipped profiles are in ``state``. Zero when there is no majority."""
        return next((count for state, count in self.tally if state == self.state), 0)


def _state_phrase(state: int) -> str:
    """How a set state reads in a sentence, over thresholds nobody wrote down.

    ``set_state`` returns the threshold met, so the number is simc's own: of the 29
    sets in ``item_set_bonus.inc`` on 69a46e1, seventeen carry a two-piece alone,
    ``MID_UWP`` carries 2 and 3 and ``DF_RT`` carries 2, 4 and 6. Spelling "the
    4-piece" from the threshold rather than from a word list is what keeps this true
    for a tier whose set is shaped differently.
    """
    return "no set bonus" if state == gearanchor.SET_NONE else f"the {state}-piece bonus"


def shipped_set_states(
    profiles: list[SpecProfile],
    tier: str,
    sets: list[TierSet],
    item_sets: dict[int, int],
) -> TierSetReference:
    """What tier-set state the tier's *shipped* profiles wear, and what each build wears.

    Derived from the tier itself, the way ``shipped_item_levels`` derives its band and
    ``spec_coverage`` derives its reference spec list. Nothing here names a spec, a
    class or a set token, so a new season needs no edit: the sets come from simc's
    ``item_set_bonus`` table, the membership of an equipped item from its ``id_set``
    in simc's item table, and the reference state from a vote among the profiles simc
    ships.

    **Shard-safe, and it has to be.** Every input is something simc *publishes* for
    the whole tier -- the profiles directory and two generated tables -- never the
    slice a shard simulated. So all twelve shards compute the same reference, and
    ``merge_shards`` keeping the newest manifest keeps a correct one. The same
    property is why the caller passes every profile of the tier rather than its own
    selection; passing a shard's slice would make the answer depend on the shard, and
    it would do so silently.

    Disabled profiles do not vote, for the same reason they are excluded from the
    item-level band: MID2's twelve wear no tier set at all, so letting them vote would
    drag the reference toward their own gap and quietly excuse it. They are still
    *counted* -- ``states`` carries every profile handed in -- because the point of
    the flag is to say so about them.

    A profile whose class the tier ships no set for gets ``None`` rather than zero. A
    class with no set cannot wear one, and calling that "wears none" would flag it for
    a gap that does not exist. Neither MID1 nor MID2 has such a class, but a tier that
    shipped a partial set list would otherwise flag exactly the classes it forgot.

    **A strict majority, or no reference at all.** The flag's sentence is "this build
    differs from what the tier wears", and a tier split down the middle has no such
    thing -- naming one half the reference would flag the other half for disagreeing
    with a coin toss. More than half is what the word means rather than a tolerance
    somebody tuned, and the failure direction is the safe one: no majority means no
    build is flagged. Note that this is deliberately *not* the tie rule
    ``gearanchor.derive_set_pieces`` uses, which breaks toward the lower state; that
    one has to answer with something because a computed build has to wear something,
    and this one does not.
    """
    states: dict[str, int | None] = {}
    tally: dict[int, int] = {}
    for profile in profiles:
        ids = gearanchor.set_ids_for(sets, tier, profile.wow_class)
        if not ids:
            states[profile.id] = None
            continue
        thresholds = _set_thresholds(sets, tier, ids) or gearanchor.DEFAULT_THRESHOLDS
        pieces = gearanchor.count_set_pieces(gearanchor.read_kit(profile.path), ids, item_sets)
        state = gearanchor.set_state(pieces, thresholds)
        states[profile.id] = state
        if not profile.unvalidated:
            tally[state] = tally.get(state, 0) + 1

    voters = sum(tally.values())
    reference: int | None = None
    for state, count in tally.items():
        if count * 2 > voters:
            reference = state
            break
    return TierSetReference(
        tier=tier,
        state=reference,
        tally=tuple(sorted(tally.items())),
        states=states,
    )


def _set_thresholds(sets: list[TierSet], tier: str, ids: frozenset[int]) -> tuple[int, ...] | None:
    """The piece counts this tier's set ships a bonus for, off simc's own table."""
    for entry in sets:
        if entry.tier == tier and entry.set_id in ids and entry.thresholds:
            return entry.thresholds
    return None


def tier_set_caveat(profile: SpecProfile, reference: TierSetReference | None) -> str | None:
    """Say so when a build wears a different tier set state from the rest of the tier.

    The same argument ``gear_caveat`` makes for item level, for the other systematic
    gear difference a tier can contain. **Absolute DPS does not survive either one**,
    and this project already states that for the tier axis -- a season-over-season
    comparison has to be restricted to within-run ratios -- while the ranking draws
    one tier's builds side by side as bars.

    Measured on simc 22b442e over MID2's 28 shipped damage profiles: 14 wear five
    pieces of this season's set, 12 wear four, and **two wear none** -- both Arcane
    Mage builds. Their gear is otherwise the tier's, item level and all, and their own
    action lists branch on ``set_bonus.midnight_season_2_4pc`` twice, so they run the
    no-four-piece branch of their own rotation. Nothing on the site said so.

    The gap is a property of the *equipped items*, not of an option: simc reads
    ``dbc_item_data_t::id_set`` off each equipped piece (``set_bonus_t::initialize``)
    and every MID2 profile's ``set_bonus=`` lines are commented out, all 35 of them,
    which is simc's generator convention and not a spec disabling its own set. Fire
    Mage wears ``primal_leywardens_manaflux`` where Arcane wears
    ``ornaments_of_the_eternal_coil``; the former carries ``id_set`` 2060 and the
    latter carries none, and Fire's tier pieces even redirect their base stats to the
    exact ids Arcane equips.

    Symmetric, like ``gear_caveat``: a build wearing the set in a tier that does not
    is as incomparable as one going without in a tier that does. Which of those MID2
    is does not need to be written down anywhere for this to fire.
    """
    if reference is None or reference.state is None:
        return None
    mine = reference.states.get(profile.id)
    if mine is None or mine == reference.state:
        # Unknown and equal are the same output and different states. Unknown is a
        # class the tier ships no set for, or a build the reference was not built
        # from; neither is something to accuse of a gap.
        return None
    return (
        f"This build wears {_state_phrase(mine)} of {reference.tier}'s tier set, where "
        f"{reference.majority} of the {reference.voters} profiles simc ships for the tier "
        f"wear {_state_phrase(reference.state)}. Absolute damage does not survive that "
        f"difference, so its position against those builds is partly gear rather than spec."
    )


def run_spec(
    simc: Path,
    profile: SpecProfile,
    scenarios: list[Scenario],
    settings: SimSettings,
    timeout: int = 1800,
    reference_item_level: int | None = None,
    reference_set: TierSetReference | None = None,
) -> SpecResult:
    """Run every scenario x target count for one spec."""
    result = SpecResult(profile=profile)
    seen_caveats: set[str] = set()

    if profile.origin_note:
        # First, so the provenance of the talents is the first sentence a reader
        # gets on a build this project materialised. The note travels in the
        # profile file itself (see ``extrabuilds``), so every shard publishes the
        # same words.
        seen_caveats.add(profile.origin_note)
        result.caveats.append(profile.origin_note)

    gear = gear_caveat(profile, reference_item_level)
    if gear:
        log.warning("  %s", gear)
        seen_caveats.add(gear)
        result.caveats.append(gear)
        result.gear_comparable = False

    # A second caveat and a second flag, deliberately not folded into the first. An
    # item-level gap and a set gap are different claims about the same bar, and MID2
    # has a build with each one alone.
    tier_set = tier_set_caveat(profile, reference_set)
    if tier_set:
        log.warning("  %s", tier_set)
        seen_caveats.add(tier_set)
        result.caveats.append(tier_set)
        result.tier_set_comparable = False

    for scenario in scenarios:
        for targets in scenario.sims():
            request = simc_runner.SimRequest(profile=profile, scenario=scenario, targets=targets)
            started = time.monotonic()
            try:
                report = simc_runner.run(simc, request, settings, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the run
                message = f"{scenario.id} @ {targets}T: {exc}"
                log.error("  FAILED %s", message)
                result.errors.append(message)
                continue

            try:
                cell = parse_cell(
                    report,
                    targets,
                    supports_funnel=scenario.supports_funnel,
                    with_timeline=targets in scenario.timeline_at,
                )
            except Exception as exc:  # noqa: BLE001
                message = f"{scenario.id} @ {targets}T: parse failed: {exc}"
                log.error("  FAILED %s", message)
                result.errors.append(message)
                continue

            result.add(scenario.id, cell)
            for caveat in simc_runner.modelling_caveats(report):
                if caveat not in seen_caveats:
                    seen_caveats.add(caveat)
                    result.caveats.append(caveat)

            log.info(
                "  %-16s %2dT  dps=%9.0f  conc=%s  (%d iters, %.1fs)",
                scenario.id,
                targets,
                cell.dps,
                f"{cell.concentration:.2f}" if cell.concentration is not None else "  - ",
                cell.iterations,
                time.monotonic() - started,
            )

    result.compute_funnel_gain(scenarios)
    return result


def write_spec(out_dir: Path, result: SpecResult) -> Path:
    specs_dir = out_dir / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    path = specs_dir / f"{result.profile.id}.json"
    path.write_text(json.dumps(result.to_json(), separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def write_manifest(
    out_dir: Path,
    results: list[SpecResult],
    scenarios: list[Scenario],
    tier: str,
    simc_meta: dict,
    settings: SimSettings,
    coverage: dict | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": tier,
        "simc": simc_meta,
        "settings": {
            "targetError": settings.target_error,
            "maxIterations": settings.max_iterations,
            "deterministic": settings.target_error == 0,
            # The error actually measured, rather than the one that was asked for.
            # In deterministic mode nobody asks for one, and reporting the requested
            # 0 would read as "no error at all" -- which is false and exactly the kind
            # of falsely precise number this project treats as a bug.
            "medianDpsError": _median_dps_error(results),
        },
        "scenarios": [
            {
                "id": s.id,
                "label": s.label,
                "description": s.description,
                "fightStyle": s.fight_style,
                "targetCounts": list(s.target_counts),
                "maxTime": s.max_time,
                "supportsFunnel": s.supports_funnel,
                # Which scenario's single-target run the gain divides by, so the UI
                # can show "alone it would take" without re-deriving the rule.
                "funnelBaseline": s.funnel_baseline,
                "sweepsTargets": len(s.target_counts) > 1,
            }
            for s in scenarios
        ],
        "specs": [r.summary() for r in results],
        # Which of the game's damage specs this tier ships a profile for. Absent on a
        # shard, which knows only its own slice; the merge carries the whole-run value.
        **({"coverage": coverage} if coverage else {}),
    }
    path = out_dir / "index.json"
    path.write_text(
        json.dumps(_settle_provenance(manifest, path), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


#: Manifest fields that describe *the run*, not the data it produced.
_PROVENANCE = ("generatedAt", "simc")


def _settle_provenance(manifest: dict, path: Path) -> dict:
    """Keep the published provenance when nothing about the dataset changed.

    Deterministic sims exist so that a run with no upstream change reproduces byte
    for byte, leaves nothing to commit, and makes any diff in the history mean that
    something actually moved. A wall-clock timestamp in the manifest defeats that on
    its own: every run would rewrite `generatedAt`, every run would commit, and the
    property the determinism was bought for would be worth nothing.

    So the timestamp and the simc build are refreshed only when the rest of the
    manifest differs -- the numbers, the settings, the scenario definitions. When
    they do not, the manifest already on disk still describes the data that is still
    there, and it is left exactly as it is.

    `generatedAt` therefore reads as "when this data last changed", which is also the
    more honest thing to show next to figures that have not moved.
    """
    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return manifest

    if {k: v for k, v in published.items() if k not in _PROVENANCE} != {
        k: v for k, v in manifest.items() if k not in _PROVENANCE
    }:
        return manifest

    settled = dict(manifest)
    for key in _PROVENANCE:
        if key not in published:
            continue
        if _describes_a_different_shape(published[key], manifest.get(key)):
            # A published block that predates a field cannot be settled onto one that
            # has it, or the field never appears: the settle would keep the old block
            # on every quiet run and the new key would wait for the numbers to move.
            # Issue #42 is the case -- `simc.dataSource` and a corrected `simc.ptr`
            # would have sat behind an unchanged dataset indefinitely.
            log.info("published %s has other fields than this run's; taking this run's", key)
            continue
        settled[key] = published[key]
    log.info("dataset unchanged; keeping generatedAt %s", settled.get("generatedAt"))
    return settled


def _describes_a_different_shape(published: object, produced: object) -> bool:
    """Do two provenance blocks carry different *fields*, never mind their values?

    Values are what the settle exists to keep still -- `simc.gitRevision` moves every
    night and says nothing about the data. A field appearing or disappearing is the
    opposite: it is the only evidence in the document that the producer changed.
    """
    return (
        isinstance(published, dict)
        and isinstance(produced, dict)
        and set(published) != set(produced)
    )


def _median(errors: list[float]) -> float | None:
    if not errors:
        return None
    errors = sorted(errors)
    middle = len(errors) // 2
    median = errors[middle] if len(errors) % 2 else (errors[middle - 1] + errors[middle]) / 2
    return round(median, 4)


def _median_dps_error(results: list[SpecResult]) -> float | None:
    """Median per-cell standard error across the whole run, in percent."""
    return _median(
        [
            cell.dps_error
            for result in results
            for by_target in result.cells.values()
            for cell in by_target.values()
            if cell.dps_error > 0
        ]
    )


def _median_dps_error_of_files(specs_dir: Path) -> float | None:
    """The same figure, recovered from spec files already on disk.

    Needed by the shard merge: each shard only ever saw its own slice of the
    matrix, so no shard's manifest carries a median that describes the whole run.
    """
    errors: list[float] = []
    for path in sorted(specs_dir.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        for scenario in spec.get("scenarios", {}).values():
            for cell in scenario.get("targets", []):
                error = cell.get("dpsError") or 0
                if error > 0:
                    errors.append(error)
    return _median(errors)


def write_tier_index(out_dir: Path) -> Path:
    """(Re)generate ``tiers.json`` from the tier directories that actually exist.

    Derived rather than accumulated: a tier that has been deleted disappears from the
    index, and a tier built for the first time appears without anything having to
    register it. The current tier is the highest-numbered one present, matching how
    ``profiles.latest_tier`` picks the tier to simulate.
    """
    tiers: list[dict] = []
    for entry in sorted(out_dir.iterdir()):
        manifest_path = entry / "index.json"
        if not entry.is_dir() or not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tiers.append(
            {
                "id": entry.name,
                "label": tier_label(entry.name),
                "generatedAt": manifest.get("generatedAt"),
                "specCount": len(manifest.get("specs", [])),
                "simcVersion": (manifest.get("simc") or {}).get("simcVersion"),
            }
        )

    if not tiers:
        raise FileNotFoundError(f"no tier directories with an index.json under {out_dir}")

    tiers.sort(key=lambda t: _tier_sort_key(t["id"]))
    index = {"current": tiers[-1]["id"], "tiers": tiers}
    path = out_dir / "tiers.json"
    path.write_text(json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _tier_sort_key(tier: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", tier)
    return (match.group(1), int(match.group(2))) if match else (tier, 0)


def apply_simulated_coverage(manifest: dict) -> dict:
    """Split "simc ships no profile" from "the profile no longer loads".

    ``profiles.spec_coverage`` answers what simc *ships* for a tier, which is the
    only question a single shard can answer -- it simulated one slice, so
    subtracting that slice would report every other shard's specs as broken. Here
    the whole run is on hand, so the third state can be worked out.

    It is not a nicety. Measured on MID1 the day it was first published: simc ships
    a profile for all 26 damage specs, so the panel read **26 of 26** and said the
    coverage was complete -- over a dataset containing no Mage, no Hunter, no
    Warrior, no Havoc, no Retribution and no Elemental Shaman, because 16 of MID1's
    41 profiles no longer load. A reader then has exactly one reading available for
    the missing classes, and it is the wrong one. That is the same "a missing spec
    looks exactly like a bad one" failure the coverage panel was built to prevent,
    one level deeper and stated with more confidence than before it existed.

    Four states, and they are four different sentences on the site:

    ``simulated``    simc ships it and it produced results
    ``broken``       simc ships a profile and this run got nothing out of it
    ``unvalidated``  simc wrote a profile and left it commented out; if this run
                     materialised it, the spec has a number that is a weaker claim
    ``missing``      simc has no profile for it at all

    Mutates and returns ``manifest``. A manifest whose coverage predates ``shipped``
    is left alone rather than guessed at: without knowing what the tier shipped,
    "broken" and "missing" cannot be told apart, and inventing the split would be
    the same error in the other direction.
    """
    coverage = dict(manifest.get("coverage") or {})
    shipped = coverage.get("shipped")
    if not shipped:
        return manifest

    simulated = {(spec.get("class"), spec.get("spec")) for spec in manifest.get("specs", [])}
    broken = sorted(
        (entry["class"], entry["spec"])
        for entry in shipped
        if (entry.get("class"), entry.get("spec")) not in simulated
    )

    coverage["simulated"] = len(shipped) - len(broken)
    coverage["broken"] = [{"class": wow_class, "spec": spec} for wow_class, spec in broken]
    # How many of the tier's unvalidated specs this run got a result out of. The
    # list of them is what simc wrote and did not switch on; this is the subset
    # that ran, which is the number a coverage panel can honestly show beside the
    # shipped one.
    coverage["unvalidatedSimulated"] = len(
        {
            (spec.get("class"), spec.get("spec"))
            for spec in manifest.get("specs", [])
            if spec.get("unvalidated")
        }
    )
    manifest["coverage"] = coverage
    return manifest


def merge_shards(shard_dirs: list[Path], out_dir: Path) -> None:
    """Combine per-shard output directories into one dataset.

    CI splits the matrix across parallel jobs (one per class); each writes its own
    ``specs/`` plus a partial manifest. Merging keeps the newest metadata and the
    union of the spec rows.

    A shard from the gear workflow carries only ``gear.json`` and no manifest at all,
    so the gear merge runs first and a run that produced *only* gear data is a
    success rather than a missing-manifest failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_gear = merge_gear_shards(shard_dirs, out_dir)
    merged_buffs = merge_buff_shards(shard_dirs, out_dir)

    out_specs = out_dir / "specs"
    manifests: list[dict] = []
    for shard in shard_dirs:
        manifest_path = shard / "index.json"
        if manifest_path.is_file():
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        spec_files = sorted((shard / "specs").glob("*.json"))
        if spec_files:
            out_specs.mkdir(parents=True, exist_ok=True)
        for spec_file in spec_files:
            (out_specs / spec_file.name).write_text(
                spec_file.read_text(encoding="utf-8"), encoding="utf-8"
            )

    if not manifests:
        if merged_gear or merged_buffs:
            return
        raise FileNotFoundError(f"no shard manifests found in {shard_dirs}")

    manifests.sort(key=lambda m: m.get("generatedAt", ""))
    merged = dict(manifests[-1])

    by_id: dict[str, dict] = {}
    for manifest in manifests:
        for spec in manifest.get("specs", []):
            by_id[spec["id"]] = spec
    merged["specs"] = [by_id[key] for key in sorted(by_id)]

    # Taking the newest shard's manifest wholesale would publish *that shard's*
    # measured error as the precision of the whole run -- one sixth of the cells
    # describing all of them. Recompute it from what actually landed on disk.
    settings = dict(merged.get("settings") or {})
    settings["medianDpsError"] = _median_dps_error_of_files(out_specs)
    merged["settings"] = settings

    # Same class of correction as the line above, and for the same reason: a shard
    # cannot know what the whole run produced. Which specs simc shipped a profile
    # for and got nothing out of is only answerable here.
    apply_simulated_coverage(merged)

    (out_dir / "index.json").write_text(
        json.dumps(merged, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    log.info("merged %d shards -> %d specs", len(manifests), len(merged["specs"]))


def merge_buff_shards(shard_dirs: list[Path], out_dir: Path) -> Path | None:
    """Combine per-shard ``buffs.json`` files, if the run produced any.

    A buff sweep shards by spec, so every shard writes the same header and a slice
    of ``specs``; merging is the union of those slices, keyed by build id so a
    re-run of one shard replaces rather than duplicates. Same shape as the gear
    merge, and for the same reason: a shard that failed has to shrink the published
    set rather than be papered over.
    """
    documents = []
    for shard in shard_dirs:
        path = shard / "buffs.json"
        if path.is_file():
            documents.append(json.loads(path.read_text(encoding="utf-8")))
    if not documents:
        return None

    documents.sort(key=lambda doc: doc.get("generatedAt", ""))
    merged = dict(documents[-1])
    by_id: dict[str, dict] = {}
    for document in documents:
        for spec in document.get("specs", []):
            by_id[spec["id"]] = spec
    merged["specs"] = [by_id[key] for key in sorted(by_id)]

    # Recounted, never carried over from a shard. `merged` starts as the NEWEST
    # shard's header, whose `coverage.specs` is that shard's own slice -- publishing
    # it as the run's is the same defect this file already records for
    # `medianDpsError` and `targetError`: a number describing a fraction of the run,
    # presented as describing all of it. `specsAvailable` is the tier's size and is
    # the same in every shard, so the max of it is that size and survives a shard
    # that wrote none.
    stated = [(doc.get("coverage") or {}).get("specsAvailable") for doc in documents]
    available = [value for value in stated if isinstance(value, int)]
    if available:
        merged["coverage"] = {"specs": len(merged["specs"]), "specsAvailable": max(available)}
    else:
        # No shard stated one, so neither does the merge. Absent is not zero: a
        # reader must be able to tell "the sweep says nothing" from "the sweep
        # covered nothing".
        merged.pop("coverage", None)

    path = out_dir / "buffs.json"
    path.write_text(json.dumps(merged, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info("merged %d buff shard(s) -> %d specs", len(documents), len(merged["specs"]))
    return path


def merge_gear_shards(shard_dirs: list[Path], out_dir: Path) -> Path | None:
    """Combine per-shard ``gear.json`` files, if the run produced any.

    A gear sweep shards by spec, so every shard writes the same slot definitions and
    a slice of the ``specs`` array; merging is the union of those slices. ``coverage``
    is recounted from what actually arrived rather than carried over from a shard,
    because a shard that failed has to shrink the published coverage rather than be
    papered over.
    """
    documents = []
    for shard in shard_dirs:
        path = shard / "gear.json"
        if path.is_file():
            documents.append(json.loads(path.read_text(encoding="utf-8")))
    if not documents:
        return None

    documents.sort(key=lambda doc: doc.get("generatedAt", ""))
    merged = dict(documents[-1])

    # What is already published joins the merge as the *oldest* document, so a slot
    # this run did not sweep keeps the results it had. Without this a single-slot
    # sweep silently deletes the others: `write_gear` emits an entry for every pool,
    # so a neck run writes a trinket slot with an empty `specs` array, and a merge
    # over shards alone would publish that as the trinket comparison. Union semantics
    # mean an empty array removes nothing, so this only ever preserves.
    published_path = out_dir / "gear.json"
    if published_path.is_file():
        try:
            documents.insert(0, json.loads(published_path.read_text(encoding="utf-8")))
        except ValueError:
            log.warning("%s is not readable JSON; publishing this run alone", published_path)

    slots: dict[str, dict] = {}
    for document in documents:
        for slot in document.get("slots", []):
            existing = slots.setdefault(slot["id"], {**slot, "specs": []})
            by_id = {spec["id"]: spec for spec in existing["specs"]}
            for spec in slot.get("specs", []):
                by_id[spec["id"]] = spec
            existing["specs"] = [by_id[key] for key in sorted(by_id)]

    merged["slots"] = [slots[key] for key in sorted(slots)]
    covered = {spec["id"] for slot in merged["slots"] for spec in slot["specs"]}
    merged["coverage"] = {
        "specs": len(covered),
        "specsAvailable": (merged.get("coverage") or {}).get("specsAvailable", len(covered)),
    }

    path = out_dir / "gear.json"
    path.write_text(json.dumps(merged, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info("merged %d gear shards -> %d specs", len(documents), len(covered))
    return path


# --------------------------------------------------------------------------------
# Gear comparison dataset
# --------------------------------------------------------------------------------

#: Written to ``<tier>/gear.json``. Bumped independently of SCHEMA_VERSION because
#: the two files are loaded independently -- an older site build reading a newer
#: spec dataset should not be told the gear file changed shape.
GEAR_SCHEMA_VERSION = 1

#: Display names for the pool sources. Kept beside the writer rather than in the
#: pool data so a correction to one item's source needs no new label.
SOURCE_LABELS: dict[str, str] = {
    "raid": "Current raid",
    "mythicplus": "Mythic+ dungeons",
}


def write_gear(
    out_dir: Path,
    results: list[gearsweep.SpecSlotResult],
    pools: dict[str, equipment.SlotPool],
    tier: str,
    simc_meta: dict,
    settings: SimSettings,
    specs_available: int,
) -> Path:
    """Write the gear-comparison dataset for one tier.

    One file per tier rather than one per slot: the whole payload is a few hundred
    kilobytes even at full coverage, and the view compares slots side by side. The
    shape is keyed by *slot* throughout -- nothing in it says "trinket" except the
    data -- so necks and rings arrive as extra entries in ``slots``.

    ``coverage`` is written whether or not the run was complete. A gear dataset
    covering six specs of twenty-six is a useful thing to publish and a misleading
    thing to publish silently.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    by_slot: dict[str, list[gearsweep.SpecSlotResult]] = {}
    for result in results:
        by_slot.setdefault(result.slot.id, []).append(result)

    slots = []
    for slot_id, pool in pools.items():
        slots.append(
            {
                "id": slot_id,
                "label": pool.slot.label,
                "sockets": list(pool.slot.sockets),
                "baselineSource": pool.baseline_source,
                "baselineSourceLabel": SOURCE_LABELS.get(
                    pool.baseline_source, pool.baseline_source
                ),
                "candidateSource": pool.candidate_source,
                "candidateSourceLabel": SOURCE_LABELS.get(
                    pool.candidate_source, pool.candidate_source
                ),
                "note": pool.note,
                "itemLevels": [level.to_json() for level in pool.item_levels],
                "items": [item.to_json() for item in pool.items],
                "specs": [result.to_json() for result in by_slot.get(slot_id, [])],
            }
        )

    covered = sorted({result.profile.id for result in results})
    document = {
        "schemaVersion": GEAR_SCHEMA_VERSION,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": tier,
        "simc": simc_meta,
        "settings": {
            "targetError": settings.target_error,
            "maxIterations": settings.max_iterations,
            "deterministic": settings.target_error == 0,
            "medianDpsError": _median_gear_error(results),
        },
        "coverage": {"specs": len(covered), "specsAvailable": specs_available},
        "slots": slots,
    }

    path = out_dir / "gear.json"
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _median_gear_error(results: list[gearsweep.SpecSlotResult]) -> float | None:
    """Median standard error actually measured across every gear variant, in percent.

    Same reasoning as ``_median_dps_error``: in deterministic mode nothing is
    requested, so the requested figure is 0 and quoting it would claim a precision
    the run does not have.
    """
    errors = sorted(
        error
        for result in results
        for target in result.targets
        for error in (
            target.baseline_dps_error,
            *(entry.dps_error for entry in target.pool),
            *(entry.dps_error for entry in target.candidates),
        )
        if error > 0
    )
    if not errors:
        return None
    middle = len(errors) // 2
    median = errors[middle] if len(errors) % 2 else (errors[middle - 1] + errors[middle]) / 2
    return round(median, 4)
