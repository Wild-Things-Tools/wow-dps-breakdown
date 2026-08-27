"""Progress hours per boss: how long guilds fought it before it died.

The owner asked for a chart of *progress hours* -- medians per boss, stacked per
season, seasons side by side -- so that "season 1's first five bosses cost as much
progress time as season 2's first three" can be read off it.

## The metric, and the two cheaper things it is not

**Progress time is the sum of every attempt up to and including the first kill.**

It is NOT ``duration`` on a progress ranking row: that is the length of the one
pull that killed the boss, minutes rather than hours, and it is ``0`` whenever the
kill did not come from a log. It is also not calendar days from tier open, which is
a different question with a different answer.

Confirmed against the live schema on 2026-08-26 (``wowdps wcl-schema``, 7 queries):
``ReportData.reports`` takes ``guildID``/``zoneID``/``limit``/``page``, and
``ReportFight`` carries ``startTime``, ``endTime``, ``kill``, ``encounterID`` and
``difficulty``. ``fights`` is selectable INSIDE the reports listing, which is the
whole affordability argument -- one query per guild and page rather than one per
report.

## Two traps in the payload, both load-bearing

**``ReportFight.startTime`` is relative to its REPORT, not the epoch.** Reports are
ordered by their own absolute ``Report.startTime``; fights keep their within-report
order. Sorting every fight together on its own ``startTime`` interleaves raid nights
and moves where the first kill falls -- **while the total still looks plausible**,
which is what makes it dangerous rather than merely wrong.

**A window with no kill cannot be summed.** Every fetched report being a wipe means
either the guild killed it later or never did, and those are indistinguishable from
here. A partial sum published as the answer is a floor wearing a measurement's
clothes, so it is refused and counted instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Warcraft Logs difficulty ids.
DIFFICULTY_NORMAL = 3
DIFFICULTY_HEROIC = 4
DIFFICULTY_MYTHIC = 5
DIFFICULTY_NAMES = {
    DIFFICULTY_NORMAL: "Normal",
    DIFFICULTY_HEROIC: "Heroic",
    DIFFICULTY_MYTHIC: "Mythic",
}

MS_PER_HOUR = 3_600_000.0

#: How far a logged kill may sit from the ranked kill and still be the same kill.
#:
#: Generous on purpose. Warcraft Logs states `killTime` on a progress ranking row and
#: does not say whether it is the kill fight's start or its end, and a long pull is
#: minutes; the failures this exists to catch are *hours to weeks* late, because they
#: are a different kill entirely. A tight tolerance would refuse real matches to buy
#: precision the screen does not need.
KILL_MATCH_TOLERANCE_MS = 1_800_000

#: The gap that separates two raid nights.
#:
#: Six hours, and it partitions what was **observed** -- it never invents a night. A
#: raid night is a few hours with short breaks; six hours is longer than any break
#: inside one and shorter than the gap to the next day.
NIGHT_GAP_MS = 21_600_000

#: What one page of `fightRankings(metric: progress)` holds, and how deep paging goes.
#:
#: **Both are the API's, not ours**, and both are read from the sibling `wtt-backend`,
#: which runs the same `fightRankings(metric: progress, ...)` query live and records
#: them as `PAGE_SIZE = 50` / `MAX_PAGE = 20` -- at most 1000 rows per filter
#: combination. Its query is additionally server-scoped where this one is not, so
#: treat the first run that pages as a schema check rather than as a settled fact.
RANKING_PAGE_SIZE = 50
RANKING_MAX_PAGE = 20

#: Which raid an encounter belongs to.
#:
#: **The zone is derived, never typed.** `fight_profiles.json` carries encounter ids
#: and no `zoneId` for any tier, so `block.get("zoneId") or args.zone or 0` resolved
#: to **0** on every query of the 2026-08-26 run -- and Warcraft Logs accepted it
#: rather than refusing, returning each guild's reports across *all* content. That is
#: this project's own recurring trap in its third disguise: after an omitted argument
#: (`hostilityType`, `includeResources`) comes a *zero* one, and a zero is a value the
#: service is entitled to interpret.
#:
#: A zone is a property of the encounter, and the encounter ids are the one thing the
#: tier file is authoritative about, so it is asked for rather than asserted. An
#: encounter whose zone cannot be resolved is REFUSED, because `zoneID: 0` is not a
#: narrower question -- it is a different one, answered plausibly.
ENCOUNTER_ZONE_QUERY = """query($e:Int!){
  worldData { encounter(id:$e) { id name zone { id name } } }
}"""


#: One guild's first kill, from `fightRankings(metric: progress)`.
PROGRESS_RANKINGS_QUERY = """query($e:Int!,$d:Int!,$p:Int!){
  worldData { encounter(id:$e) { fightRankings(metric: progress, difficulty:$d, page:$p) } }
}"""

#: How many reports one page asks for.
#:
#: **Not 100, and it is a LITERAL in the query rather than a variable.** Selecting
#: `fights` inside the reports listing -- the thing that makes this affordable --
#: pushes the document over Warcraft Logs' complexity ceiling at 100:
#:
#:     Max query complexity should be 50000 but got 50401.
#:
#: Measured live on 2026-08-26 (runs 32976978343 and 32977917321): 12 of 12 guilds
#: refused on every one of MID1's nine bosses, with the pass exiting 0 both times.
#:
#: The literal matters because a static complexity analyser cannot see a variable's
#: runtime value and must assume the field's maximum. Passing 50 as `$limit` would
#: therefore be scored as 100 and refused exactly as before. Baking it into the
#: query text is the only form the analyser can actually read.
REPORTS_PER_PAGE = 50

#: One page of a guild's reports, each carrying its attempts on one boss.
GUILD_PULLS_QUERY = f"""query($g:Int!,$z:Int!,$e:Int!,$d:Int!,$page:Int!){{
  reportData {{ reports(guildID:$g, zoneID:$z, limit:{REPORTS_PER_PAGE}, page:$page) {{
    has_more_pages
    data {{ code startTime fights(encounterID:$e, difficulty:$d) {{
      startTime endTime kill encounterID difficulty
    }} }}
  }} }}
}}"""


def encounter_zone(payload: dict) -> int | None:
    """The zone id in an ``ENCOUNTER_ZONE_QUERY`` payload, or None.

    None rather than 0: the caller must be able to tell "no zone" from a zone whose
    id happens to be falsy, because 0 is exactly the value that made the failing run
    look like a working one.
    """
    encounter = ((payload.get("worldData") or {}).get("encounter")) or {}
    zone = encounter.get("zone") or {}
    zone_id = zone.get("id")
    if not isinstance(zone_id, int) or zone_id <= 0:
        return None
    return zone_id


@dataclass(frozen=True)
class RankedKill:
    """One row of `fightRankings(metric: progress)`: whose kill, when, and whether logged.

    The progress metric returns **exactly one row per guild**, sorted by `killTime`
    ascending, so a row *is* that guild's first kill. Two of its three fields were
    fetched and thrown away until 2026-08-27, and they are the whole completeness
    screen:

    - `from_log` -- 1 when Warcraft Logs holds a log behind the kill, 0 when the kill
      came from Blizzard's own records instead. A `0` guild's first kill **cannot** be
      in any report walk, so the walk necessarily finds a later kill and calls it the
      first one.
    - `kill_time_ms` -- the kill the ranking is about. A logged kill far from it is a
      different kill.

    `from_log` is `None` when the row states neither, which the caller must treat as a
    refusal rather than a pass: a renamed field would otherwise switch both screens off
    while every published number still looked healthy.

    Field names and the 21.0% `fromlog == 0` share are read from `wtt-backend`'s
    committed live slice (Ulgrax, Normal, EU/Tarren Mill, 405 rows, 2026-08-20), not
    from a run of this code.
    """

    guild_id: int
    kill_time_ms: float | None
    from_log: bool | None


def ranking_rows(payload: dict) -> tuple[list[RankedKill], int]:
    """Ranked first kills, plus the count of rows carrying no guild id.

    `guild.id` is null on roughly 4% of rows (mostly CN), which is why the count is
    returned rather than the rows being silently shorter.
    """
    rankings = ((payload.get("worldData") or {}).get("encounter") or {}).get("fightRankings") or {}
    rows = rankings.get("rankings") if isinstance(rankings, dict) else None
    kills: list[RankedKill] = []
    without_guild = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        guild_id = (row.get("guild") or {}).get("id")
        if not guild_id:
            without_guild += 1
            continue
        raw = row.get("fromlog")
        from_log = None if raw is None else bool(int(raw))
        kill_time = row.get("killTime")
        kills.append(
            RankedKill(
                guild_id=int(guild_id),
                kill_time_ms=float(kill_time) if isinstance(kill_time, (int, float)) else None,
                from_log=from_log,
            )
        )
    return kills, without_guild


@dataclass(frozen=True)
class Attempt:
    """One pull, timed on the **absolute** clock rather than its report's.

    `ReportFight.startTime` is milliseconds from the start of its *report*. Every
    reader here needs absolute time -- to order attempts across reports, to partition
    them into nights, and to compare a kill against the ranking's `killTime` -- and a
    report-relative number used as an absolute one is a small integer that sorts
    before every real date. That trap has already cost this repository one wrong
    answer, in `firstkills`, and this type is how it stops being re-derivable.
    """

    start_ms: float
    end_ms: float
    kill: bool

    @property
    def duration_ms(self) -> float:
        """This pull's length. **Non-positive means the payload disagrees with itself**
        and the caller must skip it rather than add it: a zero silently shrinks the
        total while reading as a very fast pull, and a negative one shrinks it twice.
        """
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class PullTime:
    """One guild's answer, or the reason there is none."""

    ms: float | None
    attempts: int
    reports: int
    reason: str | None = None
    #: Absolute ms of the first attempt and of the kill, when there is one.
    first_attempt_at: float | None = None
    kill_at: float | None = None
    #: Attempts that contributed a duration. `attempts` counts pulls; this counts the
    #: ones the sum is actually built from, and they differ when a timestamp is broken.
    usable_attempts: int = 0
    #: Distinct raid nights **observed** in the window. Never an estimate of nights
    #: raided -- see `partition_nights`.
    nights: int = 0
    #: First attempt to kill, in days. Published beside the hours because a guild that
    #: took three weeks over four logged hours is visibly a partial observation.
    span_days: float | None = None


