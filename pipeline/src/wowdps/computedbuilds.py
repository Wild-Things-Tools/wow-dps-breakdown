"""The calibration gate, and the document the site reads.

Two things live here because they are two halves of one rule the owner stated:
*"wenn es etwas komplettes von simc gibt, dann sollte das verwendet werden, außer
etwas errechnetes ist besser"*. simc's build is the default; ours replaces it only
when it measurably wins. The **gate** decides whether a search is trusted enough to
publish at all; the **document** carries both sides so the site can show them together
and never substitute one silently.

The pass criterion, fixed before any calibration was run
--------------------------------------------------------
A criterion chosen after seeing the numbers is not a gate, so the two constants below
were written, committed and justified before the first calibration row existed. Both
must hold:

* ``PASS_MIN_NOT_BEHIND`` -- on at least **80%** of calibrated builds the blind search
  must **tie or beat** simc's shipped build. "Behind" means simc wins by more than the
  tie band; anything inside the band is a tie and counts as not behind.
* ``PASS_MAX_LOSS`` -- on **no** calibrated build may simc beat the search winner by
  more than **2%**.

Why those and not something else. simc's shipped build is *inside* the space the
search covers -- it is one choice assignment over the same node set -- so a search that
worked perfectly would recover it or better on every build, and "tie or beat" is the
natural bar rather than a generous one. It is not set at 100% because the search is
blind, bounded in breadth and stopped at a fixed precision, so an occasional miss is a
property of the budget rather than of the method; 80% leaves room for that while still
failing a search that is systematically behind. The second constant exists because the
first can be satisfied by a search that is *catastrophically* wrong on one build, and
one build published 5% below simc's is worse than a run that publishes nothing.

The failure mode both are aimed at is the one this repository keeps finding: a number
that reads like a measurement and is the absence of one. A search that reliably lands
below simc's build where the answer is known has no business producing a number where
it is not.

**A failed gate is a result, not an error.** ``cmd_build_search`` publishes nothing
from a failed calibration and says so; it does not retune and call the retune a
calibration.

What is published when the gate fails, and why that is not a contradiction
---------------------------------------------------------------------------
Search results are gated. **Repairs are not**, and the difference is the claim rather
than the confidence. A repair (``talentrepair``) asserts only *"simc refuses the hash
its own profile ships, and this is that hash with the correction the trait table
forces"* -- which is checkable by running simc, contains no optimality claim, and is
therefore not something calibration could ever be evidence about. Publishing a repaired
build labelled as a repair while withholding a search result labelled as a search is
the two claims being kept apart, not an inconsistency.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .buildsearch import Candidate, Measurement, SearchOutcome, separated, tie_band

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: See the module docstring. Fixed in advance; do not move them to fit a run.
PASS_MIN_NOT_BEHIND = 0.80
PASS_MAX_LOSS = 0.02

#: Fields that describe *the run* rather than the data. Same settle rule as the
#: manifest and ``fights.json``: a wall-clock stamp that rewrites itself every run
#: means every run commits, and the point of deterministic sims is that a quiet run
#: leaves nothing to commit.
_PROVENANCE = ("generatedAt",)


# --------------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationRow:
    """One build where the answer is known, and what the blind search made of it."""

    build_id: str
    label: str
    #: simc's shipped build, measured on the anchor as a profileset.
    simc: Measurement
    #: The blind search's winner, measured the same way in the same run.
    found: Measurement | None
    variants_evaluated: int
    #: True when the search's winning loadout is simc's own, node for node. Not part
    #: of the gate -- it is the strongest evidence available and also the rarest, and a
    #: gate that demanded it would be demanding the search get lucky.
    recovered_simc_build: bool = False

    @property
    def margin(self) -> float | None:
        """Signed relative lead of the search winner over simc's build."""
        if self.found is None or self.simc.dps <= 0:
            return None
        return self.found.dps / self.simc.dps - 1

    @property
    def band(self) -> float | None:
        if self.found is None:
            return None
        return tie_band(self.found.dps_error, self.simc.dps_error)

    @property
    def verdict(self) -> str:
        """``search-ahead`` / ``tie`` / ``search-behind`` / ``no-candidate``."""
        if self.found is None:
            return "no-candidate"
        if separated(self.found, self.simc):
            return "search-ahead"
        if separated(self.simc, self.found):
            return "search-behind"
        return "tie"

    def to_json(self) -> dict:
        return {
            "build": self.build_id,
            "label": self.label,
            "simcDps": round(self.simc.dps, 1),
            "simcError": round(self.simc.dps_error, 4),
            "searchDps": None if self.found is None else round(self.found.dps, 1),
            "searchError": None if self.found is None else round(self.found.dps_error, 4),
            "margin": None if self.margin is None else round(self.margin, 6),
            "tieBand": None if self.band is None else round(self.band, 6),
            "verdict": self.verdict,
            "variantsEvaluated": self.variants_evaluated,
            "recoveredSimcBuild": self.recovered_simc_build,
            "iterations": self.simc.iterations,
        }


