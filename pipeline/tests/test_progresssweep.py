"""`wowdps progress-sweep` against a stubbed Warcraft Logs client. No network.

Every rule in docs/progress-cohort.md that the sweep is responsible for is pinned
here: the cursor's five rules, the wall, the skip matrix, the two schema alarms, the
budget, the file discipline and the validator. The stub answers what the CLIENT
returns (a decoded document), not what the service sends, because the sweep reads
the former.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from wowdps import cli, progresshours, progresssweep
from wowdps.progresssweep import (
    Cadence,
    RankedRow,
    SweepOptions,
    before_cursor,
    run_sweep,
    skip_reason,
    walk_ranking,
)
from wowdps.warcraftlogs import Credentials, RateLimited, WarcraftLogsClient, WarcraftLogsError

ZONE = 44
ENC = 3129
ENC2 = 3130
MYTHIC = 5
HEROIC = 4
#: A kill time on the absolute clock, 2023-11-14.
KILL = 1_700_000_000_000
HOUR_MS = 3_600_000
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def kill_at(i: int) -> int:
    """Guild ``i``'s ranked kill time: a minute apart, ascending, like the API."""
    return KILL + i * 60_000


def row(guild_id, kill_ms=..., fromlog=1, name="Guild", server=...):
    """One `fightRankings(metric: progress)` row. `...` keeps the default; None omits."""
    entry = {"guild": {"id": guild_id}}
    if name is not None:
        entry["guild"]["name"] = f"{name} {guild_id}"
    if server is ...:
        server = {"slug": "tarren-mill", "region": "EU"}
    if server is not None:
        entry["guild"]["server"] = server
    if fromlog is not None:
        entry["fromlog"] = fromlog
    if kill_ms is ...:
        kill_ms = kill_at(guild_id)
    if kill_ms is not None:
        entry["killTime"] = kill_ms
    return entry


def good_pulls(kill_ms, encounter=ENC, difficulty=MYTHIC, code="R1"):
    """One report page: a wipe and, an hour later, the kill at exactly `kill_ms`."""
    base = kill_ms - HOUR_MS
    report = {
        "code": code,
        "startTime": base,
        "fights": [
            {
                "startTime": 0,
                "endTime": 600_000,
                "kill": False,
                "encounterID": encounter,
                "difficulty": difficulty,
            },
            {
                "startTime": HOUR_MS,
                "endTime": HOUR_MS + 300_000,
                "kill": True,
                "encounterID": encounter,
                "difficulty": difficulty,
            },
        ],
    }
    return [{"has_more_pages": False, "data": [report]}]


def zone_payload(zone_id=ZONE, frozen=True, encounters=((ENC, "Plexus Sentinel"),)):
    return {
        "id": zone_id,
        "name": "Manaforge Omega",
        "frozen": frozen,
        "encounters": [{"id": e, "name": n} for e, n in encounters],
    }


class StubClient:
    """Answers from canned rankings and pulls; records every call and its cache flag."""

    def __init__(self, *, zones=None, rankings=None, pulls=None, readings=None, more_pages=False):
        self.zones = zones if zones is not None else {ZONE: zone_payload()}
        self.rankings = rankings or {}
        self.pulls = pulls or {}
        self.readings = list(readings or [])
        self.more_pages = more_pages
        self.queries = []
        self.zone_calls = []
        self.rate_limit_calls = 0
        self.ledger = SimpleNamespace(first_reading=None, last_reading=None, entries=[])

    def rate_limit(self):
        self.rate_limit_calls += 1
        reading = self.readings.pop(0) if self.readings else None
        if isinstance(reading, Exception):
            raise reading
        if reading is None:
            reading = {"limitPerHour": 18000.0, "pointsSpentThisHour": 100.0, "pointsResetIn": 900}
        spent = reading.get("pointsSpentThisHour")
        if self.ledger.first_reading is None:
            self.ledger.first_reading = spent
        self.ledger.last_reading = spent
        self.ledger.entries.append(("rateLimit", spent, False))
        return reading

    def zone(self, zone_id, *, cache=True):
        self.zone_calls.append((zone_id, cache))
        answer = self.zones.get(zone_id)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _kill_time_of(self, guild_id, encounter, difficulty):
        for entry in self.rankings.get((encounter, difficulty), []):
            if (entry.get("guild") or {}).get("id") == guild_id:
                return entry.get("killTime")
        return None

    def query(self, document, variables, label=None, cache=True):
        self.queries.append((label, dict(variables), cache))
        self.ledger.entries.append((label, 0.0, cache))
        if document is progresshours.PROGRESS_RANKINGS_QUERY:
            rows = self.rankings.get((variables["e"], variables["d"]), [])
            if isinstance(rows, Exception):
                raise rows
            page, size = variables["p"], progresshours.RANKING_PAGE_SIZE
            chunk = rows[(page - 1) * size : page * size]
            has_more = self.more_pages or len(rows) > page * size
            return {
                "worldData": {
                    "encounter": {"fightRankings": {"rankings": chunk, "hasMorePages": has_more}}
                }
            }
        if document is progresshours.GUILD_PULLS_QUERY:
            key = (variables["g"], variables["e"], variables["d"])
            pages = self.pulls.get(key)
            if isinstance(pages, Exception):
                raise pages
            if pages is None:
                kill = self._kill_time_of(*key)
                pages = good_pulls(kill or KILL, key[1], key[2])
            index = variables["page"] - 1
            page = pages[index] if index < len(pages) else {"has_more_pages": False, "data": []}
            return {"reportData": {"reports": page}}
        raise AssertionError(f"unexpected document: {document[:40]!r}")