def ordered_attempts(
    reports: list[dict], encounter_id: int, difficulty: int
) -> tuple[list[Attempt], int]:
    """Every attempt on one boss on the absolute clock, oldest first, plus the count
    of reports refused for stating no time of their own.

    `encounterID` and `difficulty` are re-checked even though the query filters on
    both: a filter that silently stopped filtering would pool a guild's Heroic
    attempts into its Mythic progress time, and the total would merely look larger.

    **A report with no `startTime` is refused, not sorted to zero.** It used to sort
    *first* -- ahead of every real report -- so if it held a kill it terminated the
    sum immediately and the guild published a near-zero progress time. Absolute times
    make the same report unusable rather than merely mis-ordered, which is the honest
    state: without its start there is no clock to put its fights on.
    """
    dateless = 0
    dated: list[tuple[float, dict]] = []
    for report in reports:
        base = report.get("startTime")
        if not isinstance(base, (int, float)):
            dateless += 1
            continue
        dated.append((float(base), report))

    rows: list[Attempt] = []
    for base, report in sorted(dated, key=lambda pair: pair[0]):
        for fight in report.get("fights") or []:
            if int(fight.get("encounterID") or 0) != int(encounter_id):
                continue
            stated = fight.get("difficulty")
            if stated is not None and int(stated) != int(difficulty):
                continue
            start, end = fight.get("startTime"), fight.get("endTime")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            rows.append(
                Attempt(
                    start_ms=base + float(start),
                    end_ms=base + float(end),
                    kill=fight.get("kill") is True,
                )
            )
    rows.sort(key=lambda a: a.start_ms)
    return rows, dateless


