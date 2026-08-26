"""`wowdps progress-hours` end to end against a stubbed Warcraft Logs client.

Every line between picking guilds and writing the document is executed -- the zone
lookup, the page walk, the refusals and the logging -- because the three defects the
first successful live run shipped all live in the COMMAND, not in the pure module,
and the pure module's own tests were green through all of them.
"""

import argparse
import json

import pytest

from wowdps import cli, progresshours, warcraftlogs

ENCOUNTER = 3176
ZONE = 46
MYTHIC = 5
HOUR = progresshours.MS_PER_HOUR


def fight(start, end, kill=False):
    return {
        "startTime": start,
        "endTime": end,
        "kill": kill,
        "encounterID": ENCOUNTER,
        "difficulty": MYTHIC,
    }


class StubClient:
    """Records every query it is asked for, and answers from canned pages."""

    def __init__(self, pages, zone=ZONE, guilds=(1,)):
        self.pages = pages
        self.zone = zone
        self.guilds = guilds
        self.sent = []
        self.rate_limit_calls = 0
        self.ledger = argparse.Namespace(entries=[])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def rate_limit(self):
        self.rate_limit_calls += 1
        return {"limitPerHour": 18000.0, "pointsSpentThisHour": 0.0}

    def query(self, document, variables, label=None):
        self.sent.append((label, dict(variables)))
        if document is progresshours.ENCOUNTER_ZONE_QUERY:
            zone = {"id": self.zone} if self.zone else None
            return {"worldData": {"encounter": {"id": ENCOUNTER, "zone": zone}}}
        if document is progresshours.PROGRESS_RANKINGS_QUERY:
            rankings = [{"guild": {"id": g}} for g in self.guilds]
            return {"worldData": {"encounter": {"fightRankings": {"rankings": rankings}}}}
        page = variables["page"]
        return {"reportData": {"reports": self.pages[page - 1]}}


def listing(reports, more):
    return {"has_more_pages": more, "data": reports}


