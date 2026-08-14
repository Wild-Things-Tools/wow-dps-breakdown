"""What the logs cross-check actually says, beyond "X is above and Y is below".

The raw comparison file answers one question per row: how does the median ranked
parse for this build on this boss compare to its simulated single-target DPS. On its
own that is close to useless -- every row is below 1.0, because a Patchwerk sim is a
stationary target with no mechanics, and reporting that real raids fall short of it
is reporting the definition of the two things.

Three readings do carry information, and all three are comparisons *between* rows
rather than statements about any one row:

**Which boss dominates.** The ratio's spread is mostly the encounter, not the build:
the same nine bosses come out in nearly the same order for every build in the tier.
That is one fight style being compared against nine fight shapes, and it is the
measurement that pays for building per-boss scenarios at all.

**What is left of a build once the boss is removed.** Dividing each row by the median
ratio for its boss gives "did this build fall short by more or less than the field
did, on the same fight". A build at 1.10 loses less to a real raid than its peers do;
one at 0.80 loses more. That is a statement about the build -- either the simulation
flatters it, or the fight asks something of it a stationary sim never asks.

**Whether the ordering survives at all.** If the simulation ranks builds the way the
logs do, the sim is a usable guide to what to bring. Measured per boss, because
pooling across bosses mixes in the first reading and reads as noise.

Everything here is a pure function over the comparisons already in the file, so it
runs offline against a committed dataset with no credentials and no API calls.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any

#: Below this many builds a boss gets no rank-agreement figure. A correlation over
#: four points is dominated by which four, and printing it beside one drawn from
#: twenty-six invites reading them as equally solid.
MIN_RANK_SAMPLE = 8

#: Below this many bosses a build gets no `vsField` figure, for the same reason: the
#: boss adjustment only means anything averaged over several different fight shapes.
MIN_BOSS_SAMPLE = 3


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Rank correlation, ties averaged. None when there are too few points."""
    count = len(pairs)
    if count < 3:
        return None
    left = _ranks([pair[0] for pair in pairs])
    right = _ranks([pair[1] for pair in pairs])
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    spread = math.sqrt(
        sum((a - mean_left) ** 2 for a in left) * sum((b - mean_right) ** 2 for b in right)
    )
    if spread == 0:
        return None
    return covariance / spread


def _ranks(values: Sequence[float]) -> list[float]:
    """Fractional ranks, so ties do not invent an ordering that is not there."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 + 1
        for index in order[position : end + 1]:
            ranks[index] = shared
        position = end + 1
    return ranks


def _pearson(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    spread = math.sqrt(
        sum((a - mean_left) ** 2 for a in left) * sum((b - mean_right) ** 2 for b in right)
    )
    if spread == 0:
        return None
    return covariance / spread


def _explained(values: Sequence[float], residuals: Sequence[float]) -> float | None:
    """Share of the variance a grouping accounts for, as 1 - var(residual)/var(all).

    Medians rather than means are subtracted, so this is not textbook eta-squared and
    is not reported as one. It answers the question actually being asked -- how much
    of the spread survives once you know which boss (or which build) a row is from --
    and it is the same arithmetic for both, which is what makes the two comparable.
    """
    if len(values) < 2:
        return None
    total = statistics.pvariance(values)
    if total == 0:
        return None
    return 1 - statistics.pvariance(residuals) / total


def analyse(comparisons: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Derive the three readings from the rows `wowdps verify` collected."""
    rows = [
        row
        for row in comparisons
        if isinstance(row.get("logsToSimRatio"), (int, float))
        and isinstance(row.get("simDps"), (int, float))
        and isinstance(row.get("median"), (int, float))
    ]
    if len(rows) < 2:
        return None

    by_boss: dict[int, list[dict[str, Any]]] = {}
    by_build: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_boss.setdefault(row["encounterId"], []).append(row)
        by_build.setdefault(row["specId"], []).append(row)

    boss_median = {
        encounter: statistics.median([row["logsToSimRatio"] for row in group])
        for encounter, group in by_boss.items()
    }
    build_median = {
        build: statistics.median([row["logsToSimRatio"] for row in group])
        for build, group in by_build.items()
    }

    ratios = [row["logsToSimRatio"] for row in rows]
    explained_by_boss = _explained(
        ratios, [row["logsToSimRatio"] - boss_median[row["encounterId"]] for row in rows]
    )
    explained_by_build = _explained(
        ratios, [row["logsToSimRatio"] - build_median[row["specId"]] for row in rows]
    )

    # Rank movement: where a build sits in the simulated ordering on a boss, against
    # where it sits in the logged one. Computed per boss and then taken over bosses,
    # so a build missing from a boss shifts nobody's rank on the others.
    moves: dict[str, list[int]] = {}
    for group in by_boss.values():
        simulated = {
            row["specId"]: index
            for index, row in enumerate(sorted(group, key=lambda r: -r["simDps"]))
        }
        logged = {
            row["specId"]: index
            for index, row in enumerate(sorted(group, key=lambda r: -r["median"]))
        }
        for build, index in simulated.items():
            moves.setdefault(build, []).append(logged[build] - index)

    bosses = []
    for encounter, group in by_boss.items():
        values = sorted(row["logsToSimRatio"] for row in group)
        agreement = (
            _spearman([(row["simDps"], row["median"]) for row in group])
            if len(group) >= MIN_RANK_SAMPLE
            else None
        )
        bosses.append(
            {
                "encounterId": encounter,
                "encounterName": group[0]["encounterName"],
                "builds": len(group),
                "median": round(statistics.median(values), 4),
                "min": round(values[0], 4),
                "max": round(values[-1], 4),
                "rankAgreement": None if agreement is None else round(agreement, 3),
            }
        )
    bosses.sort(key=lambda entry: entry["median"])

    builds = []
    for build, group in by_build.items():
        adjusted = [row["logsToSimRatio"] / boss_median[row["encounterId"]] for row in group]
        move = moves.get(build) or []
        builds.append(
            {
                "specId": build,
                "displayName": group[0]["displayName"],
                "bosses": len(group),
                "median": round(build_median[build], 4),
                "vsField": (
                    round(statistics.median(adjusted), 4) if len(group) >= MIN_BOSS_SAMPLE else None
                ),
                "vsFieldMin": round(min(adjusted), 4) if len(group) >= MIN_BOSS_SAMPLE else None,
                "vsFieldMax": round(max(adjusted), 4) if len(group) >= MIN_BOSS_SAMPLE else None,
                # Guarded by the same floor as `vsField`: a median movement over
                # two bosses is a coin flip with a decimal point on it.
                "rankMove": (
                    float(round(statistics.median(move), 1))
                    if len(group) >= MIN_BOSS_SAMPLE and move
                    else None
                ),
                "sampleSize": sum(row.get("sampleSize", 0) for row in group),
            }
        )
    builds.sort(key=lambda entry: (entry["vsField"] is None, -(entry["vsField"] or 0)))

    # The obvious way `vsField` could be an artefact rather than a finding: Warcraft
    # Logs pages are ranked, so a build with few logged parses is represented only by
    # its very best players while a popular one's median sits further down its own
    # field. If that drove the ordering, sample size and `vsField` would move
    # together. Published either way -- a weak correlation is what makes the reading
    # above safe to state, and a strong one would be the more important finding.
    bias = _pearson(
        [
            (
                float(row.get("sampleSize") or 0),
                row["logsToSimRatio"] / boss_median[row["encounterId"]],
            )
            for row in rows
            if row.get("sampleSize")
        ]
    )

    return {
        "builds": len(by_build),
        "bosses": bosses,
        "perBuild": builds,
        "varianceExplained": {
            "boss": None if explained_by_boss is None else round(explained_by_boss, 3),
            "build": None if explained_by_build is None else round(explained_by_build, 3),
        },
        # Pooled across bosses on purpose, and low on purpose: it is the number
        # somebody would compute first, and the per-boss figures are what it hides.
        "pooledRankAgreement": (
            None
            if (pooled := _spearman([(row["simDps"], row["median"]) for row in rows])) is None
            else round(pooled, 3)
        ),
        "sampleSizeBias": None if bias is None else round(bias, 3),
        "minRankSample": MIN_RANK_SAMPLE,
        "minBossSample": MIN_BOSS_SAMPLE,
    }