def partition_nights(attempts: list[Attempt]) -> int:
    """How many distinct raid nights the observed attempts fall into.

    A partition of what was **seen**, never an estimate of what happened. Two attempts
    more than `NIGHT_GAP_MS` apart are different nights; that is all it claims. A guild
    that logged one of its six nights has `nights == 1`, which is the true statement
    about the observation and says nothing about the six.

    This is published and must never be used as a *gate*: it cannot separate "logged
    two of six nights" from "killed it in two nights", so refusing on it would drop the
    fast, well-logged guilds and push the median up -- the opposite error, introduced
    deliberately.
    """
    if not attempts:
        return 0
    nights = 1
    previous = attempts[0].start_ms
    for attempt in attempts[1:]:
        if attempt.start_ms - previous >= NIGHT_GAP_MS:
            nights += 1
        previous = attempt.start_ms
    return nights


def pull_time(
    reports: list[dict],
    encounter_id: int,
    difficulty: int,
    kill_time_ms: float | None = None,
) -> PullTime:
    """Progress time up to and including the first kill, in milliseconds.

    `ms` is None when the answer is not knowable from this window; `reason` says which
    of `no-reports` / `no-fights` / `no-kill` / `unusable` / `no-report-time` /
    `kill-too-late` / `kill-too-early` applies.

    **`no-reports` and `no-fights` used to be one bucket, and separating them is the
    whole diagnostic.** They are opposite findings:

    - `no-reports` -- the guild's listing for this zone came back EMPTY. Nothing was
      read, so the boss is irrelevant: it is a fact about the guild or about
      `reports(guildID:)`, and it would be identical on all nine bosses.
    - `no-fights` -- the listing came back with reports in it and none of them holds a
      pull of *this* boss at *this* difficulty. That is a fact about the boss.

    Under one name they are indistinguishable, and the 2026-08-26 run's central puzzle
    -- 41 of 72 guild-boss pairs refused, at rates from 1-in-8 to 7-in-8 on the same
    zone -- cannot be attributed without the split. The count is published beside it
    (`reportsSeen`) so the answer survives in the artifact rather than living in one
    run's log.

    **`kill_time_ms` is the completeness screen and the reason this function can be
    trusted at all.** Without it the walk finds *a* kill and calls it the first one.
    Two ways that is wrong, and both published a confident number until 2026-08-27:

    - the guild killed the boss on a night nobody uploaded, so the earliest logged kill
      is a *farm* kill weeks later and every logged pull before it is counted as
      progression toward a kill that had already happened;
    - only farm nights were logged at all, so one wipe and one kill publish as the
      guild's whole progression.

    Both are refused as `kill-too-late`. The mirror case -- a logged kill *earlier* than
    the ranked one -- is refused as `kill-too-early` and counted apart rather than
    resolved in either direction: the log and the ranking disagreeing is a finding, and
    picking a winner here would bury it.

    Passing `kill_time_ms=None` restores the pre-screen behaviour and is for callers
    that genuinely have no ranked kill; it is not a fallback the sweep should take.
    """
    if not reports:
        return PullTime(None, 0, 0, "no-reports")
    attempts_list, dateless = ordered_attempts(reports, encounter_id, difficulty)
    if not attempts_list:
        # A listing made entirely of dateless reports is a different failure from one
        # holding no pull of this boss, and only the first is a payload problem.
        reason = "no-report-time" if dateless and dateless == len(reports) else "no-fights"
        return PullTime(None, 0, len(reports), reason)

    total = 0.0
    attempts = 0
    usable = 0
    seen: list[Attempt] = []
    for attempt in attempts_list:
        attempts += 1
        seen.append(attempt)
        if attempt.duration_ms > 0:
            total += attempt.duration_ms
            usable += 1
        if not attempt.kill:
            continue
        if not usable:
            return PullTime(None, attempts, len(reports), "unusable")
        if kill_time_ms is not None:
            drift = attempt.start_ms - kill_time_ms
            if drift > KILL_MATCH_TOLERANCE_MS:
                return PullTime(None, attempts, len(reports), "kill-too-late")
            if drift < -KILL_MATCH_TOLERANCE_MS:
                return PullTime(None, attempts, len(reports), "kill-too-early")
        first = seen[0].start_ms
        return PullTime(
            total,
            attempts,
            len(reports),
            None,
            first_attempt_at=first,
            kill_at=attempt.start_ms,
            usable_attempts=usable,
            nights=partition_nights(seen),
            span_days=round((attempt.start_ms - first) / 86_400_000.0, 3),
        )
    return PullTime(None, attempts, len(reports), "no-kill")


