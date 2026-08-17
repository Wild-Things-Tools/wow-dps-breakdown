"""Choosing kills by date off the report search rather than off the rankings.

The route exists because `characterRankings` cannot answer "who killed this first":
it is sorted by damage and holds only ranked parses. These tests pin the two
properties that make the replacement trustworthy without a verified pagination
envelope -- it never assumes an order, and it never lets a wipe into a sample of
kills.
"""

from __future__ import annotations

from wowdps import firstkills


def test_the_envelope_is_read_defensively_because_it_could_not_be_introspected():
    """`ReportPagination` is the declared return type and the server refuses to describe it."""
    assert firstkills.reports_from_payload({"data": [{"code": "a"}, {"code": "b"}]}) == [
        {"code": "a"},
        {"code": "b"},
    ]
    # A bare list is accepted too.
    assert firstkills.reports_from_payload([{"code": "a"}]) == [{"code": "a"}]
    # Anything unrecognised yields nothing rather than raising: a nine-boss pass
    # must not die on one odd envelope, and zero reports is visible in the output.
    assert firstkills.reports_from_payload({"nodes": [{"code": "a"}]}) == []
    assert firstkills.reports_from_payload(None) == []
    # A row with no code cannot be fetched, so it is not a report.
    assert firstkills.reports_from_payload({"data": [{"startTime": 1}]}) == []


EPOCH_BASE = 1_700_000_000_000


def test_fight_times_are_relative_to_the_report_and_must_be_rebased():
    """The bug that made the first live run report 100% of kills as "earliest".

    `ReportFight.startTime` counts milliseconds from the report's own start, not
    from the epoch. Used as-is it is a number near zero, which sorts before every
    real date -- so the search returned everything and looked like a spectacular
    finding. 89 of 89 and 102 of 102 was the tell.
    """
    fights = [
        {"id": 1, "encounterID": 42, "kill": True, "startTime": 7_200_000, "endTime": 7_400_000}
    ]

    rows = firstkills.kills_from_report("abc", fights, 42, report_start_ms=EPOCH_BASE)

    assert rows[0].started_at == EPOCH_BASE + 7_200_000
    # The duration is a difference, so it is unaffected by the base.
    assert rows[0].duration_ms == 200_000


def test_a_kill_with_no_report_base_is_dropped_rather_than_sorted_first():
    """A row whose time base is unknown cannot be ranked against rows whose base is.

    Keeping it would put it at the head of a "who killed it first" list every time,
    which is precisely the wrong answer to be confident about.
    """
    fights = [{"id": 1, "encounterID": 42, "kill": True, "startTime": 5_000, "endTime": 6_000}]
    assert firstkills.kills_from_report("abc", fights, 42, report_start_ms=0.0) == []


def test_a_wipe_never_enters_a_sample_of_first_kills():
    """`killType: Kills` is the server's filter; this is the check that it worked.

    A wipe on the first night is exactly the row that would win a "who killed it
    first" sort, so trusting the filter alone is the expensive mistake here.
    """
    fights = [
        {"id": 1, "encounterID": 42, "kill": False, "startTime": 100, "endTime": 200},
        {"id": 2, "encounterID": 42, "kill": True, "startTime": 300, "endTime": 500},
    ]
    rows = firstkills.kills_from_report("abc", fights, 42, EPOCH_BASE)
    assert [row.fight_id for row in rows] == [2]
    assert rows[0].duration_ms == 200


def test_another_encounter_in_the_same_report_is_not_this_boss():
    fights = [
        {"id": 1, "encounterID": 99, "kill": True, "startTime": 100, "endTime": 200},
        {"id": 2, "encounterID": 42, "kill": True, "startTime": 300, "endTime": 400},
    ]
    rows = firstkills.kills_from_report("abc", fights, 42, EPOCH_BASE)
    assert [row.fight_id for row in rows] == [2]


def test_a_fight_with_no_start_time_is_skipped_rather_than_sorted_as_epoch_zero():
    fights = [{"id": 1, "encounterID": 42, "kill": True, "endTime": 200}]
    assert firstkills.kills_from_report("abc", fights, 42, EPOCH_BASE) == []


