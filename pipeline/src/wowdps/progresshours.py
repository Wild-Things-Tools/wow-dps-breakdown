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
    which of ``no-fights`` / ``no-kill`` / ``unusable`` applies.
    """
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


@dataclass
class BossProgress:
    """What one boss cost, and how much of that is actually measured."""

    encounter_id: int
    name: str
    order: int
    difficulty: int
    hours: list[float] = field(default_factory=list)
    #: Guilds with no answer, by reason -- part of the cost per usable number.
    refused: dict[str, int] = field(default_factory=dict)
    guilds_seen: int = 0
    rows_without_guild: int = 0

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
