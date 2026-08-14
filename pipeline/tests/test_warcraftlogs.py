"""Summarising Warcraft Logs rankings: what is publishable and what is not."""

from __future__ import annotations

import pytest

from wowdps.warcraftlogs import MIN_P95, MIN_SAMPLE, _percentile, summarise_rankings


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
