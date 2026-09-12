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


def reading(spent: float, resets_in: int = 900) -> dict:
    return {
        "rateLimitData": {
            "limitPerHour": 3600,
            "pointsSpentThisHour": spent,
            "pointsResetIn": resets_in,
        }
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


def test_a_cached_response_does_not_move_the_budget_readings():
    """Measured on run 34035705116: a cache hit pushed a PREVIOUS run's counter into
    the ledger, and the published block read ``pointsSpentThisRun: -1524.0``.

    The cached payload here carries a reading from *before* the fresh ones, which is
    exactly the shape a restored `actions/cache` produces. Nothing about it may reach
    `first_reading`, `last_reading`, `limitPerHour` or `pointsResetIn` -- a cached
    response is a record of then, and every one of those asks about now.
    """
    ledger = PointLedger()
    # The ORDER is the whole fixture. A cache hit in the middle is repaired by the
    # next fresh reading, so a ledger built that way passes with the fix reverted --
    # measured, the canary stayed green. The published defect had the hit LAST, and
    # a run served from a restored `actions/cache` ends that way by construction.
    ledger.record("first", reading(4005.27))
    ledger.record("fresh", reading(4340.19))
    ledger.record("stale", reading(2480.27, resets_in=1), cached=True)

    assert (ledger.first_reading, ledger.last_reading) == (4005.27, 4340.19)
    payload = ledger.to_json()
    assert payload["pointsSpentThisRun"] == 334.92
    assert payload["pointsSpentThisHour"] == 4340.19
    assert payload["counterWentBackwards"] is False
    # Nothing else off the cached response either: `pointsResetIn` is as much a
    # reading of "then" as the counter is.
    assert payload["pointsResetIn"] == 900
    # And the hit is still counted, because how much of a run was paid for is the
    # other question this block answers.
    assert payload["queries"] == 2 and payload["cacheHits"] == 1


def test_a_run_that_OPENS_on_a_cache_hit_does_not_take_its_floor_from_it():
    """The other end of the same rule, and the one a warm `actions/cache` hits first.

    A stale low reading taken as `first_reading` inflates the run's cost instead of
    making it negative -- the flattering direction, and the one nobody checks.
    """
    ledger = PointLedger()
    ledger.record("stale", reading(10.0), cached=True)
    ledger.record("real", reading(4005.27))
    ledger.record("real", reading(4340.19))

    assert ledger.first_reading == 4005.27
    assert ledger.to_json()["pointsSpentThisRun"] == 334.92


def test_a_counter_that_went_backwards_is_unmeasured_rather_than_a_negative_number():
    """The rule that survives from #151 even though its diagnosis did not.

    With the cache fix above, every reading in a ledger comes from this run's own
    responses in order, so a backwards counter means the hour rolled over between
    two of them. That is a third state beside "no reading" and "did not move", and
    it is never a negative number, never ``abs()`` and never clamped to zero.
    """
    ledger = PointLedger()
    ledger.record("first", reading(4004.27))
    ledger.record("last", reading(2480.27))

    assert ledger.spend_state == warcraftlogs.SPEND_WENT_BACKWARDS
    assert ledger.spent is None
    payload = ledger.to_json()
    assert payload["pointsSpentThisRun"] is None
    assert payload["counterWentBackwards"] is True
    # The raw readings stay: they are why the state is visible at all, and a reader
    # can form the lower bound from them.
    assert (payload["firstReading"], payload["lastReading"]) == (4004.27, 2480.27)

    sentence = warcraftlogs.spend_sentence(ledger)
    assert "UNMEASURED" in sentence and "BACKWARDS" in sentence
    assert "-1524" not in sentence


def test_the_three_unmeasured_states_are_told_apart_in_words():
    """Three different findings, and a reader has to be able to act on which one."""
    nothing = PointLedger()
    nothing.record("q", {"reportData": {}})
    still = PointLedger()
    still.record("a", reading(10.0))
    still.record("b", reading(10.0))
    back = PointLedger()
    back.record("a", reading(10.0))
    back.record("b", reading(1.0))
    moved = PointLedger()
    moved.record("a", reading(10.0))
    moved.record("b", reading(12.5))

    said = [warcraftlogs.spend_sentence(one) for one in (nothing, still, back, moved)]
    assert said[0] != said[1] != said[2]
    assert len({said[0], said[1], said[2]}) == 3
    assert said[3] == "2.5 points"


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


# ── the httpx mapping, the 429 class and the reset field ───────────────────────


def _bare_client():
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    client = WarcraftLogsClient(Credentials("id", "secret"))
    client._token = "token"
    return client


def test_an_httpx_timeout_is_a_warcraftlogs_error_not_a_crash():
    """A read timeout was the one failure that escaped every `except
    WarcraftLogsError` around a guild's fetch and took a whole pass down."""
    import httpx

    from wowdps.warcraftlogs import WarcraftLogsError

    client = _bare_client()

    def post(*_a, **_k):
        raise httpx.ReadTimeout("slow")

    client._client.post = post
    with pytest.raises(WarcraftLogsError, match="ReadTimeout"):
        client.query("query Q { x }", {}, cache=False)


def test_any_httpx_transport_failure_maps_the_same_way():
    import httpx

    from wowdps.warcraftlogs import WarcraftLogsError

    client = _bare_client()

    def post(*_a, **_k):
        raise httpx.ConnectError("refused")

    client._client.post = post
    with pytest.raises(WarcraftLogsError, match="ConnectError"):
        client.query("query Q { x }", {}, cache=False)


def test_a_429_is_its_own_class_and_still_a_warcraftlogs_error():
    from wowdps.warcraftlogs import RateLimited, WarcraftLogsError

    client = _bare_client()
    client._client.post = lambda *_a, **_k: type(
        "R", (), {"status_code": 429, "text": "slow down", "headers": {}}
    )()
    with pytest.raises(RateLimited) as caught:
        client.query("query Q { x }", {}, cache=False)
    assert isinstance(caught.value, WarcraftLogsError)


def test_the_timeout_is_a_constructor_parameter_with_the_old_default():
    from wowdps.warcraftlogs import DEFAULT_TIMEOUT_SECONDS, Credentials, WarcraftLogsClient

    assert DEFAULT_TIMEOUT_SECONDS == 30.0
    assert WarcraftLogsClient(Credentials("id", "secret"))._timeout == 30.0
    assert WarcraftLogsClient(Credentials("id", "secret"), timeout=90.0)._timeout == 90.0


def test_rate_limit_returns_points_reset_in(tmp_path):
    client = _client(tmp_path)
    client._client.post = _CountingTransport()
    reading = client.rate_limit()
    assert reading["pointsResetIn"] == 900


# --------------------------------------------------------------------------------
# `wowdps verify` measures what it spends, and refuses to publish what it could not
# --------------------------------------------------------------------------------


def test_every_query_document_asks_for_a_budget_reading():
    """The ratchet, and it was red for two of thirteen documents until 2026-09-12.

    `PointLedger`'s own docstring says *"every query in this module asks for
    rateLimitData alongside its real payload"*. `RANKINGS_QUERY` and `ZONE_QUERY`
    did not -- and `RANKINGS_QUERY` is the one `wowdps verify` sends 208 times a
    week, so the only scheduled points-spending pass in the project could not read
    its own cost at any point, however carefully it was bracketed.

    A document added without one fails here by name rather than by a number nobody
    can take.
    """
    documents = {
        name: value
        for name, value in vars(warcraftlogs).items()
        if name.endswith("QUERY") and isinstance(value, str)
    }
    assert len(documents) >= 13, "the module lost query documents; re-read this test"
    missing = sorted(name for name, text in documents.items() if "rateLimitData" not in text)
    assert missing == [], f"these documents cannot feed the ledger: {missing}"


#: Query documents that live outside `warcraftlogs` and carry no budget reading.
#:
#: Each entry needs a REASON, and the default answer is to add `rateLimitData` to the
#: document rather than a line here. The set exists because the four below are
#: pre-existing and each would change something a measurement has not yet settled --
#: never as somewhere to put a new document that was easier not to fix.
_DOCUMENTS_WITHOUT_A_READING = {
    # Introspection rather than data. `wowdps wcl-schema` brackets its own pass with
    # two standalone `rate_limit()` readings and reports what the counter did, so the
    # document has nothing to contribute that the brackets do not already have.
    ("wclschema", "TYPE_QUERY"),
    # The two progress RANKING documents, and the reason is #170 Schritt 1c rather
    # than neglect. Adding the field to them changes two things nobody has measured:
    # the cache key of every response the chart producer has stored (`--cache` would
    # go cold once), and the point cost, if Warcraft Logs really does charge per
    # resolved field -- which is one of #170's own open measurements. Both are the
    # hot path of a cron job (`progresssweep`), so the bet is not one to take in
    # passing.
    #
    # What it costs meanwhile is measured and is the argument for eventually doing
    # it: because these documents carry no reading, both producers have to poll the
    # counter separately -- 180 of 432 queries (42%) on the chart producer before it
    # was reduced to one poll per boss, and one `rate_limit()` per guild in the sweep
    # to this day.
    #
    # `progresshours.ENCOUNTER_ZONE_QUERY` was the third and is GONE (2026-09-12):
    # it was a second copy of `warcraftlogs.ENCOUNTER_ZONE_QUERY` selecting the same
    # three fields, so unifying it onto the richer document cost no measurement --
    # `wowdps progress-hours` restores no `actions/cache`, so there was no stored
    # response to go cold, and that document was already being sent by two other
    # commands.
    ("progresshours", "PROGRESS_RANKINGS_QUERY"),
    ("progresshours", "GUILD_PULLS_QUERY"),
}


def test_no_module_outside_warcraftlogs_grows_an_unmeasured_query_document():
    """The same ratchet, scoped the way it should have been on 2026-09-12.

    The test above scans `vars(warcraftlogs)` and so cannot see a query document in
    any other module -- and there are four, three of them sent by `cli` and
    `progresssweep`. A guard that is present and answers over the wrong population is
    this repository's signature defect, and building one and then scoping it to one
    file is that defect committed by the person who had just written about it.

    Counted, so the shape is on the record rather than in a commit message: 17
    documents across three modules, 13 of them carrying a reading.
    """
    import importlib
    import pkgutil

    import wowdps

    native = {id(v) for v in vars(warcraftlogs).values() if isinstance(v, str)}
    found: dict[tuple[str, str], str] = {}
    for info in pkgutil.iter_modules(wowdps.__path__):
        module = importlib.import_module(f"wowdps.{info.name}")
        for name, value in vars(module).items():
            if not name.endswith("QUERY") or not isinstance(value, str):
                continue
            if "query" not in value and "mutation" not in value:
                continue
            # A document imported from `warcraftlogs` is that module's to answer for;
            # identity rather than equality, because two modules genuinely holding the
            # same text is exactly the duplication #170 Schritt 1c is about.
            if info.name != "warcraftlogs" and id(value) in native:
                continue
            found[(info.name, name)] = value

    # 16 since 2026-09-12, down from 17: `progresshours.ENCOUNTER_ZONE_QUERY` was a
    # second copy of `warcraftlogs.ENCOUNTER_ZONE_QUERY` and is gone. A floor rather
    # than an equality, because a NEW document must go through the reading check
    # below rather than through this line -- and a document that DISAPPEARS should
    # make somebody read the test, which is exactly what happened here.
    assert len(found) >= 16, f"the package lost query documents; re-read this test: {len(found)}"

    missing = {key for key, text in found.items() if "rateLimitData" not in text}
    unexpected = sorted(missing - _DOCUMENTS_WITHOUT_A_READING)
    assert unexpected == [], f"these documents cannot feed the ledger: {unexpected}"

    # And the other direction, so the set cannot outlive its own evidence: a document
    # that grows a reading has to leave the list, or the list stops being a statement
    # about today.
    stale = sorted(_DOCUMENTS_WITHOUT_A_READING - missing)
    assert stale == [], f"these now carry a reading and should leave the set: {stale}"


# --------------------------------------------------------------------------------
# The key space: one entry per thing (#170 Schritt 2)
# --------------------------------------------------------------------------------


class _StoreTransport:
    """Answers every document with one payload and counts what was actually sent."""

    def __init__(self, payload: dict) -> None:
        self.posts = 0
        self.payload = payload

    def __call__(self, *_a, **_k):
        self.posts += 1
        body = {"data": dict(self.payload)}
        return type("R", (), {"status_code": 200, "json": lambda s: body, "headers": {}})()


def _stubbed_client(tmp_path, transport):
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    client = WarcraftLogsClient(Credentials("id", "secret"), cache_dir=tmp_path / "cache")
    client._token = "token"
    client._client.post = transport
    return client


def test_a_name_request_is_answered_out_of_the_zone_fetch(tmp_path):
    """The immediate win of keying on the thing, measured in requests.

    `encounter_zone` and `encounter_name` are two DOCUMENTS about one encounter, and
    the zone one selects the name as well. Under `sha256(document + variables)` they
    were two cache entries and two requests; under `encounter/<id>` plus a variant
    the second is free.

    `harvest.choose_encounter_id` asks for the name and `fightprobe` for the zone, so
    this is a pair a real pass sends.
    """
    transport = _StoreTransport(
        {"worldData": {"encounter": {"id": 3421, "name": "The Twin Fangs", "zone": {"id": 53}}}}
    )
    client = _stubbed_client(tmp_path, transport)

    assert client.encounter_zone(3421) == {"id": 53}
    assert transport.posts == 1
    assert client.encounter_name(3421) == "The Twin Fangs"
    assert transport.posts == 1, "the name was already paid for by the zone fetch"


def test_the_whole_block_and_its_zone_are_one_fetch(tmp_path):
    """#170 Schritt 3: `encounter_zone` is `encounter` with one field taken off it.

    The pair matters because a twin resolution needs the NAME to verify the
    substitution and the ZONE to walk the reports afterwards, and those are two
    fields of one payload. `cmd_progress_hours` asked for them through two documents
    until 2026-09-12 -- the second of which carried no budget reading at all.
    """
    transport = _StoreTransport(
        {"worldData": {"encounter": {"id": 3421, "name": "The Twin Fangs", "zone": {"id": 53}}}}
    )
    client = _stubbed_client(tmp_path, transport)

    block = client.encounter(3421)
    assert block == {"id": 3421, "name": "The Twin Fangs", "zone": {"id": 53}}
    assert transport.posts == 1
    assert client.encounter_zone(3421) == {"id": 53}
    assert client.encounter_name(3421) == "The Twin Fangs"
    assert transport.posts == 1, "both were already paid for by the block fetch"


def test_an_encounter_the_schema_does_not_know_is_an_empty_block(tmp_path):
    """`{}` rather than a raised error, because that is the answer
    `harvest.choose_encounter_id` refuses a substitution on -- an id whose twin is not
    an encounter Warcraft Logs knows. A raise there would take the pass down over a
    boss that simply has no twin."""
    transport = _StoreTransport({"worldData": {"encounter": None}})
    client = _stubbed_client(tmp_path, transport)

    assert client.encounter(444) == {}
    assert client.encounter_zone(444) == {}


def test_the_other_direction_still_pays(tmp_path):
    """The control, without which the test above passes against a broken store.

    A name-only payload carries no zone. If the lean entry answered the rich
    request, "the API sent no zone" and "this document never asked for one" would be
    the same answer -- and `encounter_zone` would start returning `{}` for every
    encounter, silently.
    """
    transport = _StoreTransport(
        {"worldData": {"encounter": {"id": 3421, "name": "The Twin Fangs"}}}
    )
    client = _stubbed_client(tmp_path, transport)

    assert client.encounter_name(3421) == "The Twin Fangs"
    assert transport.posts == 1
    assert client.encounter_zone(3421) == {}
    assert transport.posts == 2, "a zone request may not be served from a name-only entry"


def test_a_subset_of_one_pull_s_actors_is_answered_out_of_the_superset(tmp_path):
    """The actor ids are a variant, not part of the key.

    They are baked into the document text (`talent_codes_query`), so under the old
    key every distinct actor set was its own entry -- including one that is a strict
    subset of an entry already on disk.
    """
    transport = _StoreTransport(
        {"reportData": {"report": {"fights": [{"a1": "AAA", "a2": "BBB", "a3": "CCC"}]}}}
    )
    client = _stubbed_client(tmp_path, transport)

    assert client.talent_import_codes("abc", 1, [1, 2, 3]) == {1: "AAA", 2: "BBB", 3: "CCC"}
    assert transport.posts == 1
    assert client.talent_import_codes("abc", 1, [1, 3]) == {1: "AAA", 2: "BBB", 3: "CCC"}
    assert transport.posts == 1
    # And a set the entry does NOT cover pays, which is what stops the rule above
    # from being "any talent request hits any talent entry".
    client.talent_import_codes("abc", 1, [1, 4])
    assert transport.posts == 2


def test_a_client_without_a_cache_directory_keeps_working(tmp_path):
    """The store is optional in exactly the way the cache was: absent, not empty."""
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    transport = _StoreTransport({"worldData": {"zones": [{"id": 53}]}})
    client = WarcraftLogsClient(Credentials("id", "secret"))
    client._token = "token"
    client._client.post = transport

    assert client._store is None
    assert client.zones() == [{"id": 53}]
    assert client.zones() == [{"id": 53}]
    assert transport.posts == 2


def test_a_store_entry_does_not_also_write_the_legacy_cache_file(tmp_path):
    """One answer to one question on disk, rather than two under two keys.

    `_fetch` passes `cache=False` to the inner `query` for that reason, and without
    it every fetch would be stored twice -- which would read as the store working
    while the bytes said otherwise.
    """
    transport = _StoreTransport({"reportData": {"report": {"startTime": 1.0, "fights": []}}})
    client = _stubbed_client(tmp_path, transport)
    client.report_kills("abc")

    cache = tmp_path / "cache"
    assert (cache / "report" / "abc" / "kills.json").is_file()
    assert not list(cache.glob("*.json")), "a flat document-hash file was written too"


class _VerifyTransport:
    """A rankings service whose hourly counter moves, and which can start 429-ing."""

    def __init__(self, fail_after: int | None = None, rows: int = 12) -> None:
        self.posts = 0
        self.spent = 100.0
        self.fail_after = fail_after
        self.rows = rows

    def __call__(self, *_a, **_k):
        self.posts += 1
        if self.fail_after is not None and self.posts > self.fail_after:
            return type("R", (), {"status_code": 429, "json": lambda s: {}, "headers": {}})()
        self.spent += 0.5
        payload = {
            "data": {
                "rateLimitData": {
                    "limitPerHour": 3600,
                    "pointsSpentThisHour": self.spent,
                    "pointsResetIn": 900,
                },
                "worldData": {
                    "encounter": {
                        "id": 1,
                        "name": "Some Boss",
                        "characterRankings": {
                            "rankings": [{"amount": 100.0 + i} for i in range(self.rows)]
                        },
                    }
                },
            }
        }
        return type("R", (), {"status_code": 200, "json": lambda s: payload, "headers": {}})()


def _verify_tier(root, specs: int = 3):
    (root / "MID9").mkdir(parents=True, exist_ok=True)
    (root / "tiers.json").write_text(json.dumps({"current": "MID9", "tiers": []}))
    (root / "MID9" / "index.json").write_text(
        json.dumps(
            {
                "specs": [
                    {
                        "id": f"class{i}_spec",
                        "displayName": f"Spec {i}",
                        "class": f"Class{i}",
                        "spec": "Spec",
                        "scenarios": {"patchwerk": {"dps": {"1": 100000.0}}},
                    }
                    for i in range(specs)
                ]
            }
        )
    )


def _run_verify(tmp_path, monkeypatch, transport, **overrides):
    """Drive the real command with a stubbed transport, the way a run reaches it."""
    import argparse

    from wowdps.warcraftlogs import WarcraftLogsClient

    root = tmp_path / "data"
    _verify_tier(root)
    monkeypatch.setenv("WCL_CLIENT_ID", "id")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "secret")

    made: dict[str, WarcraftLogsClient] = {}
    real_init = WarcraftLogsClient.__init__

    def patched(self, credentials, *a, **k):
        real_init(self, credentials, *a, **k)
        self._token = "token"
        self._client.post = transport
        made["client"] = self

    monkeypatch.setattr(WarcraftLogsClient, "__init__", patched)

    fields = {
        "data": str(root),
        "tier": "MID9",
        "encounter": [1, 2],
        "difficulty": 5,
        "metric": "dps",
        "cache": None,
        "point_ceiling": 0.8,
    }
    fields.update(overrides)
    args = argparse.Namespace(**fields)
    code = warcraftlogs.cmd_verify(args)
    out = root / "MID9" / "logs-verification.json"
    document = json.loads(out.read_text()) if out.is_file() else None
    return code, document, made["client"]