def run(monkeypatch, tmp_path, client, **overrides):
    # `cmd_progress_hours` imports these inside the function, so the module is the
    # patch target rather than the cli namespace.
    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", staticmethod(lambda: object()))
    monkeypatch.setattr(warcraftlogs, "WarcraftLogsClient", lambda _c: client)
    out = tmp_path / "progress-hours.json"
    args = argparse.Namespace(
        tier="MID1",
        encounter=ENCOUNTER,
        zone=0,
        difficulty=MYTHIC,
        guilds=4,
        max_pages=3,
        point_ceiling=0.5,
        out=str(out),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    assert cli.cmd_progress_hours(args) == 0
    return json.loads(out.read_text(encoding="utf-8"))["bosses"][0]


def test_the_zone_is_looked_up_and_every_pulls_query_carries_it(monkeypatch, tmp_path):
    """The live run sent `zoneID: 0` to all 288 of its queries and nothing said so."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages)
    boss = run(monkeypatch, tmp_path, client)

    assert boss["zoneId"] == ZONE
    pulls = [v for label, v in client.sent if label and label.startswith("pulls:")]
    assert pulls, "no report query was sent"
    assert {v["z"] for v in pulls} == {ZONE}
    assert 0 not in {v["z"] for v in pulls}


def test_an_unresolvable_zone_refuses_the_boss_instead_of_searching_zone_zero(
    monkeypatch, tmp_path
):
    """Refusing costs one boss. Searching zone 0 costs every number, silently."""
    client = StubClient([listing([], False)], zone=None)
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"no-zone": 1}
    assert boss["medianHours"] is None
    assert boss["zoneId"] is None
    assert not [label for label, _ in client.sent if label and label.startswith("pulls:")]


def test_the_walk_does_not_stop_at_the_first_kill_it_finds(monkeypatch, tmp_path):
    """Reports arrive newest-first, so the first kill *found* is the LAST kill fought.

    Page 1 here is a farm night -- one pull, a kill. Page 2 is the progression night
    that preceded it. The old loop broke on page 1 and published 1 hour; the answer
    is 4 (three wipes and the kill on the older night).
    """
    farm = [{"startTime": 9_000_000, "fights": [fight(0, HOUR, kill=True)]}]
    progression = [
        {
            "startTime": 1_000_000,
            "fights": [fight(0, HOUR), fight(HOUR, 2 * HOUR), fight(0, HOUR, kill=True)],
        },
        {"startTime": 500_000, "fights": [fight(0, HOUR)]},
    ]
    client = StubClient([listing(farm, True), listing(progression, False)])
    boss = run(monkeypatch, tmp_path, client)

    assert boss["medianHours"] == 4.0
    assert boss["medianAttempts"] == 4.0
    assert len([1 for label, _ in client.sent if label and label.startswith("pulls:")]) == 2


def test_a_window_that_ran_out_of_pages_is_refused_not_answered(monkeypatch, tmp_path):
    """`has_more_pages` still true at `--max-pages` means older reports exist, so the
    guild's FIRST kill may be older than anything read. Summing what was fetched is a
    floor wearing a measurement's clothes -- the same rule `no-kill` already follows.
    """
    page = listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], True)
    client = StubClient([page, page, page])
    boss = run(monkeypatch, tmp_path, client, max_pages=3)

    assert boss["refused"] == {"truncated": 1}
    assert boss["medianHours"] is None
    assert boss["sample"] == 0


def test_a_farm_kill_is_legible_from_the_document_alone(monkeypatch, tmp_path):
    """One pull, one kill: hours look small and `medianAttempts` says why.

    Without this field the live run's 0.09-0.30 h medians were indistinguishable from
    a genuinely fast progression, and a chart drawn from them would have been wrong
    with nothing on it to argue against.
    """
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR / 6, kill=True)]}], False)]
    boss = run(monkeypatch, tmp_path, StubClient(pages))

    assert boss["medianAttempts"] == 1.0
    assert boss["medianHours"] == pytest.approx(0.167, abs=0.001)


def test_budget_polling_does_not_scale_with_the_guild_count(monkeypatch, tmp_path):
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages, guilds=tuple(range(1, 13)))
    run(monkeypatch, tmp_path, client, guilds=12)

    # 2 bracketing readings + one per boss (one boss here, --encounter is set).
    assert client.rate_limit_calls <= 3, (
        f"{client.rate_limit_calls} budget polls for 12 guilds -- "
        "the poll is back inside the guild loop"
    )


def test_one_report_is_never_counted_twice(monkeypatch, tmp_path):
    """Paging is not a snapshot. A listing that shifts between page 1 and page 2 hands
    back the same report twice, and its fights would be summed twice -- inflating both
    `attempts` and `hours` with nothing in the document saying so.
    """
    same = {
        "code": "AbC123",
        "startTime": 0,
        "fights": [fight(0, HOUR), fight(HOUR, 2 * HOUR, kill=True)],
    }
    client = StubClient([listing([same], True), listing([same], False)])
    boss = run(monkeypatch, tmp_path, client)

    assert boss["medianAttempts"] == 2.0, "the duplicated report was counted twice"
    assert boss["medianHours"] == 2.0
    assert boss["guilds"][0]["duplicateReports"] == 1


def test_a_report_with_no_code_is_kept_rather_than_deduplicated(monkeypatch, tmp_path):
    """Dropping it would lose real pulls; only a code can prove two rows are one report."""
    a = {"startTime": 0, "fights": [fight(0, HOUR)]}
    b = {"startTime": 1, "fights": [fight(0, HOUR, kill=True)]}
    client = StubClient([listing([a, b], False)])
    boss = run(monkeypatch, tmp_path, client)

    assert boss["medianAttempts"] == 2.0
    assert boss["guilds"][0].get("duplicateReports") is None


def test_an_empty_listing_is_reported_as_no_reports_not_no_fights(monkeypatch, tmp_path):
    """The two are opposite findings and were one bucket."""
    client = StubClient([listing([], False)])
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"no-reports": 1}
    assert boss["guilds"][0]["outcome"] == "no-reports"
    assert boss["guilds"][0]["reportsSeen"] == 0
    assert boss["medianReportsSeen"] == 0.0


def test_a_listing_holding_another_boss_is_no_fights_with_the_count_beside_it(
    monkeypatch, tmp_path
):
    client = StubClient([listing([{"code": "X", "startTime": 0, "fights": []}] * 3, False)])
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"no-fights": 1}
    # One report survives the dedup (all three share the code "X"), and the count is
    # what says the listing was NOT empty.
    assert boss["guilds"][0]["outcome"] == "no-fights"
    assert boss["guilds"][0]["reportsSeen"] >= 1
