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

from . import equipment, gearsweep, simc_runner
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
        if self.profile.unvalidated:
            # Emitted only when true, so a tier of shipped profiles produces the
            # same bytes it did before this existed and a quiet night still has
            # nothing to commit.
            out["unvalidated"] = True
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


def run_spec(
    simc: Path,
    profile: SpecProfile,
    scenarios: list[Scenario],
    settings: SimSettings,
    timeout: int = 1800,
) -> SpecResult:
    """Run every scenario x target count for one spec."""
    result = SpecResult(profile=profile)
    seen_caveats: set[str] = set()

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
        if key in published:
            settled[key] = published[key]
    log.info("dataset unchanged; keeping generatedAt %s", settled.get("generatedAt"))
    return settled


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