def test_a_verify_pass_publishes_what_it_cost(tmp_path, monkeypatch):
    """Measured on 2026-09-12, before this: a whole pass ended at `firstReading
    None`. Not merely un-bracketed -- the ranking document carried no reading at
    all, so no ceiling could be checked and no cost stated."""
    transport = _VerifyTransport()
    code, document, client = _run_verify(tmp_path, monkeypatch, transport)

    assert code == 0
    cost = document["cost"]
    # The bracket: a reading taken before any ranking was fetched and one after.
    assert cost["firstReading"] is not None and cost["lastReading"] is not None
    assert cost["pointsSpentThisRun"] == pytest.approx(3.5)
    assert cost["limitPerHour"] == 3600
    assert client.ledger.spend_state == warcraftlogs.SPEND_MEASURED


def test_a_rate_limit_halfway_writes_nothing_instead_of_publishing_a_thin_tier(
    tmp_path, monkeypatch
):
    """The defect this found, measured against the real command before the fix.

    `RateLimited` subclasses `WarcraftLogsError`, so the per-spec clause caught it,
    set the summary to None, and every remaining row was counted as
    `withheldForSmallSample` -- exit 0, half the comparisons, and the rate limit
    published as a statement about how many parses Warcraft Logs holds. The workflow
    then commits that over a good file.
    """
    transport = _VerifyTransport(fail_after=3)
    code, document, _ = _run_verify(tmp_path, monkeypatch, transport)

    assert code == 2, "a budget stop is the ceiling's exit status"
    assert document is None, "a partial comparison must not replace the published one"