def options(**overrides) -> SweepOptions:
    base = {"zones": (ZONE,), "difficulties": (MYTHIC,), "deadline_minutes": 60}
    base.update(overrides)
    return SweepOptions(**base)


def sweep(client, out, **overrides):
    return run_sweep(
        client,
        options(**overrides),
        out,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        now=lambda: NOW,
        run_id="test",
    )


def rows_of(out: Path, zone=ZONE, difficulty=MYTHIC):
    path = out / f"z{zone}-d{difficulty}.rows.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def refused_of(out: Path, zone=ZONE, difficulty=MYTHIC):
    path = out / f"z{zone}-d{difficulty}.refused.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def state_of(out: Path, zone=ZONE, difficulty=MYTHIC, encounter=ENC):
    path = out / f"z{zone}-d{difficulty}.state.json"
    return json.loads(path.read_text())["encounters"][str(encounter)]


# ── the cursor: five rules, ported 1:1 ───────────────────────────────────────────


def _ranked(guild_id, kill_ms):
    return RankedRow(guild_id, kill_ms, True)


def test_cursor_rule_1_a_strictly_earlier_kill_time_was_already_judged():
    assert before_cursor(_ranked(1, 999.0), (1000.0, 7)) is True


def test_cursor_rule_2_an_equal_kill_time_is_judged_only_for_the_cursor_row_itself():
    assert before_cursor(_ranked(7, 1000.0), (1000.0, 7)) is True
    assert before_cursor(_ranked(8, 1000.0), (1000.0, 7)) is False


def test_cursor_rule_3_a_row_without_a_kill_time_is_never_skipped():
    assert before_cursor(_ranked(1, None), (1000.0, 7)) is False
    assert before_cursor(_ranked(1, None), None) is False


def test_cursor_rule_4_the_cursor_moves_forward_only(tmp_path):
    """A retry walks guilds BEHIND the cursor; the cursor must not follow them back."""
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2), row(3)]})
    sweep(client, tmp_path)
    assert state_of(tmp_path)["cursorKillTimeMs"] == kill_at(3)
    # Guild 1 becomes an `error` refusal behind the cursor, then is retried.
    (tmp_path / "z44-d5.refused.jsonl").write_text(
        progresssweep.dumps_line(
            {"v": 1, "encounterId": ENC, "guildId": 1, "outcome": "error", "run": "x"}
        )
    )
    (tmp_path / "z44-d5.rows.jsonl").write_text(
        "".join(
            line
            for line in (tmp_path / "z44-d5.rows.jsonl").read_text().splitlines(keepends=True)
            if '"guildId":1,' not in line
        )
    )
    sweep(client, tmp_path, retry_errors=True)
    assert [r["guildId"] for r in rows_of(tmp_path)] == [2, 3, 1]
    assert state_of(tmp_path)["cursorKillTimeMs"] == kill_at(3), "moved backwards"


def test_cursor_rule_5_the_stored_millisecond_is_truncated_down(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1, kill_ms=kill_at(1) + 0.9)]})
    sweep(client, tmp_path)
    entry = state_of(tmp_path)
    assert entry["cursorKillTimeMs"] == kill_at(1)
    assert isinstance(entry["cursorKillTimeMs"], int)
    assert entry["cursorGuildId"] == 1


def test_the_cursor_only_moves_past_rows_that_got_a_verdict(tmp_path):
    """A row stating no killTime cannot be a boundary, so the cursor stays on the
    last one that could be."""
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2, kill_ms=None)]})
    sweep(client, tmp_path)
    assert state_of(tmp_path)["cursorKillTimeMs"] == kill_at(1)
    assert state_of(tmp_path)["cursorGuildId"] == 1


def test_a_second_run_skips_behind_the_cursor_and_the_done_set(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2)]})
    sweep(client, tmp_path)
    client.rankings[(ENC, MYTHIC)].append(row(3))
    before = len(client.queries)
    sweep(client, tmp_path)
    pulls = [q for q in client.queries[before:] if q[0].startswith("pulls:")]
    assert [q[1]["g"] for q in pulls] == [3]
    assert [r["guildId"] for r in rows_of(tmp_path)] == [1, 2, 3]