def median(values: list[float]) -> float | None:
    """Median of the values that exist, or None when none do.

    The median rather than the mean, for the reason every aggregate in this project
    uses it: one guild that left the boss running overnight drags a mean anywhere,
    and progress time has exactly that shape of outlier.
    """
    usable = sorted(v for v in values if isinstance(v, (int, float)))
    if not usable:
        return None
    middle = len(usable) // 2
    if len(usable) % 2:
        return float(usable[middle])
    return (usable[middle - 1] + usable[middle]) / 2.0


def quartiles(values: list[float]) -> tuple[float, float] | None:
    """(q1, q3) over the values that exist, or None below four of them.

    Four is the floor at which quartiles describe a spread rather than restate the
    extremes. Below it the chart shows the median alone and says the sample is thin.
    """
    usable = sorted(v for v in values if isinstance(v, (int, float)))
    if len(usable) < 4:
        return None
    half = len(usable) // 2
    lower, upper = usable[:half], usable[half + (len(usable) % 2) :]
    q1, q3 = median(lower), median(upper)
    return (q1, q3) if q1 is not None and q3 is not None else None


def guild_row(
    guild_id: int,
    outcome: str,
    reports_seen: int,
    duplicates: int = 0,
    hours: float | None = None,
    attempts: int | None = None,
    coverage: PullTime | None = None,
) -> dict:
    """One sampled guild's row: who, what happened, and over how many reports.

    A pure function rather than a closure over the sweep's loop variables -- ruff's
    B023 flags exactly that, and it is not pedantry here: a closure reading
    ``guild_id`` from the enclosing scope is correct only while it is called in the
    same iteration, which is a property of the call site rather than of the function.

    ``duplicateReports`` and the measurement fields are omitted when they have nothing
    to say, so a clean row stays the shape it was before any of this existed.
    """
    row: dict = {"id": guild_id, "outcome": outcome, "reportsSeen": reports_seen}
    if duplicates:
        row["duplicateReports"] = duplicates
    if hours is not None:
        row["hours"] = round(hours, 4)
        row["attempts"] = attempts
    if coverage is not None and coverage.ms is not None:
        # How much of a progression this observation could possibly be. A guild that
        # took three weeks over four logged hours on one night is visibly a partial
        # view; one that took four hours over two nights inside three days is not.
        # Published, never gated on -- see `partition_nights`.
        row["nightsObserved"] = coverage.nights
        row["spanDays"] = coverage.span_days
        row["firstAttemptAt"] = coverage.first_attempt_at
        row["killAt"] = coverage.kill_at
        if coverage.usable_attempts != coverage.attempts:
            # Attempts that contributed no duration. Without this the median attempt
            # count and the median hours are built from different populations and
            # nothing says so.
            row["usableAttempts"] = coverage.usable_attempts
    return row


