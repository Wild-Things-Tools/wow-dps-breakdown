"""The readings that make the logs cross-check worth publishing.

Every row of the raw file is below 1.0 by construction, so the tests here are all
about what varies *between* rows: the boss, the build, and whether the simulated
ordering survives.
"""

from __future__ import annotations

import json

import pytest

from wowdps.logsanalysis import MIN_BOSS_SAMPLE, analyse, cmd_logs_analyse


def row(build, boss, *, sim, median, sample=30):
    return {
        "specId": build,
        "displayName": build.replace("_", " ").title(),
        "encounterId": hash(boss) % 10000,
        "encounterName": boss,
        "sampleSize": sample,
        "median": median,
        "max": median * 1.2,
        "simDps": sim,
        "logsToSimRatio": round(median / sim, 4),
    }


def test_a_pure_boss_effect_is_attributed_to_the_boss_and_not_to_the_builds():
    """Three builds, three bosses, and every build loses exactly the same share on
    each: the whole spread is the encounter, and nothing is left over."""
    rows = [
        row(build, boss, sim=sim, median=sim * keep)
        for boss, keep in (("easy", 0.9), ("medium", 0.7), ("hard", 0.5))
        for build, sim in (("a", 100), ("b", 200), ("c", 300))
    ]
    analysis = analyse(rows)
    assert analysis is not None
    assert analysis["varianceExplained"]["boss"] == 1.0
    # And every build sits exactly on the field, because none of them is different.
    assert {entry["vsField"] for entry in analysis["perBuild"]} == {1.0}


def test_a_build_that_loses_more_than_the_field_is_named_even_when_the_boss_dominates():
    rows = []
    for boss, keep in (("easy", 0.9), ("medium", 0.7), ("hard", 0.5)):
        for build, penalty in (("a", 1.0), ("b", 1.0), ("fragile", 0.5)):
            rows.append(row(build, boss, sim=100, median=100 * keep * penalty))
    analysis = analyse(rows)
    assert analysis is not None
    by_build = {entry["specId"]: entry for entry in analysis["perBuild"]}
    assert by_build["fragile"]["vsField"] == pytest.approx(0.5, abs=1e-6)
    assert by_build["a"]["vsField"] == pytest.approx(1.0, abs=1e-6)
    # Ordered best-first, so the view can draw it without re-sorting.
    assert analysis["perBuild"][-1]["specId"] == "fragile"


def test_a_build_seen_on_too_few_bosses_gets_no_reading_rather_than_a_thin_one():
    """The boss adjustment averages the encounter out. Over one boss it averages
    nothing out, and the number would read as a property of the build."""
    rows = [
        row("a", "one", sim=100, median=70),
        row("b", "one", sim=100, median=60),
        row("a", "two", sim=100, median=80),
        row("b", "two", sim=100, median=50),
    ]
    analysis = analyse(rows)
    assert analysis is not None
    assert MIN_BOSS_SAMPLE == 3
    assert all(entry["vsField"] is None for entry in analysis["perBuild"])
    # And the same floor covers the rank movement, which is just as thin.
    assert all(entry["rankMove"] is None for entry in analysis["perBuild"])


def test_rank_agreement_is_per_boss_because_pooling_hides_both_directions():
    """One boss where the logs agree with the sim and one where they invert. Pooled,
    the two cancel; per boss, they are the finding."""
    agree = [row(f"b{i}", "agree", sim=100 + i * 10, median=100 + i * 10) for i in range(10)]
    invert = [row(f"b{i}", "invert", sim=100 + i * 10, median=200 - i * 10) for i in range(10)]
    analysis = analyse(agree + invert)
    assert analysis is not None
    by_boss = {entry["encounterName"]: entry for entry in analysis["bosses"]}
    assert by_boss["agree"]["rankAgreement"] == 1.0
    assert by_boss["invert"]["rankAgreement"] == -1.0
    assert abs(analysis["pooledRankAgreement"] or 0) < 0.5