# ── the wall ────────────────────────────────────────────────────────────────────


def _pages(rows_per_page, pages, more):
    def fetch(page):
        chunk = [row(page * 1000 + i) for i in range(rows_per_page)]
        return {
            "worldData": {"encounter": {"fightRankings": {"rankings": chunk, "hasMorePages": more}}}
        }

    return fetch


def test_walled_needs_page_20_more_pages_claimed_and_nothing_fresh():
    done = {page * 1000 + i for page in range(1, 21) for i in range(50)}
    walk = walk_ranking(_pages(50, 20, True), done=done, cursor=None, cap=None)
    assert walk.walled is True
    assert walk.exhausted is False
    assert walk.pages_read == 20


def test_walled_is_not_claimed_when_the_cap_stopped_the_walk():
    # Everything before page 20 is done, so the cap fills up ON page 20 -- the one
    # place a cap stop and the wall could be confused.
    done = {page * 1000 + i for page in range(1, 20) for i in range(50)}
    walk = walk_ranking(_pages(50, 20, True), done=done, cursor=None, cap=10)
    assert walk.cap_stopped is True
    assert walk.pages_read == 20
    assert walk.walled is False
    assert walk.exhausted is False
    assert len(walk.fresh) == 10


def test_walled_is_not_claimed_when_the_last_page_offered_something_fresh():
    done = {page * 1000 + i for page in range(1, 21) for i in range(50)} - {20000}
    walk = walk_ranking(_pages(50, 20, True), done=done, cursor=None, cap=None)
    assert walk.pages_read == 20 and [r.guild_id for r in walk.fresh] == [20000]
    assert walk.walled is False


def test_walled_is_not_claimed_below_the_api_s_last_page():
    walk = walk_ranking(_pages(50, 5, True), done=set(), cursor=None, cap=None, max_page=5)
    assert walk.walled is False


def test_exhausted_is_the_only_ending_that_means_no_more_guilds():
    walk = walk_ranking(_pages(3, 1, False), done=set(), cursor=None, cap=None)
    assert walk.exhausted is True and walk.walled is False


# ── the skip matrix ─────────────────────────────────────────────────────────────


def _entry(**overrides):
    entry = {
        "rankingExhausted": False,
        "walled": False,
        "stoppedOnBudget": False,
        "attempted": 0,
        "sweptAt": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"),
    }
    entry.update(overrides)
    return entry


CADENCE = Cadence()


def test_skip_frozen_exhausted_recently_with_nothing_attempted():
    assert skip_reason(_entry(rankingExhausted=True), frozen=True, now=NOW, cadence=CADENCE)


def test_skip_frozen_walled_recently_with_nothing_attempted():
    assert skip_reason(_entry(walled=True), frozen=True, now=NOW, cadence=CADENCE)


def test_never_skip_frozen_when_neither_exhausted_nor_walled():
    assert skip_reason(_entry(), frozen=True, now=NOW, cadence=CADENCE) is None


def test_never_skip_when_stopped_on_budget():
    entry = _entry(rankingExhausted=True, stoppedOnBudget=True)
    assert skip_reason(entry, frozen=True, now=NOW, cadence=CADENCE) is None


def test_never_skip_when_the_last_run_attempted_something():
    entry = _entry(rankingExhausted=True, attempted=3)
    assert skip_reason(entry, frozen=True, now=NOW, cadence=CADENCE) is None


def test_refresh_after_re_walks_a_frozen_exhausted_boss():
    old = (NOW - timedelta(hours=169)).isoformat(timespec="seconds")
    entry = _entry(rankingExhausted=True, sweptAt=old)
    assert skip_reason(entry, frozen=True, now=NOW, cadence=CADENCE) is None


def test_live_zone_skips_for_six_hours_only():
    assert skip_reason(_entry(), frozen=False, now=NOW, cadence=CADENCE)
    old = (NOW - timedelta(hours=7)).isoformat(timespec="seconds")
    assert skip_reason(_entry(sweptAt=old), frozen=False, now=NOW, cadence=CADENCE) is None


def test_live_and_walled_skips_for_a_day():
    twelve = (NOW - timedelta(hours=12)).isoformat(timespec="seconds")
    assert skip_reason(_entry(walled=True, sweptAt=twelve), frozen=False, now=NOW, cadence=CADENCE)
    day = (NOW - timedelta(hours=25)).isoformat(timespec="seconds")
    assert (
        skip_reason(_entry(walled=True, sweptAt=day), frozen=False, now=NOW, cadence=CADENCE)
        is None
    )


def test_no_state_is_never_a_skip():
    assert skip_reason(None, frozen=True, now=NOW, cadence=CADENCE) is None