def test_selection_sorts_locally_so_the_server_order_does_not_matter():
    """Whatever order the API returns reports in, the answer is the same.

    Load-bearing: the pagination envelope could not be introspected, so nothing may
    depend on Warcraft Logs returning reports oldest- or newest-first.
    """
    rows = [
        firstkills.KillRow("late", 1, 5_000),
        firstkills.KillRow("early", 1, 1_000),
        firstkills.KillRow("middle", 1, 3_000),
    ]
    assert [row.report_code for row in firstkills.earliest_kills(rows, 2)] == ["early", "middle"]
    assert [row.report_code for row in firstkills.earliest_kills(list(reversed(rows)), 2)] == [
        "early",
        "middle",
    ]


def test_two_kills_in_one_log_are_one_guilds_evening():
    """Same rule as the ranking selector: thirty kills from three logs is three guilds."""
    rows = [
        firstkills.KillRow("A", 1, 1_000),
        firstkills.KillRow("A", 2, 1_500),
        firstkills.KillRow("B", 1, 2_000),
    ]
    chosen = firstkills.earliest_kills(rows, 5)
    assert [(row.report_code, row.fight_id) for row in chosen] == [("A", 1), ("B", 1)]


def test_the_window_reaches_back_from_the_anchor():
    """The anchor is the earliest *ranked* kill; the point is to look before it."""
    anchor = 100 * firstkills.DAY_MS
    start, end = firstkills.search_window(anchor, lookback_days=10, forward_days=14)
    assert start == 90 * firstkills.DAY_MS
    assert end == 114 * firstkills.DAY_MS


def test_a_window_reaching_before_the_epoch_is_clamped_to_zero():
    """Warcraft Logs reads 0 as 'from the beginning'; a negative timestamp is nonsense."""
    start, _ = firstkills.search_window(2 * firstkills.DAY_MS, lookback_days=30, forward_days=1)
    assert start == 0


def test_the_outcome_says_whether_the_rankings_were_hiding_anything():
    """A search that beats the anchor by nothing is a real answer about the zone."""
    outcome = firstkills.SearchOutcome(reports_seen=40, pages_read=2, kills_found=31, beat_anchor=0)
    assert "none earlier" in outcome.summary(1_000.0)

    outcome.beat_anchor = 6
    assert "6 earlier" in outcome.summary(1_000.0)


def test_no_anchor_searches_the_whole_zone_rather_than_refusing():
    """The PTR case, which is the one this route exists for.

    A PTR zone has no ranked parses at all, so there is nothing to anchor on --
    and refusing there left zone 54's eight Season 2 bosses with no measurements
    while their reports were sitting in the API. Such a zone has only existed for
    weeks, so its whole report list is small and searching from zero is cheap.
    """
    start, end = firstkills.search_window(0.0, 10, 14, now_ms=1_700_000_000_000)
    assert start == 0
    assert end == 1_700_000_000_000


def test_an_unanchored_search_does_not_claim_anything_about_earlier_kills():
    """`beat_anchor` is meaningless with no anchor, and the summary says so."""
    outcome = firstkills.SearchOutcome(reports_seen=40, pages_read=2, kills_found=30)
    assert "whole zone was searched" in outcome.summary(0.0)


def test_kills_are_filtered_to_the_difficulty_the_probe_will_ask_for():
    """The search is unfiltered by difficulty; the filter has to happen here.

    One query per report serves every boss, which is what makes the search
    affordable -- so the query cannot carry a difficulty. Leaving the filter out
    entirely is not harmless: the first run against Season 2's PTR zone found 54
    kills of one boss and sampled none, because every one was at a difficulty other
    than the Mythic the probe then requested.
    """
    fights = [
        {"id": 1, "encounterID": 42, "difficulty": 4, "kill": True, "startTime": 0, "endTime": 1},
        {"id": 2, "encounterID": 42, "difficulty": 5, "kill": True, "startTime": 5, "endTime": 6},
    ]

    mythic = firstkills.kills_from_report("abc", fights, 42, EPOCH_BASE, difficulty=5)
    assert [row.fight_id for row in mythic] == [2]

    # None means any, which is right for a zone nobody has pushed to Mythic yet.
    either = firstkills.kills_from_report("abc", fights, 42, EPOCH_BASE, difficulty=None)
    assert [row.fight_id for row in either] == [1, 2]
