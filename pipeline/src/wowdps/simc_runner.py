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


#: Whether this pipeline's sims read simc's PTR client data. They do not: nothing in
#: ``build_command`` passes ``ptr=1``, and simc's report says so itself -- ``dbc``
#: carries a ``Live`` and a ``PTR`` block plus ``version_used``, and on simc 625a591
#: (2026-08-23) ``version_used`` is ``Live`` for the exact argv below.
#:
#: **This is not the same thing as ``report["ptr_enabled"]``**, which the manifest
#: publishes as ``simc.ptr``. That is ``SC_USE_PTR``, a compile-time constant
#: defined as 1 in ``engine/config.hpp`` on simc's midnight branch, so it is
#: true of every binary this project builds and says nothing about which data a run
#: used. Anything choosing between simc's ``*_ptr.inc`` and ``*.inc`` generated tables
#: to match the sims wants *this* answer -- see ``cli._tier_set_reference``.
#:
#: Placement matters and is easy to get wrong: ``ptr=1`` only bites *before* the
#: profile path, because simc copies the sim's dbc into the player while parsing the
#: profile. Measured on MID2 Arcane at 1000 deterministic iterations, ``version_used``
#: comes back Live / Live / PTR for no option / after / before -- and the DPS is
#: 176,364.3 in all three, since simc's two tables agree on 625a591. So this is about
#: reading the table the sims read, not about a number that would move today.
#: ``test_build_command_never_enables_ptr_data`` fails if this stops being true.
USES_PTR_DATA = False


