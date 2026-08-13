"""Building the static JSON dataset the SPA consumes.

Layout written under ``<out>/``::

    index.json          manifest: metadata, scenarios, and a summary row per spec
    specs/<id>.json     full detail for one spec (every scenario x target count)

The manifest carries enough to render the ranking and comparison views without
fetching anything else; per-spec files are loaded lazily when a spec is opened.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import simc_runner
from .parse import Cell, parse_cell
from .profiles import SpecProfile
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
            if funnel_cell and funnel_cell.funnel_index is not None:
                entry["funnelIndex"] = round(funnel_cell.funnel_index, 4)
                entry["funnelShare"] = round(funnel_cell.funnel_share or 0.0, 5)
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
                "  %-16s %2dT  dps=%9.0f  funnel=%s  (%d iters, %.1fs)",
                scenario.id,
                targets,
                cell.dps,
                f"{cell.funnel_index:.2f}" if cell.funnel_index is not None else "  - ",
                cell.iterations,
                time.monotonic() - started,
            )

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
            }
            for s in scenarios
        ],
        "specs": [r.summary() for r in results],
    }
    path = out_dir / "index.json"
    path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


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