def test_a_failed_query_is_not_counted_as_a_thin_ranking(tmp_path, monkeypatch):
    """Two different findings, and only one is a fact about the game.

    `withheldForSmallSample` is read as "this many spec/boss pairs have too few
    ranked parses". A query that failed says nothing about parses, and the published
    MID2 file states 358 withheld with no way to tell whether any were failures.
    """

    class _OneSpecFails(_VerifyTransport):
        def __call__(self, *a, **k):
            # The bracket reading is post 1; fail the first ranking after it.
            if self.posts == 1:
                self.posts += 1
                self.spent += 0.5
                return type(
                    "R",
                    (),
                    {"status_code": 500, "text": "boom", "json": lambda s: {}, "headers": {}},
                )()
            return super().__call__(*a, **k)

    code, document, _ = _run_verify(tmp_path, monkeypatch, _OneSpecFails())

    assert code == 0
    assert document["withheldForQueryError"] == 1
    assert document["withheldForSmallSample"] == 0


def test_a_thin_ranking_is_still_counted_as_thin(tmp_path, monkeypatch):
    """The control. Separating the two must not empty the count that already
    existed -- that would trade one wrong number for another."""
    code, document, _ = _run_verify(tmp_path, monkeypatch, _VerifyTransport(rows=MIN_SAMPLE - 1))

    assert code == 0
    assert document["comparisons"] == []
    assert document["withheldForSmallSample"] == 6
    assert document["withheldForQueryError"] == 0