@dataclass
class BossProgress:
    """What one boss cost, and how much of that is actually measured."""

    encounter_id: int
    name: str
    order: int
    difficulty: int
    hours: list[float] = field(default_factory=list)
    #: Attempts behind each measured guild's answer, index-aligned with ``hours``.
    #:
    #: Published because it is the cheapest thing that can tell a progress kill from a
    #: FARM kill, and the 2026-08-26 run needed exactly that and did not have it: every
    #: median came out at 5-18 minutes, which is one pull, and the document carried no
    #: field that said so. A boss whose median attempts is 1 was not progressed in the
    #: window that was read, whatever the hours say.
    attempts: list[int] = field(default_factory=list)
    #: Reports read per guild, whatever the outcome. The denominator behind
    #: ``no-reports`` vs ``no-fights``: a guild with 40 reports and no pull of this
    #: boss is a different finding from a guild with none.
    reports_seen: list[int] = field(default_factory=list)
    #: Every sampled guild and what became of it, so a cross-boss join is possible
    #: from the published document. Guild ids are public Warcraft Logs data; the rule
    #: this project keeps about never collecting names is about *characters*.
    guilds: list[dict] = field(default_factory=list)
    #: Guilds with no answer, by reason -- part of the cost per usable number.
    refused: dict[str, int] = field(default_factory=dict)
    #: Nights and spans behind each measured guild, index-aligned with ``hours``.
    nights: list[int] = field(default_factory=list)
    spans: list[float] = field(default_factory=list)
    #: The `fromlog` split over the guilds this boss SCREENED, measured rather than
    #: assumed. It is a property of Warcraft Logs' ingestion in that era as much as of
    #: the guilds, so it is not comparable across tiers as an absolute.
    kills_from_log: int = 0
    kills_not_from_log: int = 0
    guilds_seen: int = 0
    rows_without_guild: int = 0
    #: True when the ranking could not supply as many guilds as were asked for. Without
    #: it a short sample is indistinguishable from a small request, and `guildsSeen`
    #: alone reads as a deliberate choice.
    sample_short_of_request: bool = False
    #: The zone its reports were searched in. None means the boss was refused.
    zone_id: int | None = None

    def record(
        self,
        guild_id: int,
        outcome: str,
        reports_seen: int,
        duplicates: int = 0,
        hours: float | None = None,
        attempts: int | None = None,
        coverage: PullTime | None = None,
    ) -> None:
        """Append one sampled guild's row. **Every** guild gets one, whatever happened.

        Without this the document publishes per-boss totals only, and the question the
        refusal rate actually poses -- is the guild that fails on boss 8 the same guild
        that succeeded on boss 1 -- cannot be answered from the artifact at all, only
        from a run's log that nobody kept.
        """
        self.guilds.append(
            guild_row(guild_id, outcome, reports_seen, duplicates, hours, attempts, coverage)
        )
        self.reports_seen.append(reports_seen)
        if coverage is not None and coverage.ms is not None:
            self.nights.append(coverage.nights)
            if coverage.span_days is not None:
                self.spans.append(coverage.span_days)

    def to_json(self) -> dict:
        q = quartiles(self.hours)
        med = median(self.hours)
        return {
            "encounterId": self.encounter_id,
            "name": self.name,
            "order": self.order,
            "difficulty": self.difficulty,
            "difficultyName": DIFFICULTY_NAMES.get(self.difficulty, str(self.difficulty)),
            # `null`, never 0: a boss nobody could be measured for has NO answer, and
            # a stacked column must not draw that as a segment of zero height.
            "medianHours": None if med is None else round(med, 3),
            "q1Hours": None if q is None else round(q[0], 3),
            "q3Hours": None if q is None else round(q[1], 3),
            "sample": len(self.hours),
            "medianAttempts": (
                None if not self.attempts else median([float(a) for a in self.attempts])
            ),
            "zoneId": self.zone_id,
            "medianReportsSeen": (
                None if not self.reports_seen else median([float(r) for r in self.reports_seen])
            ),
            # How much of a progression each measured observation could be. These are
            # the honest reading of the residual limitation: after the screens the
            # remaining error is a night nobody uploaded, and a low night count beside
            # a wide span is what that looks like from outside.
            "medianNightsObserved": (
                None if not self.nights else median([float(n) for n in self.nights])
            ),
            "medianSpanDays": None if not self.spans else median(list(self.spans)),
            "killsFromLog": self.kills_from_log,
            "killsNotFromLog": self.kills_not_from_log,
            "guilds": list(self.guilds),
            "guildsSeen": self.guilds_seen,
            "sampleShortOfRequest": self.sample_short_of_request,
            "refused": dict(sorted(self.refused.items())),
            "rowsWithoutGuild": self.rows_without_guild,
        }


def stacked_total(bosses: list[BossProgress]) -> float | None:
    """The season's column height, or None when any boss is unmeasured.

    Deliberately refuses rather than summing what exists: a column built from six of
    eight bosses is shorter than one built from eight and looks like a *cheaper
    season*, which is the exact comparison the chart is for. The per-boss segments
    are still published; it is only the total that cannot honestly be stated.
    """
    values = [median(b.hours) for b in bosses]
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 3)
