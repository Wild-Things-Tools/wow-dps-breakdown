"""Summarising Warcraft Logs rankings: what is publishable and what is not."""

from __future__ import annotations

import json

import pytest

from wowdps.warcraftlogs import (
    MIN_P95,
    MIN_SAMPLE,
    PointLedger,
    _percentile,
    summarise_rankings,
    top_report_fights,
)


def rankings(*amounts: float) -> dict:
    """An encounter payload shaped the way the Warcraft Logs API returns one."""
    return {
        "id": 1234,
        "name": "Some Boss",
        "characterRankings": {"rankings": [{"amount": a} for a in amounts]},
    }


@pytest.mark.parametrize("count", [0, 1, 2, MIN_SAMPLE - 1])
def test_thin_samples_are_not_published(count):
    """A median of a handful of logs says nothing, and sits next to a sim figure."""
    assert summarise_rankings(rankings(*range(1, count + 1))) is None


def test_a_sample_at_the_threshold_is_published():
    summary = summarise_rankings(rankings(*range(1, MIN_SAMPLE + 1)))
    assert summary is not None
    assert summary["sampleSize"] == MIN_SAMPLE


def test_p95_is_omitted_until_it_means_something():
    """Below MIN_P95 the 95th percentile extrapolates from the single best parse."""
    small = summarise_rankings(rankings(*range(1, MIN_P95)))
    assert small is not None and "p95" not in small

    big = summarise_rankings(rankings(*range(1, MIN_P95 + 1)))
    assert big is not None and "p95" in big


def test_p95_lands_between_median_and_max():
    """It shipped once returning the *minimum* -- a 95th percentile below the median."""
    summary = summarise_rankings(rankings(*range(1, 101)))
    assert summary is not None
    assert summary["median"] < summary["p95"] <= summary["max"]


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([10.0], 0.95, 10.0),
        ([10.0, 20.0], 0.5, 15.0),
        ([0.0, 100.0], 0.95, 95.0),
        # 0.95 * (100 - 1) = 94.05, so five percent of the way from 95 to 96.
        (list(map(float, range(1, 101))), 0.95, 95.05),
    ],
)
def test_percentile_interpolates(values, fraction, expected):
    assert _percentile(values, fraction) == pytest.approx(expected)


def test_non_numeric_entries_are_ignored():
    noisy = {
        "id": 1,
        "name": "Boss",
        "characterRankings": {
            "rankings": [{"amount": a} for a in range(1, MIN_SAMPLE + 1)]
            + [{"amount": None}, {"not": "a ranking"}, "junk"],
        },
    }
    summary = summarise_rankings(noisy)
    assert summary is not None and summary["sampleSize"] == MIN_SAMPLE


# --------------------------------------------------------------------------------
# From "top parses for this boss" to "logs to read"
# --------------------------------------------------------------------------------


def ranking_entry(code: str, fight_id: int, amount: float = 100.0) -> dict:
    return {"amount": amount, "report": {"code": code, "fightID": fight_id, "startTime": 1}}


def test_ranking_entries_carry_the_route_to_the_actual_log():
    """This is what makes a fight probe cheap: no report search is needed, because
    every ranking already names the report and fight it came from."""
    encounter = {
        "id": 3180,
        "characterRankings": {"rankings": [ranking_entry("aaa", 4), ranking_entry("bbb", 7)]},
    }
    assert top_report_fights(encounter, 5) == [("aaa", 4), ("bbb", 7)]


def test_two_parses_from_one_pull_are_one_fight_not_two():
    """Twenty players ranked on the same kill would otherwise look like a sample of
    twenty fights while describing exactly one."""
    encounter = {
        "characterRankings": {
            "rankings": [ranking_entry("aaa", 4), ranking_entry("aaa", 4), ranking_entry("bbb", 2)]
        }
    }
    assert top_report_fights(encounter, 5) == [("aaa", 4), ("bbb", 2)]


def test_the_report_limit_is_the_cost_dial_and_is_respected():
    encounter = {"characterRankings": {"rankings": [ranking_entry(f"r{i}", i) for i in range(10)]}}
    assert len(top_report_fights(encounter, 3)) == 3


def test_rankings_returned_as_a_json_string_are_still_read():
    """``characterRankings`` is an untyped JSON scalar in the schema, and the site
    has been seen to return it both ways."""
    encounter = {"characterRankings": json.dumps({"rankings": [ranking_entry("aaa", 1)]})}
    assert top_report_fights(encounter, 5) == [("aaa", 1)]


def test_entries_without_a_report_are_skipped_rather_than_crashing():
    encounter = {
        "characterRankings": {
            "rankings": [{"amount": 1}, {"report": {"code": "aaa"}}, ranking_entry("bbb", 3)]
        }
    }
    assert top_report_fights(encounter, 5) == [("bbb", 3)]


# --------------------------------------------------------------------------------
# What a pass costs, measured rather than predicted
# --------------------------------------------------------------------------------


def reading(spent: float) -> dict:
    return {
        "rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": spent, "pointsResetIn": 900}
    }


def test_the_run_cost_is_the_difference_between_the_first_and_last_reading():
    """Warcraft Logs does not publish a cost formula, so the only defensible figure
    is the one read back out of rateLimitData."""
    ledger = PointLedger()
    ledger.record("start", reading(120.0))
    ledger.record("events", reading(151.5))
    ledger.record("end", reading(160.0))
    assert ledger.spent == 40.0
    assert ledger.limit_per_hour == 3600


def test_a_ledger_that_never_saw_a_reading_reports_no_cost_rather_than_zero():
    ledger = PointLedger()
    ledger.record("query", {"reportData": {}})
    assert ledger.spent is None
    assert ledger.to_json()["pointsSpentThisRun"] is None


def test_cache_hits_are_counted_apart_from_paid_queries():
    """A warm cache is what makes iterating on the extraction free, so the report
    has to be able to say how much of a run was actually paid for."""
    ledger = PointLedger()
    ledger.record("a", reading(10.0))
    ledger.record("b", reading(10.0), cached=True)
    payload = ledger.to_json()
    assert payload["queries"] == 1 and payload["cacheHits"] == 1