def test_a_skipped_encounter_sends_no_query(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]})
    sweep(client, tmp_path)
    # The first run attempted one guild, so it is never skipped: the second run
    # re-walks the ranking, attempts nothing and records the exhaustion. Only THEN
    # does the frozen zone's boss count as done.
    sweep(client, tmp_path)
    assert state_of(tmp_path)["attempted"] == 0
    before = len(client.queries)
    sweep(client, tmp_path)
    assert len(client.queries) == before


# ── the two schema alarms ───────────────────────────────────────────────────────


def test_an_unscreened_row_stops_the_pair_writes_nothing_and_the_next_pair_runs(tmp_path):
    client = StubClient(
        rankings={
            (ENC, MYTHIC): [row(1), row(2, fromlog=None)],
            (ENC, HEROIC): [row(3)],
        }
    )
    report = sweep(client, tmp_path, difficulties=(MYTHIC, HEROIC))
    assert report.exit_code == 3
    assert report.alarmed_pairs == ["z44-d5"]
    assert not (tmp_path / "z44-d5.rows.jsonl").exists()
    assert not (tmp_path / "z44-d5.state.json").exists()
    assert not [q for q in client.queries if q[0].startswith("pulls:") and q[1]["d"] == MYTHIC]
    assert [r["guildId"] for r in rows_of(tmp_path, difficulty=HEROIC)] == [3]


def test_a_row_with_fromlog_but_no_kill_time_is_refused_and_pull_time_is_never_called(
    tmp_path, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise AssertionError("pull_time was reached with a row that has no killTime")

    monkeypatch.setattr(progresshours, "pull_time", boom)
    client = StubClient(rankings={(ENC, MYTHIC): [row(1, kill_ms=None)]})
    report = sweep(client, tmp_path)
    assert report.exit_code == 0
    assert [r["outcome"] for r in refused_of(tmp_path)] == ["no-kill-time"]
    assert not [q for q in client.queries if q[0].startswith("pulls:")]


# ── idempotency and the file discipline ─────────────────────────────────────────


def _snapshot(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def test_a_second_identical_run_produces_byte_identical_files(tmp_path):
    """Once a pair is settled -- walked, nothing attempted -- an identical run must
    change no byte. (The run straight after a measuring one still rewrites
    `attempted` from N to 0, which is the skip matrix's own input.)"""
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2)]})
    sweep(client, tmp_path)
    sweep(client, tmp_path)
    settled = _snapshot(tmp_path)
    sweep(client, tmp_path)
    assert _snapshot(tmp_path) == settled


def test_a_run_that_measures_nothing_leaves_rows_jsonl_byte_identical(tmp_path, monkeypatch):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2)]})
    sweep(client, tmp_path)
    rows_path = tmp_path / "z44-d5.rows.jsonl"
    before = rows_path.read_bytes()
    # Re-walk (stoppedOnBudget forces it), every row already done -> nothing measured.
    state = json.loads((tmp_path / "z44-d5.state.json").read_text())
    state["encounters"][str(ENC)]["stoppedOnBudget"] = True
    (tmp_path / "z44-d5.state.json").write_text(json.dumps(state))
    written = []
    real = progresssweep.atomic_write

    def spy(path, text):
        written.append(Path(path).name)
        real(path, text)

    monkeypatch.setattr(progresssweep, "atomic_write", spy)
    report = sweep(client, tmp_path)
    assert report.new_rows == 0
    assert rows_path.read_bytes() == before
    # Not merely the same bytes: the file is not TOUCHED. Append-only is a promise
    # about what a writer does, and a rewrite that happens to reproduce the bytes
    # today is one normalisation away from not doing so.
    assert "z44-d5.rows.jsonl" not in written
    assert "z44-d5.state.json" in written
    assert state_of(tmp_path)["attempted"] == 0


def test_a_re_walk_that_finds_nothing_new_rewrites_nothing_different(tmp_path):
    """A live zone is re-walked every run; with the clock held, two such runs must
    produce identical files -- which is what a manifest stamp would break."""
    client = StubClient(
        zones={ZONE: zone_payload(frozen=False)}, rankings={(ENC, MYTHIC): [row(1)]}
    )
    live = Cadence(refresh_after_live_hours=0)
    sweep(client, tmp_path, cadence=live)
    sweep(client, tmp_path, cadence=live)
    settled = _snapshot(tmp_path)
    queries = len(client.queries)
    sweep(client, tmp_path, cadence=live)
    assert len(client.queries) > queries, "the live zone was walked again"
    assert _snapshot(tmp_path) == settled


def test_writes_are_atomic_and_leave_no_temp_file(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]})
    sweep(client, tmp_path)
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "README.md").is_file()


