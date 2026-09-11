"""The cohort sweep: every guild's progress hours, per boss, into a git-backed file set.

`wowdps progress-sweep`. Pure functions over payloads and file contents, plus one
runner (:func:`run_sweep`) that a stub client can drive end to end without a network.

## What this is, and what it deliberately is not

`wowdps progress-hours` samples N guilds per boss and publishes MEDIANS for a chart.
This command walks a boss's WHOLE progress ranking -- up to the rank-1000 wall -- and
keeps one line per guild it judged, so the private import (the sibling
``wtt-backend``) can upsert them without spending a Warcraft Logs point or a Cloud Run
hour of its own. The two share :func:`progresshours.ranking_rows`,
:func:`progresshours.pull_time` and the two GraphQL documents; they do not share a
document format, because one is a chart and the other is a ledger.

The contract this implements is ``docs/progress-cohort.md``. Where a sentence below
and that document disagree, the document wins.

## Zone-addressed, never tier-addressed

Encounters come from ``worldData.zone(id:)`` -- LIVE ids, the zone's own list -- and
never from ``fight_profiles.json`` (which files MID2 under PTR ``53xxx`` ids) and never
through the PTR-twin resolution ``progress-hours`` needs. The private side refuses a
row whose ``zoneId`` disagrees with its catalogue, so a PTR id could not import even if
it were written; this side simply cannot write one.

## The cursor, ported 1:1 from the backend

``cursorKillTimeMs``/``cursorGuildId`` per encounter, and :func:`before_cursor` is the
backend's ``before_cursor`` verbatim: a row with a strictly earlier ``killTime`` was
already judged; an equal one only when it IS the cursor row; a row without a
``killTime`` is never skipped; the cursor moves forward only, and only past rows that
got a verdict -- measured or refused, ``error`` included, since an ``error`` names its
guild in ``refused.jsonl`` and ``--retry-errors`` is the way back to it. ``int()``
truncates the stored millisecond DOWN, towards re-attempting a guild rather than
skipping one.

## Two schema alarms, and they fail in different directions

- A ranking row carrying no ``fromlog`` means the completeness screen cannot run. The
  PAIR then writes nothing -- no rows, no state -- the run goes on to the next pair, and
  exits 3 at the end. Persisting a thousand rows as "judged" on a renamed field is the
  failure this prevents; halting 84 pairs over one boss is the failure the per-pair
  scope prevents.
- A row with ``fromlog`` but no ``killTime`` is REFUSED as ``no-kill-time`` and never
  reaches :func:`progresshours.pull_time`. Both repositories used to pass ``None``
  there, which silently switches screen 2 off while every number looks healthy.

## Budget

The point ceiling is against the ABSOLUTE hourly counter, read before every walk. On
the ceiling the run SLEEPS until ``pointsResetIn`` + 30 s and continues -- a public
runner's minutes are free -- until ``--deadline-minutes``; then it writes and exits 0.
A 429 or the deadline marks the encounter ``stoppedOnBudget`` so the skip matrix never
skips it. The response cache is OFF for every query here: a cached ranking page freezes
the order and blinds the cursor.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import progresshours
from .warcraftlogs import RateLimited, WarcraftLogsError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The zones the workflow sweeps, in priority order. Typed, never derived: the
#: backend's ``auto`` needs a table only its own probe writes, and ``--seasons N``
#: slides at every season turn. Zone 53 is the live tier; the rest are frozen.
DEFAULT_ZONES = (53, 46, 44, 42, 38)
#: Mythic first, on one budget.
DEFAULT_DIFFICULTIES = (progresshours.DIFFICULTY_MYTHIC, progresshours.DIFFICULTY_HEROIC)
DEFAULT_GUILDS = 200
DEFAULT_MAX_PAGES = 4
DEFAULT_RANKINGS_PAGES = progresshours.RANKING_MAX_PAGE
DEFAULT_POINT_CEILING = 0.6
DEFAULT_DEADLINE_MINUTES = 300
DEFAULT_REFRESH_AFTER_HOURS = 168
DEFAULT_REFRESH_AFTER_LIVE_HOURS = 6
DEFAULT_LIVE_WALL_REFRESH_AFTER_HOURS = 24
DEFAULT_WORKERS = 1

#: Read timeout for the sweep's client. The backend measured read timeouts above 60 s
#: on ``reports(guildID){fights}`` (execution 8x7rr), so 30 s -- the client's default
#: -- turned ordinary slow guilds into ``error`` rows.
SWEEP_TIMEOUT_SECONDS = 90.0

#: Seconds added to ``pointsResetIn`` before re-reading the counter after a ceiling.
RESET_SLACK_SECONDS = 30
#: What to wait when the service states no ``pointsResetIn`` at all: a whole window.
#: Guessing shorter would re-read a counter that has not moved.
FALLBACK_RESET_SECONDS = 3600

NIGHT_GAP_MS = progresshours.NIGHT_GAP_MS
RANKING_MAX_PAGE = progresshours.RANKING_MAX_PAGE

#: Refusal outcomes that ``--retry-errors`` re-attempts. Only this one: every other
#: outcome is a verdict about the guild, this one is a verdict about the network.
RETRYABLE_OUTCOME = "error"
OUTCOME_MEASURED = "measured"
OUTCOME_UNSCREENED = "ranking-row-unscreened"
OUTCOME_NO_KILL_TIME = "no-kill-time"

EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_ZONE_SKIPPED = 2
EXIT_SCHEMA_ALARM = 3


# ── ports from wtt-backend/apps/wclapi/services/progress/pullhours.py ───────────


def _text(value: object, limit: int = 200) -> str | None:
    """A non-empty stripped string, or None. Never raises on a surprise type.

    Numbers and booleans are refused rather than stringified: a realm slug that
    arrives as ``1`` is an id under a name this reader does not understand, and
    ``"1"`` stored as a slug would address no realm while looking configured.
    """
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def _region_of(block: object) -> str | None:
    """The region slug inside a server-ish block, or None.

    Warcraft Logs spells a region as a bare string in some payloads and as an object
    in others -- ``guildData.guild`` returns ``server { slug region { slug } }``, while
    character-ranking rows have been seen carrying ``server { region: "EU" }``. Both
    are read; anything else is left unrecorded.
    """
    if not isinstance(block, dict):
        return None
    region = block.get("region")
    if isinstance(region, str):
        return _text(region)
    if isinstance(region, dict):
        for key in ("slug", "compactName", "name"):
            found = _text(region.get(key))
            if found:
                return found
    return None


def guild_identity(row: object) -> tuple[str | None, str | None, str | None]:
    """``(guild name, server slug, server region)`` off one ranking row.

    **The exact shape of the guild block in a ``fightRankings(metric: progress)`` row
    is not established, and this reader is written so that not knowing it costs a
    NULL rather than an exception or a wrong value.** ``fightRankings`` is a JSON
    scalar -- no schema constrains it -- so the plausible spellings are tried in turn
    and anything unrecognised yields None. The sweep counts how many of a boss's rows
    carried a name (``named``) and records the first row's keys when none did
    (``shape``), which is the measurement that separates "the payload has no names"
    from "this reader missed them".

    Two things are deliberately NOT done, both for the same reason -- a wrong realm is
    worse than an unrecorded one, because it addresses a different realm's slice while
    looking configured:

    * a display name (``Tarren Mill``) is never slugified into ``tarren-mill``. Only a
      field literally named ``slug`` fills the server slug.
    * nothing is inferred from position, and a non-string is refused outright.

    Never raises. A row that is not a dict, a guild block that is a list, a name that
    arrives as a number: all of them are three Nones.
    """
    if not isinstance(row, dict):
        return None, None, None

    guild = row.get("guild")
    guild = guild if isinstance(guild, dict) else {}

    name = _text(guild.get("name")) or _text(row.get("guildName"))

    # The server may hang off the guild or off the row. Both spellings are read
    # because only a live payload can say which one this metric uses, and the guild's
    # own block is preferred: on a row that carries both, the guild's is the guild's.
    server = guild.get("server")
    if not isinstance(server, dict):
        server = row.get("server")
    server = server if isinstance(server, dict) else {}

    slug = _text(server.get("slug")) or _text(row.get("serverSlug"))
    region = (
        _region_of(server) or _region_of(guild) or _region_of(row) or _text(row.get("serverRegion"))
    )
    return name, slug, region


def describe_row_shape(rankings: object) -> str:
    """What the first row of a ranking scalar looks like, as one line.

    Recorded only when :func:`guild_identity` recognised NOTHING across a whole boss,
    the one case where "we stored no names" and "the payload has no names" are
    indistinguishable from outside. The keys cost nothing: the blob is in hand.
    """
    rows = rankings.get("rankings") if isinstance(rankings, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        guild = row.get("guild")
        inner = (
            f", guild {sorted(guild)}"
            if isinstance(guild, dict)
            else f", guild is {type(guild).__name__}"
        )
        return f"row {sorted(row)}{inner}"
    return "no rows"


# ── the cursor, ported from fetch_progress_hours.before_cursor ──────────────────


@dataclass(frozen=True)
class RankedRow:
    """One ranking row with the screen fields AND the guild's label.

    The screen fields are read by :func:`progresshours.ranking_rows` (shared with the
    chart producer, so the two cannot disagree about which field carries what); the
    label is read beside it by :func:`guild_identity`.
    """

    guild_id: int
    kill_time_ms: float | None
    from_log: bool | None
    guild_name: str | None = None
    server_slug: str | None = None
    server_region: str | None = None


def before_cursor(row: RankedRow, cursor: tuple[float, int | None] | None) -> bool:
    """Has this ranking row already been reached a verdict on?

    ``cursor`` is ``(kill_time_ms, guild_id)`` of the last row a previous sweep reached
    a verdict on, or None. The test is deliberately asymmetric:

    * **strictly earlier** kill time -> judged. The ranking is sorted by ``killTime``
      ascending, so every such row sits before the cursor row and the walk that
      reached the cursor row went past it.
    * **equal** kill time -> judged only if it IS the cursor row. Two guilds sharing a
      millisecond is not something this project has observed, and the cost of being
      wrong is asymmetric: re-attempting a guild costs one report walk, skipping one
      that was never looked at loses it.
    * **no kill time at all** -> never skipped. Such a row cannot be placed in the
      ranking's own order, so the cursor cannot speak for it.
    """
    if cursor is None or row.kill_time_ms is None:
        return False
    cursor_time, cursor_guild = cursor
    if row.kill_time_ms < cursor_time:
        return True
    return row.kill_time_ms == cursor_time and row.guild_id == cursor_guild


def parse_ranking_page(payload: dict) -> tuple[list[RankedRow], int, bool, object]:
    """``(rows with a guild id, rows without one, hasMorePages, the scalar)``.

    The screen fields come from :func:`progresshours.ranking_rows`; the labels are
    joined back onto them by guild id, which is the only key both readings share.
    """
    kills, without_guild = progresshours.ranking_rows(payload)
    scalar = ((payload.get("worldData") or {}).get("encounter") or {}).get("fightRankings") or {}
    raw_rows = scalar.get("rankings") if isinstance(scalar, dict) else None
    labels: dict[int, tuple[str | None, str | None, str | None]] = {}
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        guild_id = (raw.get("guild") or {}).get("id")
        if guild_id:
            labels.setdefault(int(guild_id), guild_identity(raw))
    rows = [
        RankedRow(
            kill.guild_id,
            kill.kill_time_ms,
            kill.from_log,
            *labels.get(kill.guild_id, (None, None, None)),
        )
        for kill in kills
    ]
    has_more = bool(scalar.get("hasMorePages")) if isinstance(scalar, dict) else False
    return rows, without_guild, has_more, scalar


@dataclass
class RankingWalk:
    """What one ranking walk found, and how it ended.

    Exactly one of the endings is true, and the skip matrix reads them apart:
    ``exhausted`` (``hasMorePages: false`` -- the only ending that means "no more
    guilds"), ``walled`` (page 20 read, more pages claimed, nothing fresh on it --
    the rank-1000 wall), ``cap_stopped`` (``--guilds`` filled up, so rows beyond
    were never seen and neither of the other two may be claimed).
    """

    fresh: list[RankedRow] = field(default_factory=list)
    without_guild: int = 0
    seen: int = 0
    already: int = 0
    behind: int = 0
    out_of_order: int = 0
    named: int = 0
    shape: str = ""
    unscreened: int = 0
    exhausted: bool = False
    walled: bool = False
    cap_stopped: bool = False
    pages_read: int = 0


def walk_ranking(
    fetch_page: Callable[[int], dict],
    *,
    done: set[int],
    cursor: tuple[float, int | None] | None,
    cap: int | None,
    max_page: int = RANKING_MAX_PAGE,
    only: set[int] | None = None,
) -> RankingWalk:
    """Ranking pages 1..``max_page``; which rows this run will ATTEMPT.

    ``cap`` counts rows this run will look at properly -- neither every ranked row nor
    guilds it will measure. Two skips run before the cap is counted against: a guild in
    ``done`` (a stored verdict) and a guild before the ``cursor`` (asked, and it
    produced none). Without the second a capped run spends its whole cap on the same
    permanently-refusing guilds every time and never reaches the ones behind them.

    ``only`` is ``--retry-errors``: exactly those guild ids are fresh, regardless of
    ``done`` and the cursor, and nothing else is.

    The walk never skips pages. The API offers no "start after this kill time", and a
    stored page number is the index cursor this design refuses: a removed row would
    step over a guild. Pages are cheap next to the report walks behind them.
    """
    walk = RankingWalk()
    previous_kill_time: float | None = None
    page = 1
    while page <= max_page:
        payload = fetch_page(page)
        walk.pages_read = page
        rows, missing, has_more, scalar = parse_ranking_page(payload)
        walk.without_guild += missing
        walk.seen += len(rows) + missing
        walk.named += sum(1 for row in rows if row.guild_name)
        if not walk.named and not walk.shape:
            walk.shape = describe_row_shape(scalar)
        for row in rows:
            if row.from_log is None:
                # The schema alarm. Counted here and acted on by the caller: the pair
                # writes nothing, because a renamed field would otherwise persist a
                # thousand rows as judged.
                walk.unscreened += 1
            # The cursor rests on the ranking being sorted by killTime ascending, which
            # nothing here has watched across two days. Count the counter-examples
            # rather than assume; behaviour is unchanged by the count.
            if row.kill_time_ms is not None:
                if previous_kill_time is not None and row.kill_time_ms < previous_kill_time:
                    walk.out_of_order += 1
                previous_kill_time = row.kill_time_ms
            if only is not None:
                if row.guild_id in only:
                    walk.fresh.append(row)
                continue
            if row.guild_id in done:
                walk.already += 1
            elif before_cursor(row, cursor):
                walk.behind += 1
            else:
                walk.fresh.append(row)
        if cap is not None and len(walk.fresh) >= cap:
            walk.fresh = walk.fresh[:cap]
            walk.cap_stopped = True
            return walk
        if not has_more:
            walk.exhausted = True
            return walk
        page += 1
    # Ran out of pages with more claimed. That is the wall only at the API's own
    # last page and only when the last page offered nothing new: a shorter
    # `--rankings-pages` proves nothing about rank 1000.
    if walk.pages_read == RANKING_MAX_PAGE and not walk.fresh:
        walk.walled = True
    return walk


# ── the per-guild walk: the unit a pool could call later ────────────────────────


@dataclass(frozen=True)
class Verdict:
    """One guild's answer: a row to append, or the refusal outcome and its evidence."""

    guild_id: int
    outcome: str
    row: dict | None = None
    reports_seen: int = 0
    kill_time_ms: float | None = None
    detail: str = ""


