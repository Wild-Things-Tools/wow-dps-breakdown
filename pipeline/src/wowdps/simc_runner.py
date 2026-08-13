"""Invoking SimulationCraft and collecting its JSON report."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .profiles import SpecProfile
from .scenarios import Scenario, SimSettings

log = logging.getLogger(__name__)


class SimcError(RuntimeError):
    """SimulationCraft exited non-zero or produced no usable report."""


@dataclass(frozen=True)
class SimRequest:
    profile: SpecProfile
    scenario: Scenario
    targets: int

    @property
    def key(self) -> str:
        return f"{self.profile.id}__{self.scenario.id}__t{self.targets}"


def find_simc(explicit: str | None = None) -> Path:
    """Locate the simc binary, preferring an explicit path then $PATH."""
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
        raise SimcError(f"simc binary not found at {candidate}")

    found = shutil.which("simc")
    if found:
        return Path(found)
    raise SimcError(
        "simc binary not found. Pass --simc /path/to/simc or put it on $PATH "
        "(see scripts/build-simc.sh)."
    )


def build_command(
    simc: Path,
    request: SimRequest,
    settings: SimSettings,
    out_json: Path,
) -> list[str]:
    """Assemble the simc argv for one sim.

    Options placed *after* the profile file override anything the profile sets,
    which is how the scenario forces its fight style and target count.
    """
    scenario = request.scenario
    return [
        str(simc),
        str(request.profile.path),
        f"fight_style={scenario.fight_style}",
        f"desired_targets={request.targets}",
        f"max_time={scenario.max_time}",
        *scenario.extra_options,
        *settings.as_simc_options(),
        f"json2={out_json}",
    ]


def run(
    simc: Path,
    request: SimRequest,
    settings: SimSettings,
    timeout: int = 1800,
    keep_raw_in: Path | None = None,
) -> dict:
    """Run one sim and return the parsed json2 report.

    Raw reports are tens of megabytes and fully reproducible, so by default they go
    to a temporary directory and are discarded once parsed.
    """
    with tempfile.TemporaryDirectory(prefix="wowdps-") as tmp:
        out_json = Path(keep_raw_in or tmp) / f"{request.key}.json"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_command(simc, request, settings, out_json)

        log.debug("running: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            raise SimcError(f"simc exited {proc.returncode} for {request.key}:\n" + "\n".join(tail))

        if not out_json.is_file():
            raise SimcError(f"simc produced no JSON report for {request.key}")

        with out_json.open(encoding="utf-8") as fh:
            return json.load(fh)


def requests_for(profiles: list[SpecProfile], scenarios: list[Scenario]) -> Iterator[SimRequest]:
    """Every (profile, scenario, target count) triple the run should cover."""
    for profile in profiles:
        for scenario in scenarios:
            for targets in scenario.sims():
                yield SimRequest(profile=profile, scenario=scenario, targets=targets)


def simc_metadata(report: dict) -> dict:
    """Version and build information, pulled from the top level of a json2 report."""
    return {
        "simcVersion": report.get("version"),
        "buildDate": report.get("build_date"),
        "gitRevision": report.get("git_revision"),
        "gitBranch": report.get("git_branch"),
        "ptr": bool(report.get("ptr_enabled")),
        "beta": bool(report.get("beta_enabled")),
        "reportVersion": report.get("report_version"),
    }


def modelling_caveats(report: dict) -> list[str]:
    """simc's own warnings about spells it models approximately.

    Worth surfacing: they are the honest answer to "how much should I trust this
    number for this spec", and they come straight from the people writing the module.
    """
    caveats: list[str] = []
    for entry in report.get("logs") or []:
        message = entry.get("message") if isinstance(entry, dict) else None
        if message:
            caveats.append(message.strip())
    return caveats