def test_atomic_write_replaces_rather_than_appends_in_place(tmp_path, monkeypatch):
    calls = []
    real = progresssweep.os.replace

    def spy(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        real(src, dst)

    monkeypatch.setattr(progresssweep.os, "replace", spy)
    progresssweep.atomic_write(tmp_path / "x.jsonl", "a\n")
    assert calls == [("x.jsonl.tmp", "x.jsonl")]


def test_duplicate_lines_are_deduplicated_on_load_last_wins_and_counted(tmp_path):
    lines = [
        {"v": 1, "encounterId": ENC, "guildId": 1, "hours": 1.0},
        {"v": 1, "encounterId": ENC, "guildId": 1, "hours": 2.0},
        {"v": 1, "encounterId": ENC2, "guildId": 1, "hours": 3.0},
    ]
    (tmp_path / "z44-d5.rows.jsonl").write_text("".join(progresssweep.dumps_line(x) for x in lines))
    pair = progresssweep.load_pair(tmp_path, ZONE, MYTHIC)
    assert pair.duplicate_rows == 1
    assert pair.rows[(ENC, 1)]["hours"] == 2.0
    assert pair.done(ENC) == {1}
    assert pair.done(ENC2) == {1}
    assert len(pair.row_lines) == 3, "the file's lines are kept verbatim"


def test_the_manifest_carries_counts_and_no_run_timestamp(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2, fromlog=0)]})
    sweep(client, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert set(manifest) == {"v", "files"}
    (entry,) = manifest["files"]
    assert entry == {
        "path": "z44-d5.rows.jsonl",
        "zoneId": ZONE,
        "difficulty": MYTHIC,
        "rows": 1,
        "refused": 1,
        "lastSweptAt": NOW.isoformat(timespec="seconds"),
    }


# ── budget: deadline, ceiling, 429 ──────────────────────────────────────────────


def test_the_deadline_stops_the_run_marks_the_encounter_and_exits_zero(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2), row(3)]})
    # construction, the opening check, the ranking walk's, guild 1's, guild 2's --
    # then the clock jumps past the deadline before guild 3's walk.
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0])
    report = run_sweep(
        client,
        options(deadline_minutes=1),
        tmp_path,
        sleep=lambda _s: None,
        clock=lambda: next(ticks, 10_000.0),
        now=lambda: NOW,
        run_id="t",
    )
    assert report.exit_code == 0
    assert report.stopped
    entry = state_of(tmp_path)
    assert entry["stoppedOnBudget"] is True
    assert 0 < entry["attempted"] < 3
    assert len(rows_of(tmp_path)) == entry["attempted"]


def test_at_the_ceiling_the_run_sleeps_until_the_counter_resets_then_continues(tmp_path):
    over = {"limitPerHour": 18000.0, "pointsSpentThisHour": 17000.0, "pointsResetIn": 120}
    under = {"limitPerHour": 18000.0, "pointsSpentThisHour": 10.0, "pointsResetIn": 3600}
    client = StubClient(
        rankings={(ENC, MYTHIC): [row(1)]},
        readings=[under, under, over, under, under],
    )
    slept = []
    report = run_sweep(
        client,
        options(),
        tmp_path,
        sleep=slept.append,
        clock=lambda: 0.0,
        now=lambda: NOW,
        run_id="t",
    )
    assert slept == [120 + progresssweep.RESET_SLACK_SECONDS]
    assert report.stopped is None
    assert [r["guildId"] for r in rows_of(tmp_path)] == [1]


def test_a_reset_past_the_deadline_stops_instead_of_sleeping(tmp_path):
    over = {"limitPerHour": 18000.0, "pointsSpentThisHour": 17000.0, "pointsResetIn": 3600}
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]}, readings=[over])
    slept = []
    report = sweep(client, tmp_path, deadline_minutes=10)
    assert slept == []
    assert report.stopped and "deadline" in report.stopped
    assert report.exit_code == 0


def test_a_429_marks_stopped_on_budget_writes_and_exits_zero(tmp_path):
    client = StubClient(
        rankings={(ENC, MYTHIC): [row(1), row(2)]},
        pulls={(2, ENC, MYTHIC): RateLimited("429")},
    )
    report = sweep(client, tmp_path)
    assert report.exit_code == 0
    assert "rate limited" in report.stopped
    assert [r["guildId"] for r in rows_of(tmp_path)] == [1]
    entry = state_of(tmp_path)
    assert entry["stoppedOnBudget"] is True
    assert entry["cursorGuildId"] == 1, "the interrupted guild is not passed"


def test_the_budget_is_read_before_every_walk(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2), row(3)]})
    sweep(client, tmp_path)
    # one before the first walk, one before the ranking walk, one per guild
    assert client.rate_limit_calls == 1 + 1 + 3


# ── the per-guild walk ──────────────────────────────────────────────────────────


def test_every_sweep_query_bypasses_the_response_cache(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]})
    sweep(client, tmp_path)
    assert client.queries and all(cache is False for _, _, cache in client.queries)
    assert client.zone_calls == [(ZONE, False)]