def cmd_logs_analyse(args: Any) -> int:
    """Recompute the analysis block of an already-published verification file.

    Split out from `wowdps verify` because the two have completely different
    preconditions: `verify` needs Warcraft Logs credentials and spends API points, so
    it only runs in CI, while this reads a file that is already in the repository. A
    change to how the readings are derived should not need a fresh download of the
    same rankings to reach the site.
    """
    import json
    import logging
    from pathlib import Path

    log = logging.getLogger(__name__)

    root = Path(args.data)
    tier = args.tier
    if not tier or tier == "latest":
        index = root / "tiers.json"
        if not index.is_file():
            log.error("no tier index at %s -- run `wowdps build` first", index)
            return 1
        tier = json.loads(index.read_text(encoding="utf-8"))["current"]

    path = root / tier / "logs-verification.json"
    if not path.is_file():
        log.error("no verification file at %s -- run `wowdps verify --tier %s` first", path, tier)
        return 1

    document = json.loads(path.read_text(encoding="utf-8"))
    analysis = analyse(document.get("comparisons") or [])
    if analysis is None:
        log.error("%s carries too few comparisons to analyse", path)
        return 1

    if document.get("analysis") == analysis:
        log.info("%s already carries this analysis; nothing written", path)
        return 0

    # Rebuilt in the order `cmd_verify` writes rather than assigned in place: on a
    # file that has no analysis block yet, assignment would append it at the end and
    # the next CI run would rewrite the whole line to move it. A diff is supposed to
    # mean something in this repository.
    document = _in_verify_order(document, analysis)
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info(
        "wrote %s (%d builds over %d bosses; the boss explains %s of the spread)",
        path,
        analysis["builds"],
        len(analysis["bosses"]),
        _as_percent(analysis["varianceExplained"]["boss"]),
    )
    return 0


#: The key order `cmd_verify` writes. Anything the verification file grows later is
#: kept, after these, in whatever order it arrived in.
_VERIFY_KEY_ORDER = (
    "generatedAt",
    "metric",
    "difficulty",
    "note",
    "comparisons",
    "analysis",
    "minSampleSize",
    "withheldForSmallSample",
)


def _in_verify_order(document: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    merged = dict(document, analysis=analysis)
    ordered = {key: merged[key] for key in _VERIFY_KEY_ORDER if key in merged}
    ordered.update({key: value for key, value in merged.items() if key not in ordered})
    return ordered


def _as_percent(value: float | None) -> str:
    return "an unmeasured share" if value is None else f"{value:.0%}"


def add_arguments(parser: Any) -> None:
    parser.add_argument("--data", default="web/public/data", help="dataset directory")
    parser.add_argument(
        "--tier",
        default="latest",
        help="which tier's verification file to analyse, or 'latest' (default)",
    )
