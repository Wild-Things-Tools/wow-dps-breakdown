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
class PullTime:
    """One guild's answer, or the reason there is none."""

    ms: float | None
    attempts: int
    reports: int
    reason: str | None = None


def fight_duration_ms(fight: dict) -> float | None:
    """One attempt's length, or None when the payload cannot support one.

    A non-positive duration is refused rather than clamped to zero: it means the two
    timestamps disagree, and a zero silently shrinks the total while reading as a
    very fast pull.
    """
    start, end = fight.get("startTime"), fight.get("endTime")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    duration = end - start
    return duration if duration > 0 else None


def ordered_fights(reports: list[dict], encounter_id: int, difficulty: int) -> list[dict]:
    """Every attempt on one boss, in the order they were actually fought.

    ``encounterID`` and ``difficulty`` are re-checked even though the query filters
    on both: a filter that silently stopped filtering would pool a guild's Heroic
    attempts into its Mythic progress time, and the total would merely look larger.
    """
    rows: list[dict] = []
    for report in sorted(reports, key=lambda r: r.get("startTime") or 0):
        for fight in report.get("fights") or []:
            if int(fight.get("encounterID") or 0) != int(encounter_id):
                continue
            stated = fight.get("difficulty")
            if stated is not None and int(stated) != int(difficulty):
                continue
            rows.append(fight)
    return rows


def pull_time(reports: list[dict], encounter_id: int, difficulty: int) -> PullTime:
    """Progress time up to and including the first kill, in milliseconds.

    ``ms`` is None when the answer is not knowable from this window; ``reason`` says
    which of ``no-reports`` / ``no-fights`` / ``no-kill`` / ``unusable`` applies.

    **``no-reports`` and ``no-fights`` used to be one bucket, and separating them is
    the whole diagnostic.** They are opposite findings:

    - ``no-reports`` -- the guild's listing for this zone came back EMPTY. Nothing was
      read, so the boss is irrelevant: it is a fact about the guild or about
      ``reports(guildID:)``, and it would be identical on all nine bosses.
    - ``no-fights`` -- the listing came back with reports in it and none of them holds
      a pull of *this* boss at *this* difficulty. That is a fact about the boss.

    Under one name they are indistinguishable, and the 2026-08-26 run's central
    puzzle -- 41 of 72 guild-boss pairs refused, at rates from 1-in-8 to 7-in-8 on
    the same zone -- cannot be attributed without the split. The count is published
    beside it (``reportsSeen``) so the answer survives in the artifact rather than
    living in one run's log.
    """
    if not reports:
        return PullTime(None, 0, 0, "no-reports")
    fights = ordered_fights(reports, encounter_id, difficulty)
    if not fights:
        return PullTime(None, 0, len(reports), "no-fights")

    total = 0.0
    attempts = 0
    usable = 0
    for fight in fights:
        attempts += 1
        duration = fight_duration_ms(fight)
        if duration is not None:
            total += duration
            usable += 1
        if fight.get("kill") is True:
            if not usable:
                return PullTime(None, attempts, len(reports), "unusable")
            return PullTime(total, attempts, len(reports), None)
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
    guilds_seen: int = 0
    rows_without_guild: int = 0
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
    ) -> None:
        """Append one sampled guild's row. **Every** guild gets one, whatever happened.

        Without this the document publishes per-boss totals only, and the question the
        refusal rate actually poses -- is the guild that fails on boss 8 the same guild
        that succeeded on boss 1 -- cannot be answered from the artifact at all, only
        from a run's log that nobody kept.
        """
        self.guilds.append(guild_row(guild_id, outcome, reports_seen, duplicates, hours, attempts))
        self.reports_seen.append(reports_seen)

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
            "guilds": list(self.guilds),
            "guildsSeen": self.guilds_seen,
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