def test_a_measured_row_carries_the_raw_inputs_in_the_contract_s_shape(tmp_path):
    kill = kill_at(1)
    night_one = kill - 3 * 86_400_000
    pages = [
        {
            "has_more_pages": True,
            "data": [
                {
                    "code": "NEW",
                    "startTime": kill - HOUR_MS,
                    "fights": [
                        {
                            "startTime": HOUR_MS - 600_000,
                            "endTime": HOUR_MS - 60_000,
                            "kill": False,
                            "encounterID": ENC,
                            "difficulty": MYTHIC,
                        },
                        {
                            "startTime": HOUR_MS,
                            "endTime": HOUR_MS + 200_000,
                            "kill": True,
                            "encounterID": ENC,
                            "difficulty": MYTHIC,
                        },
                        # after the kill: never part of the progression
                        {
                            "startTime": HOUR_MS + 900_000,
                            "endTime": HOUR_MS + 1_000_000,
                            "kill": False,
                            "encounterID": ENC,
                            "difficulty": MYTHIC,
                        },
                    ],
                }
            ],
        },
        {
            "has_more_pages": False,
            "data": [
                {
                    "code": "OLD",
                    "startTime": night_one,
                    "fights": [
                        {
                            "startTime": 0,
                            "endTime": 300_000,
                            "kill": False,
                            "encounterID": ENC,
                            "difficulty": MYTHIC,
                        },
                        {
                            "startTime": 400_000,
                            "endTime": 700_000,
                            "kill": False,
                            "encounterID": ENC,
                            "difficulty": MYTHIC,
                        },
                    ],
                },
                {"code": "OTHER", "startTime": night_one + 86_400_000, "fights": []},
            ],
        },
    ]
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]}, pulls={(1, ENC, MYTHIC): pages})
    sweep(client, tmp_path)
    (line,) = rows_of(tmp_path)
    assert list(line) == [
        "v",
        "zoneId",
        "encounterId",
        "difficulty",
        "guildId",
        "guildName",
        "serverSlug",
        "serverRegion",
        "hours",
        "attempts",
        "nightsObserved",
        "spanDays",
        "firstKillAtMs",
        "killAtMs",
        "reportsSeen",
        "reportStartsMs",
        "nightsMs",
        "measuredAt",
        "run",
    ]
    assert line["guildName"] == "Guild 1"
    assert line["serverSlug"] == "tarren-mill" and line["serverRegion"] == "EU"
    assert line["hours"] == (300_000 + 300_000 + 540_000 + 200_000) / progresshours.MS_PER_HOUR
    assert line["attempts"] == 4 and line["nightsObserved"] == 2
    assert line["firstKillAtMs"] == kill and line["killAtMs"] == kill
    assert line["reportsSeen"] == 3
    # ascending, ints, every report the walk read -- OTHER included
    assert line["reportStartsMs"] == [night_one, night_one + 86_400_000, kill - HOUR_MS]
    assert line["nightsMs"] == [
        [night_one, night_one + 700_000],
        [kill - 600_000, kill + 200_000],
    ]
    assert line["measuredAt"] == NOW.isoformat(timespec="seconds") and line["run"] == "test"
    assert "loggingGapRatio" not in line and "nightSpanHours" not in line
    raw = (tmp_path / "z44-d5.rows.jsonl").read_text()
    assert ": " not in raw and raw.endswith("\n"), "compact JSON, newline-terminated"


def test_night_pairs_partition_on_the_previous_night_s_end():
    attempt = progresshours.Attempt
    gap = progresssweep.NIGHT_GAP_MS
    attempts = [
        attempt(0.0, 1000.0, False),
        attempt(1000.0 + gap, 1000.0 + gap + 10.0, False),  # exactly the gap: same night
        attempt(1000.0 + gap + 11.0 + gap, 1000.0 + 3 * gap, True),
    ]
    assert progresssweep.night_pairs(attempts) == [
        [0, 1000 + gap + 10],
        [1000 + gap + 11 + gap, 1000 + 3 * gap],
    ]


def test_a_truncated_report_walk_is_refused_not_summed(tmp_path):
    pages = [{"has_more_pages": True, "data": good_pulls(kill_at(1))[0]["data"]}] * 4
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]}, pulls={(1, ENC, MYTHIC): pages})
    sweep(client, tmp_path, max_pages=4)
    assert [r["outcome"] for r in refused_of(tmp_path)] == ["truncated"]
    assert rows_of(tmp_path) == []


def test_a_report_seen_on_two_pages_is_counted_once(tmp_path):
    page = good_pulls(kill_at(1))[0]
    pages = [{"has_more_pages": True, "data": page["data"]}, page]
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]}, pulls={(1, ENC, MYTHIC): pages})
    sweep(client, tmp_path)
    (line,) = rows_of(tmp_path)
    assert line["reportsSeen"] == 1 and line["attempts"] == 2