def night_pairs(attempts: Iterable[progresshours.Attempt]) -> list[list[int]]:
    """``[[first start, last end], ...]`` per raid night, on the absolute clock.

    The RAW INPUT the private import feeds to its own ``night_span_ms``: the partition
    is that function's -- a new night opens when a pull starts more than
    ``NIGHT_GAP_MS`` after the previous night's last END -- so the sum of the spans
    there equals what it would have computed from the attempts here. Nothing derived
    is written; the derivation stays private by the owner's decision.
    """
    pairs: list[list[int]] = []
    night_start: float | None = None
    night_end: float | None = None
    for attempt in attempts:
        if night_start is None or night_end is None:
            night_start, night_end = attempt.start_ms, attempt.end_ms
            continue
        if attempt.start_ms - night_end > NIGHT_GAP_MS:
            pairs.append([int(night_start), int(night_end)])
            night_start, night_end = attempt.start_ms, attempt.end_ms
            continue
        night_end = max(night_end, attempt.end_ms)
    if night_start is not None and night_end is not None:
        pairs.append([int(night_start), int(night_end)])
    return pairs


def attempts_to_kill(
    reports: list[dict], encounter_id: int, difficulty: int
) -> list[progresshours.Attempt]:
    """Every attempt up to and including the FIRST kill, oldest first.

    The same prefix :func:`progresshours.pull_time` sums over, recomputed rather than
    returned from it: that function is shared with the chart producer and its return
    type is not this module's to grow.
    """
    ordered, _dateless = progresshours.ordered_attempts(reports, encounter_id, difficulty)
    prefix: list[progresshours.Attempt] = []
    for attempt in ordered:
        prefix.append(attempt)
        if attempt.kill:
            break
    return prefix