@dataclass(frozen=True)
class Calibration:
    """The gate's verdict over a set of rows, with the criterion it was judged against."""

    rows: tuple[CalibrationRow, ...]
    min_not_behind: float = PASS_MIN_NOT_BEHIND
    max_loss: float = PASS_MAX_LOSS

    @property
    def judged(self) -> tuple[CalibrationRow, ...]:
        """Rows the gate can actually judge. A build the search produced no candidate
        for is counted as behind -- a search that finds nothing is not neutral -- but a
        build with no simc measurement at all cannot be judged and is excluded."""
        return tuple(row for row in self.rows if row.simc.dps > 0)

    @property
    def behind(self) -> tuple[CalibrationRow, ...]:
        return tuple(row for row in self.judged if row.verdict in ("search-behind", "no-candidate"))

    @property
    def not_behind_share(self) -> float:
        judged = self.judged
        if not judged:
            return 0.0
        return 1.0 - len(self.behind) / len(judged)

    @property
    def worst_loss(self) -> float:
        """The largest amount simc beats the search by, as a positive fraction.

        A row with no candidate at all counts as an unbounded loss: nothing was found,
        so no margin bounds it, and treating it as zero would let "found nothing"
        clear a criterion about how badly the search can lose.
        """
        worst = 0.0
        for row in self.judged:
            if row.verdict == "no-candidate":
                return float("inf")
            if row.verdict == "search-behind" and row.margin is not None:
                worst = max(worst, -row.margin)
        return worst

    @property
    def passed(self) -> bool:
        if not self.judged:
            return False
        return self.not_behind_share >= self.min_not_behind and self.worst_loss <= self.max_loss

    def criterion(self) -> str:
        return (
            f"fixed in advance: the blind search must tie or beat simc's shipped build on "
            f"at least {self.min_not_behind:.0%} of calibrated builds, and simc must not "
            f"beat it by more than {self.max_loss:.0%} on any of them"
        )

    def summary(self) -> str:
        judged = self.judged
        if not judged:
            return "no build could be judged: calibration did not run"
        counts: dict[str, int] = {}
        for row in judged:
            counts[row.verdict] = counts.get(row.verdict, 0) + 1
        worst = "unbounded" if self.worst_loss == float("inf") else f"{self.worst_loss:.2%}"
        return (
            f"{len(judged)} build(s) judged: "
            + ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
            + f"; not behind on {self.not_behind_share:.0%}, worst loss {worst}; "
            + ("PASSED" if self.passed else "FAILED")
        )

    def to_json(self) -> dict:
        return {
            "criterion": self.criterion(),
            "minNotBehind": self.min_not_behind,
            "maxLoss": self.max_loss,
            "passed": self.passed,
            "judged": len(self.judged),
            "notBehindShare": round(self.not_behind_share, 4),
            "worstLoss": (None if self.worst_loss == float("inf") else round(self.worst_loss, 6)),
            "recoveredSimcBuild": sum(1 for r in self.judged if r.recovered_simc_build),
            "summary": self.summary(),
            "rows": [row.to_json() for row in self.rows],
        }