def test_a_boss_with_too_few_builds_gets_no_correlation():
    rows = [row(f"b{i}", "thin", sim=100 + i, median=100 + i) for i in range(4)]
    rows += [row(f"b{i}", "wide", sim=100 + i, median=100 + i) for i in range(10)]
    analysis = analyse(rows)
    assert analysis is not None
    by_boss = {entry["encounterName"]: entry for entry in analysis["bosses"]}
    assert by_boss["thin"]["rankAgreement"] is None
    assert by_boss["wide"]["rankAgreement"] == 1.0


def test_the_sample_size_check_is_published_whichever_way_it_comes_out():
    """`vsField` would be an artefact rather than a finding if it tracked how many
    people log the build. The correlation is published so that is checkable."""
    rows = []
    for boss in ("one", "two", "three"):
        for index, build in enumerate(("a", "b", "c", "d")):
            rows.append(row(build, boss, sim=100, median=70 + index, sample=10 + index * 40))
    analysis = analyse(rows)
    assert analysis is not None
    # Contrived to correlate perfectly: more parses, better ratio.
    assert analysis["sampleSizeBias"] == pytest.approx(1.0, abs=0.05)


def test_too_few_comparisons_analyse_to_nothing_rather_than_to_zeroes():
    assert analyse([]) is None
    assert analyse([row("a", "one", sim=100, median=70)]) is None


def test_rows_missing_the_numbers_are_skipped_rather_than_crashing():
    rows = [row("a", "one", sim=100, median=70), {"specId": "b", "displayName": "B"}]
    rows += [row("a", "two", sim=100, median=80)]
    assert analyse(rows) is not None


def write_verification(root, tier, rows, analysis=None):
    directory = root / tier
    directory.mkdir(parents=True, exist_ok=True)
    (root / "tiers.json").write_text(
        json.dumps({"current": tier, "tiers": [{"id": tier}]}), encoding="utf-8"
    )
    document = {
        "generatedAt": "2026-08-14T00:00:00+00:00",
        "metric": "dps",
        "difficulty": 5,
        "note": "n",
        "comparisons": rows,
        "minSampleSize": 5,
        "withheldForSmallSample": 3,
    }
    if analysis is not None:
        document["analysis"] = analysis
    (directory / "logs-verification.json").write_text(json.dumps(document), encoding="utf-8")
    return directory / "logs-verification.json"


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_the_command_recomputes_from_a_committed_file_without_credentials(tmp_path):
    rows = [
        row(build, boss, sim=100, median=100 * keep)
        for boss, keep in (("one", 0.9), ("two", 0.7), ("three", 0.5))
        for build in ("a", "b", "c")
    ]
    path = write_verification(tmp_path, "MID2", rows)

    assert cmd_logs_analyse(Args(data=str(tmp_path), tier="latest")) == 0
    document = json.loads(path.read_text())
    assert document["analysis"]["builds"] == 3
    # Written where `cmd_verify` writes it, so a CI run does not reorder the file.
    assert list(document) == [
        "generatedAt",
        "metric",
        "difficulty",
        "note",
        "comparisons",
        "analysis",
        "minSampleSize",
        "withheldForSmallSample",
    ]


def test_recomputing_an_unchanged_analysis_writes_nothing(tmp_path):
    """Determinism again: a run that found the same answer must leave no diff."""
    rows = [
        row(build, boss, sim=100, median=100 * keep)
        for boss, keep in (("one", 0.9), ("two", 0.7), ("three", 0.5))
        for build in ("a", "b", "c")
    ]
    path = write_verification(tmp_path, "MID2", rows)
    cmd_logs_analyse(Args(data=str(tmp_path), tier="MID2"))
    before = path.read_bytes()
    assert cmd_logs_analyse(Args(data=str(tmp_path), tier="MID2")) == 0
    assert path.read_bytes() == before


def test_a_missing_file_is_an_error_with_the_command_to_run(tmp_path):
    (tmp_path / "tiers.json").write_text(json.dumps({"current": "MID2"}), encoding="utf-8")
    assert cmd_logs_analyse(Args(data=str(tmp_path), tier="MID2")) == 1