def report_starts_ms(reports: Iterable[dict]) -> list[int]:
    """``startTime`` of every report the walk read, ascending, as ints.

    Every report, not only those holding a pull of this boss: the private
    ``logging_gap_ratio`` asks when the guild RAIDED in the zone.
    """
    return sorted(
        int(report["startTime"])
        for report in reports
        if isinstance(report.get("startTime"), (int, float))
    )


def walk_reports(
    client, guild_id: int, zone_id: int, encounter_id: int, difficulty: int, max_pages: int
) -> tuple[list[dict], bool]:
    """``(reports deduplicated on code, truncated)``. Every query uncached."""
    reports: list[dict] = []
    seen_codes: set[str] = set()
    truncated = True
    for page in range(1, max_pages + 1):
        payload = client.query(
            progresshours.GUILD_PULLS_QUERY,
            {"g": guild_id, "z": zone_id, "e": encounter_id, "d": difficulty, "page": page},
            label=f"pulls:{encounter_id}",
            cache=False,
        )
        listing = (payload.get("reportData") or {}).get("reports") or {}
        for report in listing.get("data") or []:
            # Keyed on the report code, because paging is not a snapshot: a listing
            # that shifts between two pages can hand back one report twice, and
            # `pull_time` would count its fights twice. A report with no code cannot
            # be deduplicated and is kept, since dropping it would lose real pulls.
            code = report.get("code")
            if code is not None:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
            reports.append(report)
        if not listing.get("has_more_pages"):
            truncated = False
            break
    return reports, truncated