def test_the_screens_refuse_before_and_after_the_report_walk(tmp_path):
    late = good_pulls(kill_at(3) + 3 * HOUR_MS)  # log says the kill was hours later
    client = StubClient(
        rankings={(ENC, MYTHIC): [row(1, fromlog=0), row(2), row(3)]},
        pulls={(2, ENC, MYTHIC): [{"has_more_pages": False, "data": []}], (3, ENC, MYTHIC): late},
    )
    sweep(client, tmp_path)
    outcomes = {r["guildId"]: r["outcome"] for r in refused_of(tmp_path)}
    assert outcomes == {1: "unlogged-kill", 2: "no-reports", 3: "kill-too-late"}
    assert not [q for q in client.queries if q[0].startswith("pulls:") and q[1]["g"] == 1]
    assert state_of(tmp_path)["outcomes"] == {
        "kill-too-late": 1,
        "no-reports": 1,
        "unlogged-kill": 1,
    }
    assert refused_of(tmp_path)[0].keys() == {
        "v",
        "encounterId",
        "guildId",
        "outcome",
        "killTimeMs",
        "reportsSeen",
        "at",
        "run",
    }


def test_a_guild_whose_fetch_fails_is_an_error_row_and_the_run_continues(tmp_path):
    client = StubClient(
        rankings={(ENC, MYTHIC): [row(1), row(2)]},
        pulls={(1, ENC, MYTHIC): WarcraftLogsError("request failed: ReadTimeout")},
    )
    report = sweep(client, tmp_path)
    assert report.stopped is None
    assert [r["outcome"] for r in refused_of(tmp_path)] == ["error"]
    assert [r["guildId"] for r in rows_of(tmp_path)] == [2]
    assert state_of(tmp_path)["cursorGuildId"] == 2


def test_retry_errors_re_attempts_exactly_the_error_guilds(tmp_path):
    client = StubClient(
        rankings={(ENC, MYTHIC): [row(1), row(2, fromlog=0), row(3)]},
        pulls={(1, ENC, MYTHIC): WarcraftLogsError("boom")},
    )
    sweep(client, tmp_path)
    client.pulls.clear()
    client.rankings[(ENC, MYTHIC)].append(row(4))  # fresh, but a retry does not take it
    before = len(client.queries)
    sweep(client, tmp_path, retry_errors=True)
    pulls = [q[1]["g"] for q in client.queries[before:] if q[0].startswith("pulls:")]
    assert pulls == [1]
    assert [r["guildId"] for r in rows_of(tmp_path)] == [3, 1]


def test_an_httpx_timeout_inside_the_real_client_becomes_an_error_row(tmp_path):
    """End to end through `WarcraftLogsClient.query`: the httpx mapping plus the
    sweep's per-guild handling, with only the HTTP hop replaced."""
    rankings = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2)]})

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, data):
            self._data = data

        def json(self):
            return {"data": self._data}

    def post(url, json=None, headers=None, **_):
        variables = json["variables"]
        document = json["query"]
        if document == progresshours.GUILD_PULLS_QUERY and variables["g"] == 1:
            raise httpx.ReadTimeout("slow guild")
        if document == progresshours.PROGRESS_RANKINGS_QUERY:
            return Response(rankings.query(progresshours.PROGRESS_RANKINGS_QUERY, variables))
        if document == progresshours.GUILD_PULLS_QUERY:
            return Response(rankings.query(progresshours.GUILD_PULLS_QUERY, variables))
        if "rateLimitData" in document and "zone(" not in document:
            return Response(
                {
                    "rateLimitData": {
                        "limitPerHour": 18000,
                        "pointsSpentThisHour": 5,
                        "pointsResetIn": 900,
                    }
                }
            )
        return Response({"worldData": {"zone": zone_payload()}})

    client = WarcraftLogsClient(Credentials("id", "secret"))
    client._token = "token"
    client._client.post = post
    report = sweep(client, tmp_path)
    assert report.stopped is None
    assert [r["outcome"] for r in refused_of(tmp_path)] == ["error"]
    assert [r["guildId"] for r in rows_of(tmp_path)] == [2]


# ── zones ───────────────────────────────────────────────────────────────────────


def test_encounters_come_from_the_zone_never_from_fight_profiles(tmp_path):
    client = StubClient(
        zones={ZONE: zone_payload(encounters=((ENC, "A"), (ENC2, "B")))},
        rankings={(ENC, MYTHIC): [row(1)], (ENC2, MYTHIC): [row(2)]},
    )
    sweep(client, tmp_path)
    assert sorted(r["encounterId"] for r in rows_of(tmp_path)) == [ENC, ENC2]
    state = json.loads((tmp_path / "z44-d5.state.json").read_text())
    assert state["zoneName"] == "Manaforge Omega" and state["frozen"] is True
    assert set(state["encounters"]) == {str(ENC), str(ENC2)}


def test_a_zone_the_service_does_not_list_is_skipped_with_exit_2(tmp_path):
    client = StubClient(
        zones={ZONE: None, 46: zone_payload(46)}, rankings={(ENC, MYTHIC): [row(1)]}
    )
    report = sweep(client, tmp_path, zones=(ZONE, 46))
    assert report.exit_code == 2
    assert report.zones_skipped == [ZONE]
    assert [r["guildId"] for r in rows_of(tmp_path, zone=46)] == [1]