def test_verify_writes_no_cache_by_default(tmp_path, monkeypatch):
    """A ranking's cache key does not vary with time, so a cache restored between
    weekly runs would serve last week's medians under this run's date. The flag
    exists for iterating offline; the default must not quietly create one."""
    transport = _VerifyTransport()
    code, _, client = _run_verify(tmp_path, monkeypatch, transport)

    assert code == 0
    assert client._cache_dir is None
    # The control: asked for one, the client caches -- so the default is a decision
    # rather than the flag being inert.
    code, _, cached = _run_verify(
        tmp_path, monkeypatch, _VerifyTransport(), cache=str(tmp_path / "wcl")
    )
    assert code == 0
    assert cached._cache_dir == tmp_path / "wcl"
    # `rglob`, not `glob`: since #170 Schritt 2 a ranking is stored under the thing
    # it is about (`encounter/<id>/rankings/<metric>/d<diff>/p<n>.json`) rather than
    # as a flat document hash. The claim this line makes is unchanged -- asked for a
    # cache, the client writes one -- only where it looks.
    assert list((tmp_path / "wcl").rglob("*.json"))


def test_a_rankings_cache_key_does_not_vary_with_time(tmp_path):
    """The measurement the workflow's missing actions/cache rests on."""
    from wowdps.warcraftlogs import Credentials, WarcraftLogsClient

    client = WarcraftLogsClient(Credentials("id", "secret"), cache_dir=tmp_path)
    variables = {
        "encounterId": 1,
        "difficulty": 5,
        "metric": "dps",
        "className": "Mage",
        "specName": "Arcane",
        "page": 1,
    }
    # Nothing in a ranking query's variables is a clock, so two runs a week apart
    # ask for the same file. A report's events are immutable and may be cached; a
    # ranking is the thing the weekly pass exists to re-read.
    assert client._cache_path(warcraftlogs.RANKINGS_QUERY, variables) == client._cache_path(
        warcraftlogs.RANKINGS_QUERY, dict(variables)
    )
