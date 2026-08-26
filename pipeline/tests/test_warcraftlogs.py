"""Summarising Warcraft Logs rankings: what is publishable and what is not."""

from __future__ import annotations

import json

import pytest

from wowdps import warcraftlogs
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


def routes(selected) -> list[tuple[str, int]]:
    """Drop the kill timestamp, which these cases are not about."""
    return [(code, fight_id) for code, fight_id, _ in selected]


def test_ranking_entries_carry_the_route_to_the_actual_log():
    """This is what makes a fight probe cheap: no report search is needed, because
    every ranking already names the report and fight it came from."""
    encounter = {
        "id": 3180,
        "characterRankings": {"rankings": [ranking_entry("aaa", 4), ranking_entry("bbb", 7)]},
    }
    assert routes(top_report_fights(encounter, 5)) == [("aaa", 4), ("bbb", 7)]


def test_two_parses_from_one_pull_are_one_fight_not_two():
    """Twenty players ranked on the same kill would otherwise look like a sample of
    twenty fights while describing exactly one."""
    encounter = {
        "characterRankings": {
            "rankings": [ranking_entry("aaa", 4), ranking_entry("aaa", 4), ranking_entry("bbb", 2)]
        }
    }
    assert routes(top_report_fights(encounter, 5)) == [("aaa", 4), ("bbb", 2)]


def test_the_report_limit_is_the_cost_dial_and_is_respected():
    encounter = {"characterRankings": {"rankings": [ranking_entry(f"r{i}", i) for i in range(10)]}}
    assert len(top_report_fights(encounter, 3)) == 3


def test_rankings_returned_as_a_json_string_are_still_read():
    """``characterRankings`` is an untyped JSON scalar in the schema, and the site
    has been seen to return it both ways."""
    encounter = {"characterRankings": json.dumps({"rankings": [ranking_entry("aaa", 1)]})}
    assert routes(top_report_fights(encounter, 5)) == [("aaa", 1)]


def test_entries_without_a_report_are_skipped_rather_than_crashing():
    encounter = {
        "characterRankings": {
            "rankings": [{"amount": 1}, {"report": {"code": "aaa"}}, ranking_entry("bbb", 3)]
        }
    }
    assert routes(top_report_fights(encounter, 5)) == [("bbb", 3)]


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


def test_the_boss_list_comes_from_the_tier_and_a_tier_with_none_is_a_refusal(
    tmp_path, monkeypatch, caplog
):
    """A season whose raid has not opened yet must not borrow the last one's bosses.

    The published MID2 comparison is what the old fallback ("the newest zone
    Warcraft Logs is ranking") cost: 192 rows of Season 2 sim output against Season
    1 kills, under a Season 2 heading. It is the failure shape this project keeps
    running into -- a full set of plausible numbers answering a question nobody
    asked -- so the fallback is gone and the empty case is an error rather than a
    quiet substitution.
    """
    import argparse
    import json as _json

    from wowdps import warcraftlogs

    root = tmp_path / "data"
    (root / "MID9").mkdir(parents=True)
    (root / "tiers.json").write_text(_json.dumps({"current": "MID9", "tiers": []}))
    (root / "MID9" / "index.json").write_text(_json.dumps({"specs": []}))

    monkeypatch.setenv("WCL_CLIENT_ID", "id")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "secret")

    args = argparse.Namespace(
        data=str(root), tier="MID9", encounter=None, difficulty=5, metric="dps"
    )

    with caplog.at_level("ERROR"):
        assert warcraftlogs.cmd_verify(args) == 1

    message = caplog.text
    assert "no fight profiles for tier MID9" in message
    # The way out is named, and so is the reason there is no fallback.
    assert "fight-zones" in message
    assert "previous" in message


# --------------------------------------------------------------------------------
# The bracketing readings must not go through the response cache
# --------------------------------------------------------------------------------


class _CountingTransport:
    """A stub post() whose hourly counter moves with every real request.

    The counter moving is the whole point: a client that serves the second reading
    out of the first one's cache entry cannot see it move, and that is exactly the
    failure this pins.
    """

    def __init__(self, start: float = 100.0, per_call: float = 9.0) -> None:
        self.spent = start
        self.per_call = per_call
        self.posts = 0

    def __call__(self, *args, **kwargs):
        self.posts += 1
        self.spent += self.per_call
        payload = {
            "data": {
                "rateLimitData": {
                    "limitPerHour": 3600,
                    "pointsSpentThisHour": self.spent,
                    "pointsResetIn": 900,
                },
                "reportData": {"report": {"code": "aBcD1234"}},
            }
        }
        return type("Response", (), {"status_code": 200, "json": lambda self: payload})()


