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

from . import simc_runner
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
    }
    path = out_dir / "index.json"
    path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _median_dps_error(results: list[SpecResult]) -> float | None:
    """Median per-cell standard error across the whole run, in percent."""
    errors = sorted(
        cell.dps_error
        for result in results
        for by_target in result.cells.values()
        for cell in by_target.values()
        if cell.dps_error > 0
    )
    if not errors:
        return None
    middle = len(errors) // 2
    median = errors[middle] if len(errors) % 2 else (errors[middle - 1] + errors[middle]) / 2
    return round(median, 4)


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


def merge_shards(shard_dirs: list[Path], out_dir: Path) -> None:
    """Combine per-shard output directories into one dataset.

    CI splits the matrix across parallel jobs (one per class); each writes its own
    ``specs/`` plus a partial manifest. Merging keeps the newest metadata and the
    union of the spec rows.
    """
    out_specs = out_dir / "specs"
    out_specs.mkdir(parents=True, exist_ok=True)

    manifests: list[dict] = []
    for shard in shard_dirs:
        manifest_path = shard / "index.json"
        if manifest_path.is_file():
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        for spec_file in sorted((shard / "specs").glob("*.json")):
            (out_specs / spec_file.name).write_text(
                spec_file.read_text(encoding="utf-8"), encoding="utf-8"
            )

    if not manifests:
        raise FileNotFoundError(f"no shard manifests found in {shard_dirs}")

    manifests.sort(key=lambda m: m.get("generatedAt", ""))
    merged = dict(manifests[-1])

    by_id: dict[str, dict] = {}
    for manifest in manifests:
        for spec in manifest.get("specs", []):
            by_id[spec["id"]] = spec
    merged["specs"] = [by_id[key] for key in sorted(by_id)]

    (out_dir / "index.json").write_text(
        json.dumps(merged, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    log.info("merged %d shards -> %d specs", len(manifests), len(merged["specs"]))
