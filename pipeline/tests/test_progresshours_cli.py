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


def fight(start, end, kill=False, encounter=None):
    # `encounter` is explicit for the twin tests: after a PTR id is read as its live
    # twin the walk asks for the TWIN's fights, and `ordered_attempts` re-checks
    # `encounterID` -- so a fight still carrying the filed id is correctly dropped and
    # the boss comes back `no-fights`. That is the code working, not the test.
    return {
        "startTime": start,
        "endTime": end,
        "kill": kill,
        "encounterID": ENCOUNTER if encounter is None else encounter,
        "difficulty": MYTHIC,
    }


#: Sentinel: "work the kill time out from the pages" rather than "state none".
_DERIVE = object()


def _earliest_kill(pages):
    """The absolute time of the first kill in the canned pages, or None.

    Absolute -- `report["startTime"] + fight["startTime"]` -- because that is the
    clock a ranking row's `killTime` is on. A stub that returned the report-relative
    number would model a world where the two disagree by hours on every fixture.
    """
    times = []
    for page in pages:
        for report in page.get("data") or []:
            base = report.get("startTime")
            if not isinstance(base, (int, float)):
                continue
            for f in report.get("fights") or []:
                if f.get("kill") is True:
                    times.append(base + f["startTime"])
    return min(times) if times else None


class StubClient:
    """Records every query it is asked for, and answers from canned pages."""

    def __init__(self, pages, zone=ZONE, guilds=(1,), kill_time=_DERIVE, from_log=1):
        self.pages = pages
        self.zone = zone
        self.guilds = guilds
        self.sent = []
        self.rate_limit_calls = 0
        self.ledger = argparse.Namespace(entries=[])
        # A ranking row states the guild's first kill and whether a log backs it, and
        # `cmd_progress_hours` screens on both. The DEFAULT models the ordinary world
        # -- the ranking and the log agree -- by deriving the kill time from the canned
        # pages, so a fixture about paging or deduplication does not also have to be a
        # fixture about the screen. Override either to model a disagreement.
        self.from_log = from_log
        self.kill_time = _earliest_kill(pages) if kill_time is _DERIVE else kill_time

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
            if variables.get("p", 1) > 1:
                # One page holds every stubbed guild, so page 2 is empty. Modelled
                # rather than ignored: the paging loop must stop on a short page.
                return {"worldData": {"encounter": {"fightRankings": {"rankings": []}}}}
            rankings = []
            for g in self.guilds:
                row = {"guild": {"id": g}}
                if self.from_log is not None:
                    row["fromlog"] = self.from_log
                if self.kill_time is not None:
                    row["killTime"] = self.kill_time
                rankings.append(row)
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
        rankings_pages=3,
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
    # The kill sits at the END of its night's offsets, which is the second thing this
    # fixture has to get right and used not to. It read `fight(0, HOUR, kill=True)`
    # after a wipe at offset HOUR -- so on the absolute clock that wipe starts an hour
    # AFTER the kill, and counting it as progression toward that kill is exactly the
    # error the ordering exists to prevent. Offsets are a real clock, not an array.
    farm = [{"startTime": 9_000_000, "fights": [fight(0, HOUR, kill=True)]}]
    progression = [
        {
            "startTime": 1_000_000,
            "fights": [
                fight(0, HOUR),
                fight(HOUR, 2 * HOUR),
                fight(2 * HOUR, 3 * HOUR, kill=True),
            ],
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


# ── The completeness screens, end to end ─────────────────────────────────────


def test_a_guild_whose_first_kill_has_no_log_is_refused_before_a_report_is_fetched(
    monkeypatch, tmp_path
):
    """`fromlog == 0` means Warcraft Logs holds no log behind that kill.

    A report walk therefore CANNOT find it, and whatever kill the walk does find is a
    later one -- so the guild would publish a farm night as its progression. Refusing
    costs nothing and saves the whole walk, which is why the query count is asserted
    rather than just the outcome.
    """
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages, from_log=0)
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"unlogged-kill": 1}
    assert boss["guilds"][0]["outcome"] == "unlogged-kill"
    assert boss["killsNotFromLog"] == 1 and boss["killsFromLog"] == 0
    assert boss["medianHours"] is None
    assert not [1 for label, _ in client.sent if label and label.startswith("pulls:")], (
        "the report walk ran for a guild whose kill cannot be in it"
    )


def test_a_ranking_row_stating_neither_screen_field_is_refused_not_waved_through(
    monkeypatch, tmp_path
):
    """Fail closed. A renamed field must not switch both screens off silently."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages, from_log=None, kill_time=None)
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"ranking-row-unscreened": 1}
    assert boss["medianHours"] is None


def test_a_log_that_only_saw_a_later_kill_is_refused_rather_than_measured(monkeypatch, tmp_path):
    """The ranking says the guild killed it weeks before anything in the log.

    Before the screen this published one wipe and one kill as a whole progression.
    """
    week = 7 * 86_400_000
    pages = [
        listing(
            [{"startTime": week, "fights": [fight(0, HOUR), fight(HOUR, 2 * HOUR, kill=True)]}],
            False,
        )
    ]
    client = StubClient(pages, kill_time=0)
    boss = run(monkeypatch, tmp_path, client)

    assert boss["refused"] == {"kill-too-late": 1}
    assert boss["medianHours"] is None


def test_the_document_states_the_screens_and_that_the_metric_is_a_floor(monkeypatch, tmp_path):
    """A reader who never reads `note` still has to be able to tell this is a bound."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages, guilds=(1, 2), from_log=0)
    out = tmp_path / "progress-hours.json"
    run(monkeypatch, tmp_path, client, guilds=2, out=str(out))
    document = json.loads(out.read_text(encoding="utf-8"))

    assert document["metricIsFloor"] is True
    assert document["screens"]["unloggedKill"] == 2
    assert document["screens"]["killsNotFromLog"] == 2
    assert "LOWER BOUND" in document["note"]


