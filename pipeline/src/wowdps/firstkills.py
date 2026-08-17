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


#: Epoch milliseconds for 2000-01-01. A kill time below this is not a date, it is
#: a report-relative offset that never got its base added -- the exact bug the
#: `report_start_ms` argument exists to prevent, and the one that made the first
#: live run report 100% of kills as "earlier than the ranked sample".
_IMPLAUSIBLE_BEFORE_MS = 946_684_800_000


def kills_from_report(
    code: str,
    fights: object,
    encounter_id: int,
    report_start_ms: float = 0.0,
    difficulty: int | None = None,
) -> list[KillRow]:
    """Kills of one encounter out of a report's ``fights`` list, in epoch time.

    ``ReportFight.startTime`` is milliseconds **since the report began**, not an
    epoch timestamp, so ``report_start_ms`` is added to every one. Getting this
    wrong has dramatic effects and is very hard to spot: every kill comes out as a
    number near zero, which compares as older than any real date, so a "which kills
    are earliest" search returns *everything* and reads as a spectacular finding.
    The first live run reported 89 of 89 and 102 of 102 kills as earlier than the
    ranked sample. **100% is the tell** -- a real answer to "did the rankings hide
    anything" is never unanimous.

    ``difficulty`` must match what the probe will ask for afterwards. The search is
    unfiltered by difficulty on purpose -- one query per report serves every boss,
    which is what makes it affordable -- so the filter belongs here.

    Leaving it out is not harmless: the first run against Season 2's PTR zone found
    54 kills of one boss and sampled **none**, with `fight_structure` answering
    "fight 7 not in the report's fights" once per kill. What that means is that the
    search and the probe disagreed about which fights exist, and difficulty is the
    only filter between them. *Why* they disagreed is not settled -- the owner says
    PTR testing does happen on Mythic, so "these were all Heroic" is not the
    explanation it looked like. `difficulties_seen` exists to answer it from a run
    rather than from a guess.

    ``None`` means any, and is the setting to reach for when a zone's kills are not
    where they are expected to be.

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
        if difficulty is not None and fight.get("difficulty") != difficulty:
            continue
        start = fight.get("startTime")
        end = fight.get("endTime")
        if not isinstance(start, (int, float)):
            continue
        absolute = report_start_ms + float(start)
        if absolute < _IMPLAUSIBLE_BEFORE_MS:
            # Loud rather than silently early, and dropped rather than kept: a row
            # whose time base is unknown cannot be ranked against rows whose is.
            log.warning(
                "report %s fight %s: kill time %.0f is before the year 2000, so the "
                "report start is missing -- skipping rather than sorting it first",
                code,
                fight.get("id"),
                absolute,
            )
            continue
        rows.append(
            KillRow(
                report_code=code,
                fight_id=int(fight.get("id") or 0),
                started_at=absolute,
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


def search_window(
    anchor_ms: float, lookback_days: float, forward_days: float, now_ms: float | None = None
) -> tuple[int, int]:
    """The ``[startTime, endTime]`` to search, in epoch milliseconds.

    ``anchor_ms`` is the earliest kill already known -- from the ranking sample, or
    from a previous run's payload. The window reaches *back* from it because the
    whole point is to find kills the rankings could not show, and forward far enough
    to fill the sample once the early ones are in hand.

    **An anchor of zero means the whole of time**, and that is the case this route
    exists for rather than a failure of it. A PTR zone has no ranked parses at all,
    so there is nothing to anchor on -- and it is also a zone that has existed for
    weeks, so its entire report list is small and searching it from the beginning is
    both correct and cheap. Refusing here is what the first run against zone 54 did,
    and it left the eight Season 2 bosses with no measurements while the reports were
    sitting there.

    A negative start is clamped to zero: Warcraft Logs reads 0 as "from the
    beginning", and a negative timestamp is not a smaller number to it, it is
    nonsense.
    """
    if not anchor_ms:
        import time

        return 0, int(now_ms if now_ms is not None else time.time() * 1000)
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
    #: Set when the point ceiling stopped the search. The sample is then the
    #: earliest of what was *reached*, not the earliest that exists, and the two
    #: must not be reported as the same thing.
    aborted: str | None = None

    def summary(self, anchor_ms: float) -> str:
        base = (
            f"{self.reports_seen} report(s) over {self.pages_read} page(s), "
            f"{self.kills_found} kill(s)"
        )
        if not anchor_ms:
            return f"{base}; no ranked kill to compare against, so the whole zone was searched"
        if self.aborted:
            base = f"{base}, STOPPED EARLY ({self.aborted})"
        if self.beat_anchor:
            return f"{base}; {self.beat_anchor} earlier than the best-parse sample"
        if self.aborted:
            return f"{base}; none earlier so far, but the search did not finish"
        return f"{base}; none earlier than the best-parse sample"


def difficulties_seen(fights: object, encounter_id: int) -> dict[int | None, int]:
    """How many kills of one encounter sit at each difficulty, for diagnosis.

    Written because a guess was made once and was wrong. When a search finds kills
    that the probe then cannot open, this says whether difficulty is the reason --
    and it reports ``None`` as its own key, because "the field was absent" and "the
    field said Heroic" are different findings.
    """
    counts: dict[int | None, int] = {}
    if not isinstance(fights, list):
        return counts
    for fight in fights:
        if not isinstance(fight, dict) or fight.get("encounterID") != encounter_id:
            continue
        if not fight.get("kill"):
            continue
        key = fight.get("difficulty")
        counts[key if isinstance(key, int) else None] = counts.get(key, 0) + 1
    return counts