def attempt_guild(
    client,
    *,
    zone_id: int,
    encounter_id: int,
    difficulty: int,
    row: RankedRow,
    max_pages: int,
    measured_at: str,
    run_id: str,
) -> Verdict:
    """One guild, start to verdict. Everything that spends a point on it is in here.

    Raises :class:`RateLimited` (a 429) upward: that is a run-level stop, not a
    guild-level finding. Every other :class:`WarcraftLogsError` -- timeouts included,
    after the client's httpx mapping -- is the ``error`` outcome: the guild is named
    in ``refused.jsonl`` and ``--retry-errors`` reaches it again.
    """
    # Screen 1, before any report query. `fromlog == 0` means Warcraft Logs holds no
    # log behind the first kill, so a report walk CANNOT find it and would find a
    # later one. Refusing here refunds the whole walk.
    if row.from_log is None:
        return Verdict(row.guild_id, OUTCOME_UNSCREENED, kill_time_ms=row.kill_time_ms)
    if not row.from_log:
        return Verdict(row.guild_id, "unlogged-kill", kill_time_ms=row.kill_time_ms)
    if row.kill_time_ms is None:
        # Never handed to `pull_time` as None: that disables screen 2 silently.
        return Verdict(row.guild_id, OUTCOME_NO_KILL_TIME)

    try:
        reports, truncated = walk_reports(
            client, row.guild_id, zone_id, encounter_id, difficulty, max_pages
        )
    except RateLimited:
        raise
    except WarcraftLogsError as exc:
        return Verdict(
            row.guild_id, RETRYABLE_OUTCOME, kill_time_ms=row.kill_time_ms, detail=str(exc)[:200]
        )
    if truncated:
        # The guild's FIRST kill may be older than anything fetched, so the window
        # cannot support the claim. Counted, never summed.
        return Verdict(
            row.guild_id, "truncated", reports_seen=len(reports), kill_time_ms=row.kill_time_ms
        )

    # Screen 2: the kill found must BE the ranked first kill.
    answer = progresshours.pull_time(reports, encounter_id, difficulty, row.kill_time_ms)
    if answer.ms is None:
        return Verdict(
            row.guild_id,
            answer.reason or "unknown",
            reports_seen=answer.reports,
            kill_time_ms=row.kill_time_ms,
        )
    prefix = attempts_to_kill(reports, encounter_id, difficulty)
    line = {
        "v": SCHEMA_VERSION,
        "zoneId": zone_id,
        "encounterId": encounter_id,
        "difficulty": difficulty,
        "guildId": row.guild_id,
        "guildName": row.guild_name,
        "serverSlug": row.server_slug,
        "serverRegion": row.server_region,
        # UNROUNDED: json writes the shortest round-trip form.
        "hours": answer.ms / progresshours.MS_PER_HOUR,
        "attempts": answer.attempts,
        "nightsObserved": answer.nights,
        "spanDays": answer.span_days,
        # The RANKING's kill time: the ranking defines the first kill, the log is one
        # recording of it. `killAtMs` beside it is the log's and is not imported.
        "firstKillAtMs": int(row.kill_time_ms),
        "killAtMs": int(answer.kill_at) if answer.kill_at is not None else None,
        "reportsSeen": answer.reports,
        "reportStartsMs": report_starts_ms(reports),
        "nightsMs": night_pairs(prefix),
        "measuredAt": measured_at,
        "run": run_id,
    }
    return Verdict(
        row.guild_id,
        OUTCOME_MEASURED,
        row=line,
        reports_seen=answer.reports,
        kill_time_ms=row.kill_time_ms,
    )


# ── files ───────────────────────────────────────────────────────────────────────


def pair_stem(zone_id: int, difficulty: int) -> str:
    return f"z{zone_id}-d{difficulty}"


def dumps_line(document: dict) -> str:
    """Compact, UTF-8, newline-terminated. The one spelling every writer here uses."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


def atomic_write(path: Path, text: str) -> None:
    """Temp file beside the target, then ``os.replace``: a reader sees old or new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_lines(path: Path) -> list[str]:
    """The file's ``\\n``-terminated lines, verbatim, or none when it does not exist.

    Verbatim, so a rewrite that appends reproduces every existing byte: the
    append-only promise is byte-level, not JSON-level.
    """
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if not text:
        return []
    parts = text.split("\n")
    tail = parts.pop()  # "" when the file is newline-terminated
    lines = [part + "\n" for part in parts]
    if tail:
        lines.append(tail)
    return lines


@dataclass
class PairData:
    """One ``(zone, difficulty)`` file set, loaded.

    ``rows``/``refused`` are deduplicated on ``(encounterId, guildId)``, last line wins,
    and the duplicates are counted rather than hidden: a duplicate is a fact about a
    past run, not something to repair on the way past. The file LINES are kept as
    read, because the file is append-only at the byte level.
    """

    zone_id: int
    difficulty: int
    rows_path: Path
    refused_path: Path
    state_path: Path
    row_lines: list[str] = field(default_factory=list)
    refused_lines: list[str] = field(default_factory=list)
    rows: dict[tuple[int, int], dict] = field(default_factory=dict)
    refused: dict[tuple[int, int], dict] = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    duplicate_rows: int = 0
    duplicate_refused: int = 0
    unreadable_lines: int = 0

    def done(self, encounter_id: int) -> set[int]:
        """Guild ids with a stored verdict on this encounter: rows ∪ refused."""
        return {g for (e, g) in self.rows if e == encounter_id} | {
            g for (e, g) in self.refused if e == encounter_id
        }

    def error_guilds(self, encounter_id: int) -> set[int]:
        """Guilds whose LAST refusal on this encounter is ``error`` and who have no row."""
        return {
            g
            for (e, g), line in self.refused.items()
            if e == encounter_id
            and line.get("outcome") == RETRYABLE_OUTCOME
            and (e, g) not in self.rows
        }

    def encounter_state(self, encounter_id: int) -> dict | None:
        return (self.state.get("encounters") or {}).get(str(encounter_id))

    def cursor(self, encounter_id: int) -> tuple[float, int | None] | None:
        entry = self.encounter_state(encounter_id)
        if not entry or entry.get("cursorKillTimeMs") is None:
            return None
        return float(entry["cursorKillTimeMs"]), entry.get("cursorGuildId")