def test_a_measured_guild_publishes_what_the_observation_could_possibly_cover(
    monkeypatch, tmp_path
):
    """The residual limitation, disclosed rather than repaired.

    Nothing can see a night nobody uploaded, so the honest move is to publish how many
    nights WERE seen and over how long. Few nights across a wide span is what a partial
    view looks like from outside.
    """
    day = 86_400_000
    pages = [
        listing(
            [
                {"code": "a", "startTime": 0, "fights": [fight(0, HOUR)]},
                {"code": "b", "startTime": 2 * day, "fights": [fight(0, HOUR, kill=True)]},
            ],
            False,
        )
    ]
    boss = run(monkeypatch, tmp_path, StubClient(pages))

    row = boss["guilds"][0]
    assert row["outcome"] == "measured"
    assert row["nightsObserved"] == 2
    assert row["spanDays"] == 2.0
    assert boss["medianNightsObserved"] == 2.0
    assert boss["medianSpanDays"] == 2.0


def test_guilds_seen_counts_what_was_actually_reached(monkeypatch, tmp_path):
    """Set incrementally, never to the sample size up front.

    A ceiling stop mid-boss would otherwise publish "50 guilds seen" above three rows
    -- a fraction of a run presented as the whole.
    """
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = StubClient(pages, guilds=(1, 2, 3))
    boss = run(monkeypatch, tmp_path, client, guilds=10)

    assert boss["guildsSeen"] == 3
    assert boss["sampleShortOfRequest"] is True, "asked for 10, the ranking held 3"


# ── PTR ids: a tier seeded from a PTR zone addresses the wrong encounter ──────


class TwinClient(StubClient):
    """A stub where the FILED id has no ranking rows and a live twin does.

    Modelled on the measurement of 2026-08-27: all eight MID2 bosses returned 0 of 0
    guilds at Mythic AND at Heroic, because the tier carries `5xxxx` PTR ids.
    """

    def __init__(self, pages, filed, twin, twin_name, filed_name=None, **kw):
        super().__init__(pages, **kw)
        self.filed = filed
        self.twin = twin
        self.twin_name = twin_name
        self.filed_name = filed_name if filed_name is not None else twin_name

    def query(self, document, variables, label=None):
        self.sent.append((label, dict(variables)))
        if document is progresshours.ENCOUNTER_ZONE_QUERY:
            which = variables["e"]
            name = self.filed_name if which == self.filed else self.twin_name
            if name is None:
                return {"worldData": {"encounter": None}}
            zone = {"id": self.zone} if self.zone else None
            return {"worldData": {"encounter": {"id": which, "name": name, "zone": zone}}}
        if document is progresshours.PROGRESS_RANKINGS_QUERY:
            if variables["e"] != self.twin or variables.get("p", 1) > 1:
                return {"worldData": {"encounter": {"fightRankings": {"rankings": []}}}}
            rows = [
                {"guild": {"id": g}, "fromlog": self.from_log, "killTime": self.kill_time}
                for g in self.guilds
            ]
            return {"worldData": {"encounter": {"fightRankings": {"rankings": rows}}}}
        return {"reportData": {"reports": self.pages[variables["page"] - 1]}}


def test_a_ptr_id_with_no_ranking_rows_is_read_as_its_live_twin(monkeypatch, tmp_path):
    pages = [
        listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True, encounter=3445)]}], False)
    ]
    client = TwinClient(pages, filed=53445, twin=3445, twin_name="Entombed Sentinels")
    boss = run(monkeypatch, tmp_path, client, tier="MID2", encounter=53445)

    assert boss["medianHours"] == 1.0, "the twin's ranking was never used"
    assert "read as 3445" in (boss["readAs"] or "")
    assert any(label == "progress:3445:p1" for label, _ in client.sent)


def test_a_twin_that_is_a_different_boss_is_refused_not_substituted(monkeypatch, tmp_path):
    """Filing a season's progression under the wrong fight is undetectable downstream."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = TwinClient(
        pages, filed=53445, twin=3445, twin_name="Some Other Boss", filed_name="Entombed Sentinels"
    )
    boss = run(monkeypatch, tmp_path, client, tier="MID2", encounter=53445)

    assert boss["medianHours"] is None
    assert "different boss" in (boss["readAs"] or "")
    assert not any(label == "progress:3445:p1" for label, _ in client.sent)


def test_an_id_that_is_not_a_ptr_id_has_no_twin_and_says_so(monkeypatch, tmp_path):
    """An ordinary live id with an empty ranking is a fact about the season."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    client = TwinClient(pages, filed=3176, twin=176, twin_name="Never Asked")
    boss = run(monkeypatch, tmp_path, client, encounter=3176)

    assert boss["medianHours"] is None
    assert "not a PTR id" in (boss["readAs"] or "")


def test_a_filed_id_that_answers_costs_no_twin_lookup(monkeypatch, tmp_path):
    """The ordinary case must not pay for the PTR branch."""
    pages = [listing([{"startTime": 0, "fights": [fight(0, HOUR, kill=True)]}], False)]
    boss = run(monkeypatch, tmp_path, StubClient(pages))

    assert boss["medianHours"] == 1.0
    assert boss["readAs"] is None