def build_command(
    simc: Path,
    request: SimRequest,
    settings: SimSettings,
    out_json: Path,
) -> list[str]:
    """Assemble the simc argv for one sim.

    Options placed *after* the profile file override anything the profile sets,
    which is how the scenario forces its fight style and target count.

    ``ptr=`` is deliberately not among them, and it could not be here even if it
    were wanted: simc copies the sim's dbc into the player when the profile is
    parsed, so the option only bites *before* the profile path. See
    ``USES_PTR_DATA``.
    """
    scenario = request.scenario
    return [
        str(simc),
        str(request.profile.path),
        # command_options() supplies the fight style only when the scenario names one;
        # scenarios that build their own encounter must not, or simc clears their
        # raid events. See Scenario.fight_style.
        *scenario.command_options(),
        f"desired_targets={request.targets}",
        f"max_time={scenario.max_time}",
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


#: The name simc gives its live client data in a report's ``dbc`` block, and the
#: fallback when a report does not say which one it read. It is what this pipeline
#: reads -- see ``USES_PTR_DATA`` -- so falling back to it is falling back to the
#: answer that has been true of every run, not to a guess. ``dataSource`` publishes
#: ``None`` in that case, so "simc said Live" stays distinguishable from "simc said
#: nothing".
LIVE_DATA_SOURCE = "Live"


def simc_metadata(report: dict) -> dict:
    """Version and build information, pulled from a json2 report.

    The game build is the part a reader actually needs -- it says which patch and
    which hotfix these numbers model, so "is last night's class tuning in here?" has
    an answer on the page instead of a guess. It is not at the top level: it lives
    under the first actor's ``dbc`` block, per data source (``Live``/``PTR``), and it
    is the same for every actor in a run, so the first one settles it.

    **``ptr`` says which client data this run read, and it used to say something
    else.** It was ``report["ptr_enabled"]``, i.e. ``SC_USE_PTR``, a compile-time
    constant defined as 1 in ``engine/config.hpp`` on simc's midnight branch -- true
    of every binary this project builds, and therefore an answer to a question nobody
    was asking. Every consumer wanted the other question: the patch panel prints
    "from the PTR data set" from this field, and ``spec-index``, ``hero-trees`` and
    ``talent-trees`` pick between simc's ``*_ptr.inc`` and ``*.inc`` generated tables
    with it. All four were reading a constant, so the published manifest named the
    **PTR** build (69382) while the actor had read the **Live** one, and the panel
    built to answer "which build are these numbers from" named a build they were not
    from (issue #42).

    So it is derived from ``dbc.version_used`` now, which is what the run *used*.
    ``dataSource`` carries simc's own word for it beside the boolean, because a
    boolean with no evidence in the document is how the previous version stayed wrong
    for months.
    """
    game = _game_build(report)
    source = data_source(report)
    return {
        "simcVersion": report.get("version"),
        "buildDate": report.get("build_date"),
        "gitRevision": report.get("git_revision"),
        "gitBranch": report.get("git_branch"),
        # None when the report does not name one: "unknown" is not "Live".
        "dataSource": source,
        "ptr": bool(source) and source != LIVE_DATA_SOURCE,
        "beta": bool(report.get("beta_enabled")),
        "reportVersion": report.get("report_version"),
        # None rather than absent so a reader can tell "no game build in this report"
        # from "this field was never captured".
        "wowVersion": game.get("wow_version"),
        "wowBuild": game.get("build_level"),
        "hotfixDate": game.get("hotfix_date"),
    }


def manifest_used_ptr_data(manifest: dict | None) -> bool:
    """Did the run behind this manifest read simc's PTR client data?

    The one place that question is answered, because three commands ask it --
    ``spec-index``, ``hero-trees`` and ``talent-trees`` all have to read the same
    generated table the sims read, and a wrong answer decodes cleanly while quietly
    describing a different tree.

    ``simc.dataSource`` is the evidence and is preferred. A manifest without it was
    written before ``simc.ptr`` stopped meaning ``SC_USE_PTR`` (issue #42), so its
    ``ptr`` is a compile-time constant carrying no information about the data -- and
    reading it would reproduce the very bug for exactly the documents that have it.
    ``USES_PTR_DATA`` is the answer for those: every manifest was written by this
    pipeline, and nothing in ``build_command`` has ever passed ``ptr=1``.
    """
    simc = (manifest or {}).get("simc") or {}
    source = simc.get("dataSource")
    if isinstance(source, str) and source:
        return source != LIVE_DATA_SOURCE
    return USES_PTR_DATA


def _player_dbc(report: dict) -> dict:
    """The first actor's ``dbc`` block, which every actor in a run shares."""
    players = (report.get("sim") or {}).get("players") or []
    dbc = players[0].get("dbc") if players else None
    return dbc if isinstance(dbc, dict) else {}


def data_source(report: dict) -> str | None:
    """Which client data set the run read: simc's own ``version_used``, or ``None``.

    Simply what simc says. There is no cleverness available here and none wanted --
    the alternative that shipped was ``report["ptr_enabled"]``, which is a compile
    flag and cannot answer this at all.

    ``None`` for a report that names no source, which is not the same claim as
    ``Live`` and is published as its own value rather than folded into one.
    """
    used = _player_dbc(report).get("version_used")
    return used if isinstance(used, str) and used else None


def _game_build(report: dict) -> dict:
    """The WoW build the run modelled: version, build number, hotfix date.

    Read out of the block for the source simc says it **used**. The two blocks can
    differ by a patch -- 69404 against 69382 on the published MID2 run -- so picking
    the wrong one names a build the numbers were not produced against, which is the
    one question the patch panel exists to answer.
    """
    dbc = _player_dbc(report)
    if not dbc:
        return {}
    source = data_source(report)
    block = dbc.get(source) if source else None
    if not isinstance(block, dict):
        # Either the report named no source or it named one it does not carry. Live
        # is what every run of this pipeline has read; a report shape that makes this
        # fire is worth knowing about, so it is not silent.
        if source:
            log.warning(
                "simc's report says it used %r data but carries no such block; reading %s instead",
                source,
                LIVE_DATA_SOURCE,
            )
        block = dbc.get(LIVE_DATA_SOURCE) or {}
    return block if isinstance(block, dict) else {}


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


# --------------------------------------------------------------------------------
# Profilesets: many variants of one actor in a single simc run
# --------------------------------------------------------------------------------
#
# Everything this project compares *within* one profile goes through here, because
# the alternative -- one simc invocation per variant -- is both slower and less
# exact. Measured on MID2 Arcane Mage at 3000 deterministic iterations:
#
# * Two profilesets with identical options return **bit-identical** DPS, and a
#   profileset returns the same number regardless of which others share its run. So
#   a difference between two variants is an exact difference rather than a
#   difference plus Monte Carlo noise, and results from two separate invocations are
#   comparable.
# * The **base actor is not one of them**. It ran 2996 iterations where the
#   profilesets ran 3000 and landed 0.09% away from an identical profileset, outside
#   its own 0.088% error. Whatever a caller wants to compare against must itself be
#   a profileset. This is the single easiest way to get a wrong-by-0.1% answer here.
# * ``profileset_work_threads=1`` is mandatory. Without it each variant silently
#   runs at ``iterations / threads`` -- half the precision, no warning.


@dataclass(frozen=True)
class Profileset:
    """One named variant: a key and the simc options that make it different."""

    key: str
    options: tuple[str, ...]


@dataclass
class ProfilesetResult:
    key: str
    dps: float
    #: Standard error of the mean, in percent.
    dps_error: float
    iterations: int
    #: Damage per second to the priority target, when the run asked for it.
    priority_dps: float | None = None


def profileset_options(sets: list[Profileset]) -> list[str]:
    """``profileset.<key>=<option>`` lines, ``+=`` for the second option onward."""
    options: list[str] = []
    for entry in sets:
        for index, option in enumerate(entry.options):
            options.append(f"profileset.{entry.key}{'=' if index == 0 else '+='}{option}")
    return options


def run_profilesets(
    simc: Path,
    request: SimRequest,
    settings: SimSettings,
    sets: list[Profileset],
    timeout: int = 1800,
    metrics: str = "dps,prioritydps",
) -> dict[str, ProfilesetResult]:
    """One simc invocation covering every variant, keyed by profileset name.

    ``metrics`` is a list because ``profileset_metric`` takes one: the second and
    later entries come back per result under ``additional_metrics``, so a single
    sweep yields the best variant by damage *and* the best by priority damage
    without running anything twice. Note that this only selects what is *reported*
    -- simc never optimises, it runs a fixed action list and tells you the result.
    """
    extra = [
        "profileset_work_threads=1",
        f"profileset_metric={metrics}",
        *profileset_options(sets),
    ]
    report = run(
        simc,
        request,
        SimSettings(
            target_error=settings.target_error,
            max_iterations=settings.max_iterations,
            threads=settings.threads,
            extra_options=(*settings.extra_options, *extra),
        ),
        timeout=timeout,
    )
    return parse_profilesets(report)


def parse_profilesets(report: dict) -> dict[str, ProfilesetResult]:
    """Pull the profileset table out of a json2 report. Pure, so it is testable."""
    results: dict[str, ProfilesetResult] = {}
    for entry in ((report.get("sim") or {}).get("profilesets") or {}).get("results") or []:
        mean = float(entry.get("mean") or 0.0)
        if mean <= 0:
            continue
        # simc keys the extra metric by its *display* name ("Damage per Second to
        # Priority Target/Boss"), so match on what the name is about rather than on
        # an exact string that could be reworded upstream.
        priority = None
        for metric in entry.get("additional_metrics") or []:
            if "priority" in str(metric.get("metric", "")).lower():
                priority = float(metric.get("mean") or 0.0) or None
        results[entry["name"]] = ProfilesetResult(
            key=entry["name"],
            dps=mean,
            dps_error=float(entry.get("mean_stddev") or 0.0) / mean * 100,
            iterations=int(entry.get("iterations") or 0),
            priority_dps=priority,
        )
    return results