# --------------------------------------------------------------------------------
# The published document
# --------------------------------------------------------------------------------


def contender_json(
    candidate: Candidate,
    measurement: Measurement,
    *,
    hero_talent: str | None = None,
    outcome: SearchOutcome | None = None,
    harvest: dict | None = None,
) -> dict:
    """One side of the comparison, in ``DpsComputedContender``'s shape.

    ``dpsError`` is required rather than optional and this is the reason: the site's
    ``usable()`` refuses a contender whose error cannot be read, because
    ``combinedNoise(undefined, x)`` is NaN and a candidate three percent ahead then
    classifies as a **tie**. An omitted error is not a small imprecision here; it
    silently reverses the verdict.
    """
    entry: dict = {
        "origin": candidate.origin,
        "label": candidate.label,
        "talentHash": candidate.talent_hash,
        "heroTalent": hero_talent,
        "dps": round(measurement.dps, 1),
        "dpsError": round(measurement.dps_error, 4),
        "iterations": measurement.iterations,
        "priorityDps": (
            None if measurement.priority_dps is None else round(measurement.priority_dps, 1)
        ),
    }
    if outcome is not None:
        entry["search"] = {
            "method": outcome.method(),
            "description": outcome.description(),
            "seed": outcome.seed_value,
            "variantsEvaluated": outcome.variants_evaluated,
            "startedFrom": candidate.parent,
        }
    if harvest is not None:
        entry["harvest"] = harvest
    return entry


@dataclass
class SpecEntry:
    """One row of the document: one build, one scenario, one target count.

    The join key is ``(id, scenario, targets)`` and all three are emitted. Joining on
    the id alone would hand a reader on the ten-target charts a verdict measured at one
    target, and would make every row after the first unreachable when a build is
    computed at two target counts.
    """

    build_id: str
    scenario: str
    targets: int
    searched: bool
    simc: dict | None
    best: dict | None
    runner_up: dict | None
    anchor: dict
    caveats: list[str]

    def to_json(self) -> dict:
        return {
            "id": self.build_id,
            "scenario": self.scenario,
            "targets": self.targets,
            "searched": self.searched,
            "simc": self.simc,
            "best": self.best,
            "runnerUp": self.runner_up,
            "anchor": self.anchor,
            "caveats": self.caveats,
        }


def build_document(
    tier: str,
    entries: list[SpecEntry],
    *,
    iterations: int,
    deterministic: bool,
    builds_available: int,
    calibration: Calibration | None,
    notes: list[str] | None = None,
) -> dict:
    """The whole ``computed-builds.json``.

    ``coverage`` is carried rather than inferred: the site's own field comment states
    the project rule that a view must never read "N of M" off an array length, and the
    two numbers differ the moment a shard stops early.
    """
    document: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": tier,
        "note": (
            "Builds this project computed, beside SimulationCraft's own where simc "
            "ships one. simc's build is never replaced here -- both are published and "
            "the site decides what to present, so a computed build has to win by more "
            "than the two runs' combined sampling error to be shown as the answer. "
            "Every comparison is profileset against profileset on one anchored kit, so "
            "the difference is the talents."
        ),
        "settings": {"iterations": iterations, "deterministic": deterministic},
        "coverage": {"specs": len(entries), "specsAvailable": builds_available},
        "specs": [entry.to_json() for entry in entries],
    }
    if calibration is not None:
        document["calibration"] = calibration.to_json()
    if notes:
        document["notes"] = list(notes)
    return document


def write_computed_builds(out_dir: Path, document: dict) -> Path:
    """Write ``<out_dir>/computed-builds.json``, keeping the stamp when nothing moved.

    The sims are deterministic, so a re-run that found the same answer should leave
    nothing to commit -- which is what makes a diff in the history mean something
    actually changed. Same rule as ``_settle_provenance`` in ``dataset.py``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "computed-builds.json"
    if path.is_file():
        try:
            published = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            published = None
        if isinstance(published, dict):
            settled = dict(document)
            for key in _PROVENANCE:
                if key in published:
                    settled[key] = published[key]
            if settled == published:
                document = published
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    return path