def _client(tmp_path):
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    client = WarcraftLogsClient(Credentials("id", "secret"), cache_dir=tmp_path / "cache")
    client._token = "token"
    return client


def test_the_bracketing_readings_bypass_the_response_cache(tmp_path):
    """Without this the cost of a pass can never be measured, at any size.

    `RATE_LIMIT_QUERY` takes no variables, so both readings hash to the same
    (query, {}). Cached, the second is served from the first one's response, the
    two readings are equal by construction, and `pointsSpentThisRun` is 0.0 for
    every run -- which `describe_cost` correctly reports as UNMEASURED. The probe
    exists to take exactly this measurement, and the workflow always passes
    --cache, so the default mode could never produce the number it exists for.
    """
    client = _client(tmp_path)
    transport = _CountingTransport()
    client._client.post = transport

    first = client.rate_limit()
    last = client.rate_limit()

    assert transport.posts == 2, "the second reading was served from the cache"
    assert first["pointsSpentThisHour"] == 109.0
    assert last["pointsSpentThisHour"] == 118.0
    assert client.ledger.spent == 9.0
    # And nothing was written to disk for them, so a later run cannot restore a
    # reading the API returned hours ago and call it "before".
    assert not list((tmp_path / "cache").glob("*.json"))


def test_every_other_query_is_still_cached(tmp_path):
    """The control. A bypass that leaked to the rest would make the cache useless
    and every re-run of an extraction cost points again."""
    client = _client(tmp_path)
    transport = _CountingTransport()
    client._client.post = transport

    client.query("query Q { reportData { report { code } } }", {"code": "aBcD1234"})
    client.query("query Q { reportData { report { code } } }", {"code": "aBcD1234"})

    assert transport.posts == 1
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


def test_the_ledger_counts_real_requests_and_keeps_rate_limit_headers():
    """Points are not the only budget, and this side was blind to the other one.

    `rateLimitData` arrives in the response BODY and meters points; anything the
    service says about requests per hour arrives in the HEADERS, which nothing read.
    A pass can therefore sit comfortably inside 18,000 points and hit a ceiling it
    never measured — and this module's own 429 message would call that "the hourly
    point budget is spent", which would be the wrong diagnosis.
    """
    ledger = warcraftlogs.PointLedger()
    ledger.note_response(
        {
            "X-RateLimit-Limit": "800",
            "x-ratelimit-remaining": "412",
            "Content-Type": "application/json",
        }
    )

    assert ledger.requests_sent == 1
    # Matched on the folded name, because the service's capitalisation and hyphenation
    # are not something this side gets to assume.
    assert ledger.request_headers == {
        "x-ratelimit-limit": "800",
        "x-ratelimit-remaining": "412",
    }
    assert "content-type" not in ledger.request_headers


def test_a_cache_hit_is_not_a_request(tmp_path):
    """The ratio this exists to measure would otherwise be wrong in the flattering
    direction: a warm cache would read as a pass that sent hundreds of requests.

    Driven through `client.query()` rather than `ledger.record()`, because the count
    happens in `query()` and a test that calls the recorder directly guards nothing
    there. Written the direct way first, it passed unchanged while the counter was
    deliberately incremented on the cache-hit branch -- a canary that did not sing.
    """
    client = _client(tmp_path)
    transport = _CountingTransport()
    client._client.post = transport
    document = "query Q { reportData { report { code } } }"

    client.query(document, {"code": "aBcD1234"})
    assert transport.posts == 1
    assert client.ledger.requests_sent == 1

    # Same (query, variables): served from disk, so the service never sees it.
    client.query(document, {"code": "aBcD1234"})
    assert transport.posts == 1, "the second call reached the network"
    assert client.ledger.requests_sent == 1, "a cache hit was counted as a request"


def test_headers_without_an_items_method_are_survived_not_raised_on():
    """A budget reading must never be the thing that kills a pass."""
    ledger = warcraftlogs.PointLedger()
    ledger.note_response(object())
    assert ledger.requests_sent == 1
    assert ledger.request_headers == {}


def test_a_response_with_no_headers_at_all_is_survived():
    """The guard has to sit at reaching the headers, not only at parsing them.

    A response object without `.headers` is exactly what a test double is, and
    `response.headers` raised AttributeError on two existing tests before the guard
    moved up a level. Same rule either way: a budget reading must never be the thing
    that kills a pass.
    """
    ledger = warcraftlogs.PointLedger()
    ledger.note_response(None)
    assert ledger.requests_sent == 1
    assert ledger.request_headers == {}
