"""Selecting kills by *when they happened*, off the report search rather than rankings.

``characterRankings`` cannot answer "who killed this first". It is sorted by damage
and contains only parses Warcraft Logs chose to rank, so the best the probe can do
with it is sort a window of the top parses by date -- which returns the earliest of
the *best*, systematically later than the earliest full stop, and gives no way at
all to see a public log that was never ranked.

Introspection settled that there is another route (2026-08-16, against the live
service, recorded in CLAUDE.md):

    ReportData.reports(zoneID:, startTime:, endTime:, limit:, page:)

That is every log uploaded for a zone in a time window, ranked or not, bounded by
*time* instead of by damage. Each report then yields its kills through
``Report.fights(encounterID:, killType: Kills)``, and ``ReportFight`` already
carries everything ``_probe_fight`` reads, so only the selection changes.

Two properties this module is built around, both because the pagination envelope
could **not** be introspected -- ``ReportPagination`` is the declared return type of
``reports`` and the server refuses to describe it:

- **Nothing here assumes an order.** Every kill found in the window is sorted by its
  own ``startTime`` locally. If Warcraft Logs returns reports newest-first,
  oldest-first, or in no particular order, the answer is the same.
- **Paging stops on a short page**, not on a ``has_more_pages`` field whose name is
  unverified. A page returning fewer rows than the limit is the last one.

The window is anchored rather than guessed. Nothing in the schema says when a raid
opened -- ``Zone.partitions`` has names and no dates -- so the anchor is the earliest
kill already known from the ranking sample, and the search runs from before it. That
makes the question concrete and checkable: *is there a public kill earlier than the
earliest ranked one?* A run that finds nothing earlier is a real answer, not a
failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

DAY_MS = 86_400_000


@dataclass(frozen=True)
class KillRow:
    """One kill of one encounter, with the report it lives in."""

    report_code: str
    fight_id: int
    started_at: float
    duration_ms: float = 0.0


def reports_from_payload(payload: object) -> list[dict]:
    """The report rows out of a ``reports`` response, whatever it is wrapped in.

    ``ReportPagination`` cannot be introspected, so the envelope is read
    defensively: a ``data`` list is the Laravel-style shape these APIs normally
    use, and a bare list is accepted too. Anything else yields nothing rather than
    an exception -- an unrecognised envelope should make the run report zero
    reports, which is visible, instead of crashing a nine-boss pass.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        found = payload.get("data")
        rows = found if isinstance(found, list) else []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("code")]


def kills_from_report(code: str, fights: object, encounter_id: int) -> list[KillRow]:
    """Kills of one encounter out of a report's ``fights`` list.

    ``killType: Kills`` is passed to the query, but ``kill`` is re-checked here: a
    filter that silently stopped filtering would otherwise put wipes into a sample
    of first *kills*, and a wipe on the first night is exactly the row that would
    win a "who killed it first" sort.
    """
    if not isinstance(fights, list):
        return []
    rows: list[KillRow] = []
    for fight in fights:
        if not isinstance(fight, dict):
            continue
        if fight.get("encounterID") != encounter_id or not fight.get("kill"):
            continue
        start = fight.get("startTime")
        end = fight.get("endTime")
        if not isinstance(start, (int, float)):
            continue
        rows.append(
            KillRow(
                report_code=code,
                fight_id=int(fight.get("id") or 0),
                started_at=float(start),
                duration_ms=float(end) - float(start) if isinstance(end, (int, float)) else 0.0,
            )
        )
    return rows


def earliest_kills(rows: list[KillRow], limit: int) -> list[KillRow]:
    """The earliest kills by start time, one per report.

    One per report for the same reason the ranking selector does it: two kills in
    one log are one guild's evening, and a sample of thirty that is really three
    guilds describes three guilds. Ties break on report code so a re-run is
    reproducible.
    """
    by_report: dict[str, KillRow] = {}
    for row in sorted(rows, key=lambda row: (row.started_at, row.report_code)):
        by_report.setdefault(row.report_code, row)
    ordered = sorted(by_report.values(), key=lambda row: (row.started_at, row.report_code))
    return ordered[:limit]


def search_window(anchor_ms: float, lookback_days: float, forward_days: float) -> tuple[int, int]:
    """The ``[startTime, endTime]`` to search, in epoch milliseconds.

    ``anchor_ms`` is the earliest kill already known -- from the ranking sample, or
    from a previous run's payload. The window reaches *back* from it because the
    whole point is to find kills the rankings could not show, and forward far enough
    to fill the sample once the early ones are in hand.

    A zero or negative start is clamped to zero: Warcraft Logs treats 0 as "from the
    beginning", and a negative timestamp is not a smaller number to it, it is
    nonsense.
    """
    start = max(0.0, anchor_ms - lookback_days * DAY_MS)
    end = anchor_ms + forward_days * DAY_MS
    return int(start), int(end)


@dataclass
class SearchOutcome:
    """What a report search found, itemised enough to argue with.

    ``beat_anchor`` is the finding: kills strictly earlier than the earliest ranked
    parse the probe already knew about. If it is zero across a whole zone, then the
    rankings were not hiding anything after all, and that is worth publishing rather
    than quietly reverting to them.
    """

    reports_seen: int = 0
    pages_read: int = 0
    kills_found: int = 0
    beat_anchor: int = 0
    truncated: bool = False

    def summary(self, anchor_ms: float) -> str:
        base = (
            f"{self.reports_seen} report(s) over {self.pages_read} page(s), "
            f"{self.kills_found} kill(s)"
        )
        if not anchor_ms:
            return base
        if self.beat_anchor:
            return f"{base}; {self.beat_anchor} earlier than the best-parse sample"
        return f"{base}; none earlier than the best-parse sample"