def _index_lines(lines: list[str]) -> tuple[dict[tuple[int, int], dict], int, int]:
    keyed: dict[tuple[int, int], dict] = {}
    duplicates = 0
    unreadable = 0
    for line in lines:
        try:
            document = json.loads(line)
            key = (int(document["encounterId"]), int(document["guildId"]))
        except (ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        if key in keyed:
            duplicates += 1
        keyed[key] = document
    return keyed, duplicates, unreadable


def load_pair(out_dir: Path, zone_id: int, difficulty: int) -> PairData:
    stem = pair_stem(zone_id, difficulty)
    pair = PairData(
        zone_id,
        difficulty,
        out_dir / f"{stem}.rows.jsonl",
        out_dir / f"{stem}.refused.jsonl",
        out_dir / f"{stem}.state.json",
    )
    pair.row_lines = read_lines(pair.rows_path)
    pair.refused_lines = read_lines(pair.refused_path)
    pair.rows, pair.duplicate_rows, bad_rows = _index_lines(pair.row_lines)
    pair.refused, pair.duplicate_refused, bad_refused = _index_lines(pair.refused_lines)
    pair.unreadable_lines = bad_rows + bad_refused
    if pair.state_path.is_file():
        pair.state = json.loads(pair.state_path.read_text(encoding="utf-8"))
    else:
        pair.state = {
            "v": SCHEMA_VERSION,
            "zoneId": zone_id,
            "zoneName": None,
            "frozen": None,
            "difficulty": difficulty,
            "encounters": {},
        }
    return pair


def _last_swept_at(state: dict) -> str | None:
    stamps = [
        entry.get("sweptAt")
        for entry in (state.get("encounters") or {}).values()
        if isinstance(entry, dict) and entry.get("sweptAt")
    ]
    return max(stamps) if stamps else None


def build_manifest(out_dir: Path) -> dict:
    """The manifest is DERIVED from the directory, never accumulated, and carries no
    run timestamp: a run that changes nothing must not change it."""
    files = []
    for state_path in sorted(out_dir.glob("z*-d*.state.json")):
        stem = state_path.name[: -len(".state.json")]
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
        files.append(
            {
                "path": f"{stem}.rows.jsonl",
                "zoneId": state.get("zoneId"),
                "difficulty": state.get("difficulty"),
                "rows": len(read_lines(out_dir / f"{stem}.rows.jsonl")),
                "refused": len(read_lines(out_dir / f"{stem}.refused.jsonl")),
                "lastSweptAt": _last_swept_at(state),
            }
        )
    return {"v": SCHEMA_VERSION, "files": files}


README_TEXT = """# progress-cohort

Progress hours per guild per boss -- the sum of every attempt up to and including
the guild's first kill -- for every guild a boss's Warcraft Logs progress ranking
lists, up to the API's rank-1000 wall.

Written by `wowdps progress-sweep` in `Wild-Things-Tools/wow-dps-breakdown` (GitHub
Actions), read by `manage.py import_progress_hours` in `wtt-backend`. The contract is
`docs/progress-cohort.md` in the writer's repository.

- `z<zone>-d<difficulty>.rows.jsonl`: one MEASURED guild per line, append-only.
- `z<zone>-d<difficulty>.refused.jsonl`: one judged-but-unmeasured guild per line
  (ids and outcomes, no names), append-only.
- `z<zone>-d<difficulty>.state.json`: the sweep's cursor per encounter, rewritten
  atomically after every encounter.
- `manifest.json`: per file set, line counts and the newest `sweptAt`. No run
  timestamp, so a run that changes nothing changes nothing.

Every `hours` figure is a LOWER BOUND: a raid night nobody uploaded is invisible to
every reader of the Warcraft Logs API. Rows carry the raw inputs (`reportStartsMs`,
`nightsMs`); the derived disclosures are computed on the private side.
"""


def write_pair(pair: PairData, out_dir: Path, new_rows: list[str], new_refused: list[str]) -> None:
    """Append the encounter's lines, rewrite state and manifest -- each atomically.

    Rows and refusals are only touched when there is something to append, so a run
    that measures nothing leaves them byte-identical. The whole file is rewritten
    through a temp file because that is what makes the append atomic; the bytes
    before the appended lines are the bytes that were read.
    """
    if new_rows:
        pair.row_lines.extend(new_rows)
        atomic_write(pair.rows_path, "".join(pair.row_lines))
    if new_refused:
        pair.refused_lines.extend(new_refused)
        atomic_write(pair.refused_path, "".join(pair.refused_lines))
    atomic_write(pair.state_path, json.dumps(pair.state, indent=1, ensure_ascii=False) + "\n")
    readme = out_dir / "README.md"
    if not readme.is_file():
        atomic_write(readme, README_TEXT)
    atomic_write(
        out_dir / "manifest.json",
        json.dumps(build_manifest(out_dir), indent=1, ensure_ascii=False) + "\n",
    )


# ── the skip matrix ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Cadence:
    refresh_after_hours: float = DEFAULT_REFRESH_AFTER_HOURS
    refresh_after_live_hours: float = DEFAULT_REFRESH_AFTER_LIVE_HOURS
    live_wall_refresh_after_hours: float = DEFAULT_LIVE_WALL_REFRESH_AFTER_HOURS


def _age_hours(swept_at: object, now: datetime) -> float | None:
    if not isinstance(swept_at, str):
        return None
    try:
        then = datetime.fromisoformat(swept_at)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (now - then).total_seconds() / 3600.0


def skip_reason(entry: dict | None, *, frozen: bool, now: datetime, cadence: Cadence) -> str | None:
    """Why this encounter needs no points spent on it this run, or None.

    ``stoppedOnBudget`` or ``attempted > 0`` last time -> never skipped: the first
    was cut off, the second is still yielding. Otherwise a FROZEN zone's encounter is
    skipped once its walk was exhausted or walled, for ``refresh_after`` hours (the
    safety net for a removed log letting a row move up); a LIVE zone's encounter for
    ``refresh_after_live`` hours, or ``live_wall_refresh_after`` when it is walled,
    because behind page 20 nothing new can appear. A skip too many costs latency,
    never a guild.
    """
    if not entry:
        return None
    if entry.get("stoppedOnBudget") or (entry.get("attempted") or 0) > 0:
        return None
    age = _age_hours(entry.get("sweptAt"), now)
    if age is None:
        return None
    exhausted, walled = bool(entry.get("rankingExhausted")), bool(entry.get("walled"))
    if frozen:
        if not (exhausted or walled):
            return None
        if age < cadence.refresh_after_hours:
            ending = "exhausted" if exhausted else "walled"
            return f"frozen zone, ranking {ending} {age:.1f} h ago, nothing attempted"
        return None
    if walled:
        if age < cadence.live_wall_refresh_after_hours:
            return f"live zone at the rank-1000 wall {age:.1f} h ago, nothing attempted"
        return None
    if age < cadence.refresh_after_live_hours:
        return f"live zone walked {age:.1f} h ago, nothing attempted"
    return None


# ── budget: the absolute counter, the ceiling, the sleep, the deadline ──────────


class DeadlineReached(RuntimeError):
    """The wall-clock budget is spent. Write what is measured and stop."""


@dataclass
class Budget:
    """The ceiling against the ABSOLUTE hourly counter, and what to do at it.

    ``check`` is called before every walk. At the ceiling it sleeps until the service
    says the counter resets (plus slack) and reads again -- the runner's minutes are
    free -- and gives up only when the deadline would pass first, which it reports as
    :class:`DeadlineReached`. ``sleep`` and ``clock`` are injectable so a test can
    drive it in no time at all.
    """

    client: object
    ceiling: float
    deadline_seconds: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    started: float | None = None
    sleeps: int = 0
    slept_seconds: float = 0.0
    readings: int = 0

    def __post_init__(self) -> None:
        if self.started is None:
            self.started = self.clock()

    def remaining_seconds(self) -> float:
        assert self.started is not None
        return self.deadline_seconds - (self.clock() - self.started)

    def check(self) -> None:
        while True:
            if self.remaining_seconds() <= 0:
                raise DeadlineReached("deadline reached")
            reading = self.client.rate_limit()
            self.readings += 1
            limit = float(reading.get("limitPerHour") or 0)
            spent = float(reading.get("pointsSpentThisHour") or 0)
            if not limit or spent < limit * self.ceiling:
                return
            reset = reading.get("pointsResetIn")
            wait = (
                float(reset) if isinstance(reset, (int, float)) else FALLBACK_RESET_SECONDS
            ) + RESET_SLACK_SECONDS
            if wait >= self.remaining_seconds():
                raise DeadlineReached(
                    f"point ceiling reached ({spent:.0f} of {limit:.0f}) and the counter "
                    f"resets in {wait:.0f} s, past the deadline"
                )
            log.warning(
                "point ceiling reached (%.0f of %.0f); sleeping %.0f s until the counter resets",
                spent,
                limit,
                wait,
            )
            self.sleeps += 1
            self.slept_seconds += wait
            self.sleep(wait)


# ── the runner ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepOptions:
    zones: tuple[int, ...]
    difficulties: tuple[int, ...] = DEFAULT_DIFFICULTIES
    guilds: int = DEFAULT_GUILDS
    max_pages: int = DEFAULT_MAX_PAGES
    rankings_pages: int = DEFAULT_RANKINGS_PAGES
    point_ceiling: float = DEFAULT_POINT_CEILING
    deadline_minutes: float = DEFAULT_DEADLINE_MINUTES
    cadence: Cadence = Cadence()
    retry_errors: bool = False
    workers: int = DEFAULT_WORKERS


@dataclass
class SweepReport:
    exit_code: int = EXIT_OK
    new_rows: int = 0
    new_refused: int = 0
    attempted: int = 0
    zones_skipped: list[int] = field(default_factory=list)
    alarmed_pairs: list[str] = field(default_factory=list)
    stopped: str | None = None
    ranking_errors: int = 0
    sleeps: int = 0
    slept_seconds: float = 0.0
    points_first: float | None = None
    points_last: float | None = None
    queries: int = 0

    def to_json(self) -> dict:
        spent = (
            round(self.points_last - self.points_first, 1)
            if self.points_first is not None
            and self.points_last is not None
            and self.points_last >= self.points_first
            else None
        )
        return {
            "exitCode": self.exit_code,
            "newRows": self.new_rows,
            "newRefused": self.new_refused,
            "attempted": self.attempted,
            "zonesSkipped": self.zones_skipped,
            "alarmedPairs": self.alarmed_pairs,
            "stopped": self.stopped,
            "rankingErrors": self.ranking_errors,
            "sleeps": self.sleeps,
            "sleptSeconds": self.slept_seconds,
            # None is UNMEASURED, never zero: a counter that did not move is the
            # absence of a measurement.
            "points": spent,
            "queries": self.queries,
        }


class _PairAlarm(Exception):
    """A ranking row carried no ``fromlog``; this pair writes nothing."""


class _StopRun(Exception):
    """Budget or deadline: write what is measured, stop the run, exit 0."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _zone_encounters(client, zone_id: int) -> tuple[dict | None, str]:
    try:
        zone = client.zone(zone_id, cache=False)
    except RateLimited:
        raise
    except WarcraftLogsError as exc:
        return None, f"zone {zone_id}: {exc}"
    if not isinstance(zone, dict):
        return None, f"zone {zone_id}: Warcraft Logs returns no such zone"
    encounters = [
        e
        for e in zone.get("encounters") or []
        if isinstance(e, dict) and isinstance(e.get("id"), int)
    ]
    if not encounters:
        return None, f"zone {zone_id} ({zone.get('name')}): no encounters listed"
    return {
        "name": zone.get("name"),
        "frozen": bool(zone.get("frozen")),
        "encounters": encounters,
    }, ""


def _refusal_line(verdict: Verdict, encounter_id: int, at: str, run_id: str) -> str:
    line = {
        "v": SCHEMA_VERSION,
        "encounterId": encounter_id,
        "guildId": verdict.guild_id,
        "outcome": verdict.outcome,
        "killTimeMs": int(verdict.kill_time_ms) if verdict.kill_time_ms is not None else None,
        "reportsSeen": verdict.reports_seen,
        "at": at,
        "run": run_id,
    }
    return dumps_line(line)


def run_sweep(
    client,
    options: SweepOptions,
    out_dir: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    run_id: str | None = None,
) -> SweepReport:
    """Every pair, every encounter, in the order given. See the module docstring.

    Returns the report; its ``exit_code`` is 3 when any pair alarmed, 2 when any zone
    could not be listed, 0 otherwise -- a budget or deadline stop is 0, because what
    was measured is written and the next run continues from the cursor.
    """
    now = now or (lambda: datetime.now(UTC))
    run_id = run_id or os.environ.get("GITHUB_RUN_ID") or "local"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    measured_at = now().isoformat(timespec="seconds")
    report = SweepReport()
    budget = Budget(
        client, options.point_ceiling, options.deadline_minutes * 60.0, sleep=sleep, clock=clock
    )
    ledger = getattr(client, "ledger", None)

    def note_points() -> None:
        if ledger is None:
            return
        report.points_first = getattr(ledger, "first_reading", None)
        report.points_last = getattr(ledger, "last_reading", None)
        entries = getattr(ledger, "entries", None)
        report.queries = len(entries) if isinstance(entries, list) else 0
        report.sleeps, report.slept_seconds = budget.sleeps, budget.slept_seconds

    try:
        try:
            budget.check()
        except (DeadlineReached, WarcraftLogsError) as exc:
            raise _StopRun(f"before the first walk: {exc}") from None
        for zone_id in options.zones:
            zone, why = _zone_encounters(client, zone_id)
            if zone is None:
                log.warning("%s; skipping the zone", why)
                report.zones_skipped.append(zone_id)
                continue
            for difficulty in options.difficulties:
                pair = load_pair(out_dir, zone_id, difficulty)
                pair.state["zoneName"] = zone["name"]
                pair.state["frozen"] = zone["frozen"]
                if pair.duplicate_rows or pair.duplicate_refused or pair.unreadable_lines:
                    log.warning(
                        "%s: %d duplicate row(s), %d duplicate refusal(s), %d unreadable line(s)",
                        pair_stem(zone_id, difficulty),
                        pair.duplicate_rows,
                        pair.duplicate_refused,
                        pair.unreadable_lines,
                    )
                try:
                    for encounter in zone["encounters"]:
                        _sweep_encounter(
                            client,
                            options,
                            pair,
                            out_dir,
                            zone,
                            encounter,
                            budget,
                            report,
                            measured_at,
                            run_id,
                            now,
                        )
                except _PairAlarm as alarm:
                    stem = pair_stem(zone_id, difficulty)
                    log.error("%s: %s; nothing written for this pair", stem, alarm)
                    report.alarmed_pairs.append(stem)
    except _StopRun as stop:
        log.warning("stopped: %s", stop.reason)
        report.stopped = stop.reason
    note_points()
    if report.alarmed_pairs:
        report.exit_code = EXIT_SCHEMA_ALARM
    elif report.zones_skipped:
        report.exit_code = EXIT_ZONE_SKIPPED
    return report


def _sweep_encounter(
    client,
    options: SweepOptions,
    pair: PairData,
    out_dir: Path,
    zone: dict,
    encounter: dict,
    budget: Budget,
    report: SweepReport,
    measured_at: str,
    run_id: str,
    now: Callable[[], datetime],
) -> None:
    encounter_id = int(encounter["id"])
    name = encounter.get("name") or str(encounter_id)
    stem = pair_stem(pair.zone_id, pair.difficulty)
    entry = pair.encounter_state(encounter_id)
    only: set[int] | None = None
    if options.retry_errors:
        only = pair.error_guilds(encounter_id)
        if not only:
            log.info("%s %s: no error refusal to retry", stem, name)
            return
    else:
        reason = skip_reason(entry, frozen=zone["frozen"], now=now(), cadence=options.cadence)
        if reason:
            log.info("%s %s: skipped, %s", stem, name, reason)
            return

    try:
        budget.check()
    except DeadlineReached as exc:
        raise _StopRun(str(exc)) from None
    except WarcraftLogsError as exc:
        raise _StopRun(f"budget reading failed: {exc}") from None

    def fetch_page(page: int) -> dict:
        return client.query(
            progresshours.PROGRESS_RANKINGS_QUERY,
            {"e": encounter_id, "d": pair.difficulty, "p": page},
            label=f"progress:{encounter_id}:p{page}",
            cache=False,
        )

    try:
        walk = walk_ranking(
            fetch_page,
            done=pair.done(encounter_id),
            cursor=pair.cursor(encounter_id),
            cap=options.guilds or None,
            max_page=max(1, min(options.rankings_pages, RANKING_MAX_PAGE)),
            only=only,
        )
    except RateLimited as exc:
        raise _StopRun(f"rate limited on a ranking page: {exc}") from None
    except WarcraftLogsError as exc:
        log.warning("%s %s: ranking walk failed: %s", stem, name, exc)
        report.ranking_errors += 1
        return
    if walk.unscreened:
        raise _PairAlarm(
            f"{walk.unscreened} ranking row(s) of {name} carry no fromlog ({OUTCOME_UNSCREENED})"
        )

    outcomes: dict[str, int] = {}
    new_rows: list[str] = []
    new_refused: list[str] = []
    cursor: tuple[float, int] | None = None
    stopped: str | None = None
    for row in walk.fresh:
        try:
            budget.check()
            verdict = attempt_guild(
                client,
                zone_id=pair.zone_id,
                encounter_id=encounter_id,
                difficulty=pair.difficulty,
                row=row,
                max_pages=options.max_pages,
                measured_at=measured_at,
                run_id=run_id,
            )
        except DeadlineReached as exc:
            stopped = str(exc)
            break
        except RateLimited as exc:
            stopped = f"rate limited: {exc}"
            break
        except WarcraftLogsError as exc:
            # Only `budget.check()` can raise this here: `attempt_guild` turns its
            # own into the `error` verdict. A counter that cannot be read is a
            # budget that cannot be honoured, so it stops the run like the ceiling.
            stopped = f"budget reading failed: {exc}"
            break
        outcomes[verdict.outcome] = outcomes.get(verdict.outcome, 0) + 1
        report.attempted += 1
        if verdict.row is not None:
            new_rows.append(dumps_line(verdict.row))
            pair.rows[(encounter_id, verdict.guild_id)] = verdict.row
        else:
            new_refused.append(_refusal_line(verdict, encounter_id, measured_at, run_id))
            pair.refused[(encounter_id, verdict.guild_id)] = {"outcome": verdict.outcome}
            if verdict.detail:
                log.warning("%s %s guild %s: %s", stem, name, verdict.guild_id, verdict.detail)
        # Only now: a row the budget interrupted was paid for and not answered, so
        # advancing over it would lose the guild -- and a row stating no killTime
        # cannot be a boundary at all.
        if row.kill_time_ms is not None:
            cursor = (row.kill_time_ms, verdict.guild_id)

    previous = pair.cursor(encounter_id)
    if only is not None:
        # A retry re-attempts guilds BEHIND the cursor; it must not move it.
        cursor = None
    if cursor is not None and previous is not None and cursor[0] < previous[0]:
        # Forward only. Reachable only when the ranking's order moved under the
        # walk, which `outOfOrder` counts; a cursor that went backwards would re-open
        # every guild between, which costs re-attempts and never a guild.
        cursor = None
    attempted = sum(outcomes.values())
    if only is not None:
        # A retry walks the pages for its own guilds only, so what it saw of the
        # ranking's END is not a finding about the ranking; the last full walk's is.
        exhausted = bool(entry and entry.get("rankingExhausted"))
        walled = bool(entry and entry.get("walled"))
    else:
        exhausted, walled = walk.exhausted, walk.walled
    entry = {
        "name": name,
        "cursorKillTimeMs": int(cursor[0]) if cursor else (int(previous[0]) if previous else None),
        "cursorGuildId": cursor[1] if cursor else (previous[1] if previous else None),
        "rankingExhausted": exhausted,
        "walled": walled,
        "stoppedOnBudget": stopped is not None,
        "sweptAt": measured_at,
        "guildsSeen": walk.seen,
        "withoutGuild": walk.without_guild,
        "attempted": attempted,
        "named": walk.named,
        "shape": walk.shape,
        "outOfOrder": walk.out_of_order,
        "outcomes": dict(sorted(outcomes.items())),
    }
    pair.state.setdefault("encounters", {})[str(encounter_id)] = entry
    write_pair(pair, out_dir, new_rows, new_refused)
    report.new_rows += len(new_rows)
    report.new_refused += len(new_refused)
    log.info(
        "%s %s: %d attempted, %d measured, seen %d, %s%s",
        stem,
        name,
        attempted,
        outcomes.get(OUTCOME_MEASURED, 0),
        walk.seen,
        "exhausted" if walk.exhausted else "walled" if walk.walled else "open",
        f"; STOPPED ({stopped})" if stopped else "",
    )
    if stopped:
        raise _StopRun(stopped)


# ── --seed-only and --validate ──────────────────────────────────────────────────


def describe_pairs(out_dir: Path, zones: Iterable[int], difficulties: Iterable[int]) -> list[str]:
    """One line per pair: rows, refusals, cursor and wall per encounter. No query."""
    lines = []
    for zone_id in zones:
        for difficulty in difficulties:
            pair = load_pair(Path(out_dir), zone_id, difficulty)
            stem = pair_stem(zone_id, difficulty)
            lines.append(
                f"{stem}: {len(pair.row_lines)} row(s), {len(pair.refused_lines)} refusal(s), "
                f"{len(pair.state.get('encounters') or {})} encounter state(s)"
                + (f", {pair.duplicate_rows} duplicate row(s)" if pair.duplicate_rows else "")
            )
            for encounter_id, entry in sorted((pair.state.get("encounters") or {}).items()):
                lines.append(
                    f"  {encounter_id} {entry.get('name')}: cursor "
                    f"{entry.get('cursorKillTimeMs')}/{entry.get('cursorGuildId')}, "
                    f"exhausted {entry.get('rankingExhausted')}, walled {entry.get('walled')}, "
                    f"stoppedOnBudget {entry.get('stoppedOnBudget')}, "
                    f"attempted {entry.get('attempted')}, swept {entry.get('sweptAt')}"
                    + (" (seed)" if entry.get("seed") else "")
                )
    return lines


def _git_head_line_count(directory: Path, path: Path) -> int | None:
    """Lines of ``path`` at HEAD, or None when git cannot say (untracked, no repo)."""
    try:
        top = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        relative = path.resolve().relative_to(Path(top).resolve()).as_posix()
        shown = subprocess.run(
            ["git", "-C", top, "show", f"HEAD:{relative}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None
    return shown.count("\n")


def validate(out_dir: Path) -> list[str]:
    """Every violation, as a sentence. Empty means the directory may be committed.

    Three claims, each refused rather than repaired: every line of every jsonl file
    is JSON, the manifest's counts equal the files' line counts, and no tracked
    rows/refused file is SHORTER than it is at HEAD -- append-only is the promise the
    private import's per-file digest rests on.
    """
    out_dir = Path(out_dir)
    problems: list[str] = []
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"{manifest_path.name} is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{manifest_path.name} is not JSON: {exc}"]
    listed = {entry.get("path"): entry for entry in manifest.get("files") or []}

    for path in sorted(out_dir.glob("z*-d*.jsonl")):
        text = path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            problems.append(f"{path.name}: last line is not newline-terminated")
        lines = read_lines(path)
        for number, line in enumerate(lines, start=1):
            try:
                json.loads(line)
            except ValueError:
                problems.append(f"{path.name}:{number}: not JSON")
        stem, kind = path.name.rsplit(".", 2)[0], path.name.rsplit(".", 2)[1]
        entry = listed.get(f"{stem}.rows.jsonl")
        if entry is None:
            problems.append(f"{path.name}: not in manifest.json")
        elif entry.get(kind) != len(lines):
            problems.append(
                f"{path.name}: manifest says {entry.get(kind)} line(s), file holds {len(lines)}"
            )
        at_head = _git_head_line_count(out_dir, path)
        if at_head is not None and len(lines) < at_head:
            problems.append(f"{path.name}: {len(lines)} line(s), shorter than HEAD's {at_head}")
    for path in sorted(out_dir.glob("z*-d*.state.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: not JSON: {exc}")
    for listed_path in listed:
        if listed_path and not (out_dir / listed_path).is_file():
            stem = str(listed_path)[: -len(".rows.jsonl")]
            if not (out_dir / f"{stem}.state.json").is_file():
                problems.append(f"manifest lists {listed_path} but no such file set exists")
    return problems