def test_named_and_shape_measure_the_guild_block(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1, name=None, server=None)]})
    sweep(client, tmp_path)
    entry = state_of(tmp_path)
    assert entry["named"] == 0 and entry["guildsSeen"] == 1
    assert entry["shape"].startswith("row ['fromlog', 'guild', 'killTime'], guild ['id']")
    (line,) = rows_of(tmp_path)
    assert line["guildName"] is None and line["serverSlug"] is None


def test_rows_without_a_guild_id_are_counted_and_never_enter_the_done_set(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [{"killTime": KILL, "fromlog": 1}, row(1)]})
    sweep(client, tmp_path)
    entry = state_of(tmp_path)
    assert entry["withoutGuild"] == 1 and entry["guildsSeen"] == 2
    assert [r["guildId"] for r in rows_of(tmp_path)] == [1]


# ── the validator ───────────────────────────────────────────────────────────────


def _git(cwd: Path, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _committed_sweep(tmp_path):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1), row(2)]})
    sweep(client, tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")


def test_validate_passes_on_what_the_sweep_wrote(tmp_path):
    _committed_sweep(tmp_path)
    assert progresssweep.validate(tmp_path) == []


def test_validate_refuses_a_file_shorter_than_at_head(tmp_path):
    _committed_sweep(tmp_path)
    rows_path = tmp_path / "z44-d5.rows.jsonl"
    rows_path.write_text(rows_path.read_text().splitlines(keepends=True)[0])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["rows"] = 1
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    problems = progresssweep.validate(tmp_path)
    assert any("shorter than HEAD" in p for p in problems), problems


def test_validate_refuses_manifest_counts_that_disagree_with_the_files(tmp_path):
    _committed_sweep(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["rows"] = 5
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    problems = progresssweep.validate(tmp_path)
    assert any("manifest says 5" in p for p in problems), problems


def test_validate_refuses_a_line_that_is_not_json(tmp_path):
    _committed_sweep(tmp_path)
    with (tmp_path / "z44-d5.rows.jsonl").open("a") as handle:
        handle.write("{not json\n")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["rows"] = 3
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    problems = progresssweep.validate(tmp_path)
    assert any("not JSON" in p for p in problems), problems


def test_validate_refuses_a_missing_manifest(tmp_path):
    assert progresssweep.validate(tmp_path) == ["manifest.json is missing"]


# ── the command line ────────────────────────────────────────────────────────────


def test_the_parser_refuses_zone_zero_and_non_ints(capsys):
    with pytest.raises(SystemExit):
        cli.main(["progress-sweep", "--zones", "53,0", "--out", "x"])
    assert "refuses 0" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["progress-sweep", "--zones", "53,abc", "--out", "x"])
    assert "whole numbers" in capsys.readouterr().err


def test_the_parser_keeps_the_zones_in_the_order_given():
    parser = cli.build_parser()
    args = parser.parse_args(["progress-sweep", "--zones", "44,53", "--out", "x"])
    assert args.zones == (44, 53)
    assert args.difficulties == (5, 4)
    assert args.guilds == 200 and args.max_pages == 4 and args.rankings_pages == 20
    assert args.point_ceiling == 0.6 and args.deadline_minutes == 300
    assert args.refresh_after == 168 and args.refresh_after_live == 6
    assert args.live_wall_refresh_after == 24 and args.workers == 1


def test_seed_only_prints_the_pairs_and_sends_no_query(tmp_path, monkeypatch, capsys):
    client = StubClient(rankings={(ENC, MYTHIC): [row(1)]})
    sweep(client, tmp_path)

    def refuse(*_a, **_k):
        raise AssertionError("a client was constructed under --seed-only")

    monkeypatch.setattr(cli, "WarcraftLogsClient", refuse, raising=False)
    monkeypatch.setattr("wowdps.warcraftlogs.WarcraftLogsClient", refuse)
    code = cli.main(
        [
            "progress-sweep",
            "--zones",
            "44",
            "--difficulties",
            "5",
            "--seed-only",
            "--out",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "z44-d5: 1 row(s), 0 refusal(s), 1 encounter state(s)" in out
    assert f"cursor {kill_at(1)}/1" in out and "walled False" in out


def test_more_than_one_worker_is_refused_with_a_reason(tmp_path, caplog):
    code = cli.main(["progress-sweep", "--workers", "2", "--out", str(tmp_path)])
    assert code == 1
    assert "only 1 is implemented" in caplog.text


def test_validate_is_reachable_from_the_command_line(tmp_path):
    assert cli.main(["progress-sweep", "--validate", str(tmp_path)]) == 1
    _committed_sweep(tmp_path)
    assert cli.main(["progress-sweep", "--validate", str(tmp_path)]) == 0
