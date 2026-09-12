"""Warcraft Logs v2 client, used to sanity-check simulated output against reality.

Sims answer "what is theoretically possible on a stationary dummy". Logs answer
"what do people actually do on this boss". They disagree for real reasons -- movement,
mechanics, deaths, target swaps, and players who are not perfect -- so this is a
*plausibility check*, never a correction factor. The site presents both and says
where they diverge.

Auth is OAuth2 client credentials (no user login): register a client at
https://www.warcraftlogs.com/api/clients/ and export::

    WCL_CLIENT_ID=...
    WCL_CLIENT_SECRET=...

The public client endpoint is rate limited by *points* per hour, not requests, and
the cost of a query is not published as a formula -- Warcraft Logs' own advice is to
read ``rateLimitData`` and find out. A rankings query costs well under a point, so a
nightly verification pass over a raid tier is comfortably inside budget; event
queries over a whole fight are the expensive end, which is why ``fightprobe`` meters
itself against ``rate_limit()`` and caches every response it gets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import fightprofile, logsanalysis

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

#: What a client waits for one response unless told otherwise. 30 s is the value
#: every existing caller has always run with; it is named so a caller can say why
#: it wants a different one.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Two of this module's thirteen query documents used to carry no `rateLimitData`,
# and this was one of them -- the one `wowdps verify` sends 208 times a week. The
# `PointLedger` docstring below says, in words, that *"every query in this module
# asks for rateLimitData alongside its real payload"*; it was false for exactly the
# document belonging to the only scheduled, points-spending pass with no cost
# measurement at all. Measured on 2026-09-12: a whole verify run left the ledger at
# `firstReading None, lastReading None` -- UNMEASURED by construction rather than
# merely un-bracketed, so no ceiling could be checked and no cost published.
#
# The block rides on a query that is being sent anyway, so it costs no round trip.
# Whether a resolved field costs POINTS is a different question and is one of #170's
# open measurements; it is not claimed here. What is claimed is that the other
# eleven documents already take that bet, and that a reading nobody takes cannot be
# compared with anything.
RANKINGS_QUERY = """
query SpecRankings(
  $encounterId: Int!
  $difficulty: Int!
  $metric: CharacterRankingMetricType!
  $className: String
  $specName: String
  $page: Int!
) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  worldData {
    encounter(id: $encounterId) {
      id
      name
      characterRankings(
        difficulty: $difficulty
        metric: $metric
        className: $className
        specName: $specName
        page: $page
      )
    }
  }
}
"""

# One zone by id, which the zone *list* cannot always reach. Measured on
# 2026-08-17: `worldData.zones` returns 42 zones topping out at id 50, and zone 54
# -- the Season 2 PTR zone, which warcraftlogs.com/zone/reports?zone=54 serves --
# is not among them. So the list is not an enumeration of every zone, and anything
# that treats it as one silently concludes a zone does not exist. `zones` also
# takes an `expansion_id`, which is the likelier explanation than PTR-specific
# hiding, but either way the direct lookup is the answer rather than a guess about
# the filter.
ZONE_BY_ID_QUERY = """
query ZoneById($zoneId: Int!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  worldData {
    zone(id: $zoneId) {
      id
      name
      frozen
      encounters { id name }
    }
  }
}
"""

# The second document that carried no reading, and the same defect one command
# across: `cmd_fight_zones` prints `spend_sentence(ledger)` over a ledger this was
# the only feed for, so a read-only `wowdps fight-zones` could only ever print
# UNMEASURED. Its sibling `ZONE_BY_ID_QUERY` -- the `--seed`/`--scan` path -- has
# always carried one, so the same command measured itself on one route and never on
# the other.
ZONE_QUERY = """
query Zones {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  worldData {
    zones {
      id
      name
      frozen
      encounters { id name }
    }
  }
}
"""

# The route to "who killed this first" that does not go through rankings at all.
# Verified against the live schema on 2026-08-16: `reportData.reports` takes a
# zoneID and a startTime/endTime window and is not restricted to ranked parses, so
# a public log Warcraft Logs never ranked is still in it.
#
# `ReportPagination` is the declared return type and the server refuses to
# introspect it, so only `data` is requested -- the Laravel-style envelope these
# APIs use -- and paging stops on a short page rather than on a `has_more_pages`
# field whose name is unverified. `firstkills.reports_from_payload` reads the
# envelope defensively for the same reason.
REPORTS_QUERY = """
query Reports($zoneId: Int!, $startTime: Float!, $endTime: Float!, $limit: Int!, $page: Int!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  reportData {
    reports(zoneID: $zoneId, startTime: $startTime, endTime: $endTime, limit: $limit, page: $page) {
      data { code startTime endTime }
    }
  }
}
"""

# One report's kills of one encounter. `killType: Kills` is the server-side filter;
# `kill` is requested anyway so the extraction can re-check it, because a filter
# that silently stopped filtering would put wipes into a sample of first *kills*.
# Deliberately *not* filtered by encounter. A zone's report list is the same for
# every boss in it, so filtering server-side would ask the same question nine times
# with nine different variable sets and nine cache misses -- measured at 2880 of
# 3600 points before the ceiling stopped the first run. Unfiltered, the query and
# its variables are identical across every encounter, the response cache serves the
# second through ninth for nothing, and `firstkills.kills_from_report` does the
# encounter filter locally, which it had to do anyway.
REPORT_KILLS_QUERY = """
query ReportKills($code: String!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  reportData {
    report(code: $code) {
      code
      startTime
      fights(killType: Kills) {
        id
        encounterID
        difficulty
        kill
        startTime
        endTime
      }
    }
  }
}
"""

# The zone a boss belongs to, which `reports` needs and the probe does not otherwise
# know. `Encounter.zone` is a non-null field, so this cannot come back empty for a
# real encounter id.
ENCOUNTER_ZONE_QUERY = """
query EncounterZone($encounterId: Int!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  worldData {
    encounter(id: $encounterId) {
      id
      name
      zone { id name frozen }
    }
  }
}
"""

# The gear and specialisation of every player in one pull, in one request.
#
# `playerDetails` is documented as "a table of information for the players of a
# report, including their specs, talents, gear, etc. This data is not considered
# frozen, and it can change without notice" -- so it is an untyped `JSON` scalar
# and `harvest.player_detail_rows` reads it defensively, exactly as
# `_ranking_entries` reads `characterRankings`.
#
# Scoped to one fight by `fightIDs`, which is the whole reason this is affordable:
# unscoped it returns the players of every pull in the report. It is also what makes
# a sampled kill cost the same whether one spec is wanted out of it or twenty --
# the response carries the entire raid.
#
# `killType` is passed explicitly. Its default is `All`, and this project has
# already paid once for the general lesson that an omitted argument is a *default*
# rather than nothing: the server answers helpfully with a wider set and nothing
# says so.
#
# **`includeCombatantInfo: true` is the whole reason this query returns gear**, and
# it is the same lesson a second time. It was omitted; its default is `false`; and
# the server then answers with every row's `combatantInfo` present and **empty** --
# serialised as `[]`, not as an absent key, so nothing downstream reads as broken.
# The first live probe (CI run 32660348582, 2026-08-23) accordingly reported
# `combatantInfo keys: list` and `gear entries: 0 readable, 0 skipped` over fourteen
# real players, which reads like a payload shape this code failed to parse and was
# an argument it failed to send.
#
# Read from the server rather than from a mirror: `wowdps wcl-schema --type Report`
# on 2026-08-23 (CI run 32660759853) returns
#
#     playerDetails: JSON
#         difficulty: Int = 0
#         encounterID: Int = 0
#         endTime: Float = 0
#         fightIDs: [Int] = []
#         killType: KillType = All
#         startTime: Float = 0
#         translate: Boolean = true
#         includeCombatantInfo: Boolean = false
#
# Every argument this query passes is therefore stated, and no argument it does not
# pass is load-bearing. `Encounter.characterRankings` carries an argument of the
# same name and the same default; nothing here uses it, because the gear has to come
# from the sampled kill rather than from whichever pull a ranking row happens to be.
PLAYER_DETAILS_QUERY = """
query PlayerDetails($code: String!, $fightId: Int!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  reportData {
    report(code: $code) {
      code
      startTime
      playerDetails(
        fightIDs: [$fightId]
        killType: Kills
        translate: true
        includeCombatantInfo: true
      )
    }
  }
}
"""

# One encounter's name, which is what verifies a PTR id's live twin before anything
# is harvested from it. `Encounter.name` is `String!` -- introspected on 2026-08-23,
# CI run 32660759853 -- so a name that comes back absent means the *encounter* is
# absent, which is a real answer and the one `harvest.choose_encounter_id` refuses on.
#
# Deliberately not `ENCOUNTER_ZONE_QUERY`, which would answer as well and asks for a
# zone nothing needs: this query is sent for an id nobody has established exists.
ENCOUNTER_NAME_QUERY = """
query EncounterName($encounterId: Int!) {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
  worldData {
    encounter(id: $encounterId) {
      id
      name
    }
  }
}
"""


def talent_codes_query(actor_ids: list[int]) -> str:
    """One query that asks for every actor's talent code in a single request.

    ``ReportFight.talentImportCode`` takes one ``actorID`` and returns one string,
    so the obvious reading is one request per player -- twenty per sampled kill, on
    the field this project has measured to be the expensive kind. GraphQL aliases
    collapse that to one: the same field is selected once per actor under a
    distinct name, and the server resolves them all against a fight it has already
    loaded.

    The actor ids are written into the document rather than passed as variables
    because the *number* of them varies per fight, and a variable list cannot
    produce a variable number of selections. They are therefore forced through
    ``int`` first -- an id that is not an integer is a bug in the actor extraction,
    and letting one reach the query text would be the one place in this module
    where a payload value becomes executable syntax.
    """
    if not actor_ids:
        raise ValueError("no actor ids to ask for")
    aliases = "\n        ".join(
        f"a{int(actor)}: talentImportCode(actorID: {int(actor)})" for actor in actor_ids
    )
    return f"""
query TalentCodes($code: String!, $fightId: Int!) {{
  rateLimitData {{ limitPerHour pointsSpentThisHour pointsResetIn }}
  reportData {{
    report(code: $code) {{
      fights(fightIDs: [$fightId]) {{
        id
        {aliases}
      }}
    }}
  }}
}}
"""


RATE_LIMIT_QUERY = """
query RateLimit {
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
}
"""

# Everything one report can say about the *shape* of its fights, in a single
# request. Splitting these apart would cost points per round trip for data the
# server has already loaded: the schema notes that fetching fights and phases
# together does not double-charge.
#
# `masterData.actors` is what turns the numeric actor ids in event payloads into
# names, and `abilities` does the same for aura ids. Both are per report rather
# than per fight, so one fetch serves every fight in it.
#
# Deliberately unfiltered. Restricting it to `type: "NPC"` reads as an obvious
# saving -- the events we care about are on enemies -- but then the payload holds
# no player ids, and there is no way left to tell an aura the encounter puts on
# its own add from a debuff a player put there. Both land on an enemy and both
# arrive in the same stream. Without the player list the nearest-window search
# nominated a Paladin cooldown as an encounter mechanic on the first real run.
FIGHT_STRUCTURE_QUERY = """
query FightStructure($code: String!, $encounterId: Int!, $difficulty: Int!) {
  reportData {
    report(code: $code) {
      code
      title
      startTime
      endTime
      phases { encounterID separatesWipes phases { id name isIntermission } }
      masterData(translate: true) {
        actors { id gameID name subType type petOwner }
        abilities { gameID name type }
      }
      fights(encounterID: $encounterId, difficulty: $difficulty, killType: Encounters) {
        id
        encounterID
        name
        difficulty
        kill
        size
        startTime
        endTime
        fightPercentage
        averageItemLevel
        friendlyPlayers
        enemyNPCs { id gameID instanceCount groupCount }
        phaseTransitions { id startTime }
        lastPhase
      }
    }
  }
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
}
"""

# The expensive one. `limit` is capped at 10000 by the API; a page that comes back
# with a nextPageTimestamp means the fight has more events than one page holds, and
# the caller decides whether to pay for the next page or mark the result truncated.
EVENTS_QUERY = """
query FightEvents(
  $code: String!
  $fightId: Int!
  $dataType: EventDataType!
  $hostility: HostilityType!
  $startTime: Float!
  $endTime: Float!
  $limit: Int!
) {
  reportData {
    report(code: $code) {
      events(
        fightIDs: [$fightId]
        dataType: $dataType
        hostilityType: $hostility
        startTime: $startTime
        endTime: $endTime
        limit: $limit
      ) {
        data
        nextPageTimestamp
      }
    }
  }
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
}
"""

# The same document as EVENTS_QUERY with `includeResources: true`, and it is a
# SEPARATE document on purpose rather than a variable on the one above.
#
# Two reasons, and the second is the load-bearing one. The response cache is keyed on
# `sha256(json({query, variables}))`, so adding a variable to EVENTS_QUERY would change
# the key of every event page this project has ever cached -- a nightly that reads from
# that cache would silently re-fetch the lot and pay for it. And the cost of resources
# is not free: they ride on every event, so a stream fetched this way is materially
# larger than the same stream without them. Keeping the two documents apart keeps that
# cost visible and opt-in.
#
# WHY IT EXISTS AT ALL: `x` and `y` are not ordinary event fields. `RpgLogs.d.ts` types
# them on `ResourceData` (x, y, facing, hitPoints, maxHitPoints), and the v2 API only
# emits that block when `includeResources` is true -- its default is **false**, which is
# the fourth time this project has been caught by an omitted argument being a default
# rather than nothing (`hostilityType`, `includeResources`, `zoneID: 0`,
# `includeCombatantInfo`). Without this document there are no coordinates in any
# response, and every cache entry written before it is therefore useless for a question
# about position -- not stale, simply silent.
#
# WHOSE position arrives is NOT assumed here. wtt-frontend measured that v2 sends the
# resource fields FLAT on the event with a `resourceActor` discriminator (1 = source,
# 2 = target) where the Scripting API nests them as sourceResources/targetResources.
# `addspawns.describe_event_shapes` reports what actually turns up rather than reading
# one of the two shapes and calling the other absent.
EVENTS_WITH_RESOURCES_QUERY = """
query FightEventsWithResources(
  $code: String!
  $fightId: Int!
  $dataType: EventDataType!
  $hostility: HostilityType!
  $startTime: Float!
  $endTime: Float!
  $limit: Int!
) {
  reportData {
    report(code: $code) {
      events(
        fightIDs: [$fightId]
        dataType: $dataType
        hostilityType: $hostility
        startTime: $startTime
        endTime: $endTime
        limit: $limit
        includeResources: true
      ) {
        data
        nextPageTimestamp
      }
    }
  }
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
}
"""

TABLE_QUERY = """
query FightTable(
  $code: String!
  $fightId: Int!
  $dataType: TableDataType!
  $viewBy: ViewType!
  $hostility: HostilityType!
) {
  reportData {
    report(code: $code) {
      table(
        fightIDs: [$fightId]
        dataType: $dataType
        viewBy: $viewBy
        hostilityType: $hostility
      )
    }
  }
  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
}
"""


class WarcraftLogsError(RuntimeError):
    pass


class RateLimited(WarcraftLogsError):
    """A 429: the hourly point budget is spent.

    Its own class because a caller that walks guilds one at a time has to tell
    "this guild's fetch failed" (name it, move on, retry later) from "the service
    will refuse everything for the rest of the hour" (stop the run). Both used to
    arrive as one class distinguished only by the message text.
    """


@dataclass
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> Credentials:
        client_id = os.environ.get("WCL_CLIENT_ID")
        client_secret = os.environ.get("WCL_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise WarcraftLogsError(
                "WCL_CLIENT_ID and WCL_CLIENT_SECRET must be set. Create a client at "
                "https://www.warcraftlogs.com/api/clients/"
            )
        return cls(client_id=client_id, client_secret=client_secret)


#: The four things two bracketing readings of the hourly counter can say. A run's
#: cost is only a *measurement* in the last of them, and the other three are three
#: different findings rather than three ways of writing zero.
SPEND_NO_READING = "no-reading"
SPEND_WENT_BACKWARDS = "went-backwards"
SPEND_DID_NOT_MOVE = "did-not-move"
SPEND_MEASURED = "measured"


def spend_state(first: float | None, last: float | None) -> str:
    """Classify a pair of readings, in one place for every reader of them.

    Both a live ``PointLedger`` and a published ``cost`` block have to answer this,
    and a second implementation of the rule is the thing that drifts -- the split
    ``fightextract.group_uploads`` already carries for the same reason.
    """
    if first is None or last is None:
        return SPEND_NO_READING
    if last < first:
        return SPEND_WENT_BACKWARDS
    if last == first:
        return SPEND_DID_NOT_MOVE
    return SPEND_MEASURED


def spend_state_of(ledger: dict) -> str:
    """The same classification over a published ``cost`` block."""
    return spend_state(ledger.get("firstReading"), ledger.get("lastReading"))


@dataclass
class PointLedger:
    """What the API actually charged, read back from ``rateLimitData``.

    Warcraft Logs meters by points and does not publish the cost function, so the
    only honest way to state what a pass costs is to measure it. Every query in
    this module asks for ``rateLimitData`` alongside its real payload, which costs
    no extra round trip, and the ledger keeps the running total.

    One caveat that must travel with any per-query number produced from this: the
    reading arrives *with* the response, and whether the server has already
    charged for that same response is not documented. So a delta between two
    consecutive readings is reliable as a total and may be attributed one query
    late. Totals are what get published; per-query costs are labelled estimates.
    """

    limit_per_hour: int | None = None
    first_reading: float | None = None
    last_reading: float | None = None
    resets_in: int | None = None
    entries: list[tuple[str, float, bool]] = field(default_factory=list)
    #: Requests sent that actually reached the network (a cache hit is not one).
    requests_sent: int = 0
    #: Whatever the response headers say about a REQUEST ceiling, as opposed to the
    #: point ceiling in the body.
    #:
    #: **Points are not the only budget, and this side has been blind to the other
    #: one.** `rateLimitData` arrives in the response *body* and meters points;
    #: anything Warcraft Logs says about requests per hour arrives in the *headers*,
    #: which nothing here read. A pass can therefore be comfortably inside 18,000
    #: points and hit a ceiling it never measured -- and a 429 would read as "the
    #: hourly point budget is spent", which is what this module's own error message
    #: says and would be the wrong diagnosis.
    #:
    #: Recorded rather than enforced: whether such a header exists, and what it is
    #: called, is not established from this side. `None` means no header matched,
    #: which is not the same as no ceiling.
    request_headers: dict[str, str] = field(default_factory=dict)

    def note_response(self, headers) -> None:
        """Count the request and keep any rate-limit headers it carried.

        Called only on a real network response, so ``requests_sent`` is what the
        service actually saw -- a cache hit is not a request and must not be counted
        as one, or the ratio this exists to measure is wrong in the flattering
        direction.
        """
        self.requests_sent += 1
        try:
            items = headers.items()
        except AttributeError:
            return
        if items is None:
            return
        for key, value in items:
            if "ratelimit" in str(key).lower().replace("-", ""):
                self.request_headers[str(key).lower()] = str(value)

    def record(self, label: str, payload: dict, cached: bool = False) -> None:
        """Count the query, and take its budget reading only if it is this run's.

        **A cache hit must not move the readings**, and that is measured rather
        than tidy. A cached response carries the ``rateLimitData`` block that was
        preserved with it, so serving one pushes a *previous run's* counter into
        this ledger. Run 34035705116 (spawn-probe, 2026-09-06) published
        ``pointsSpentThisRun: -1524.0`` for exactly that reason: its 74 fresh
        responses read 4005.27 -> 4340.19, rising throughout, and the final
        "reading" of 2480.27 came out of a restored cache file whose sha256
        matches the artifact of a run seven minutes earlier.

        Two consequences were worse than the negative number. ``pointsSpentThisHour``
        was published as the earlier run's figure, understating the hour by ~1,860
        points; and ``fightprobe.check_budget`` compared ``--point-ceiling``
        against a stale balance, so the guard that exists to stop short of a 429
        was arguing from a number that was minutes old.

        This is the rule ``rate_limit`` already states one function down -- *a
        cached response is a record of then and this query asks about now* -- and
        it was never applied to the readings that ride along with every other
        query. The entry is still appended, so ``cacheHits`` counts it.
        """
        data = payload.get("rateLimitData") or {}
        spent = data.get("pointsSpentThisHour")
        if isinstance(spent, (int, float)) and not cached:
            if self.first_reading is None:
                self.first_reading = float(spent)
            self.last_reading = float(spent)
            self.limit_per_hour = data.get("limitPerHour", self.limit_per_hour)
            self.resets_in = data.get("pointsResetIn", self.resets_in)
        self.entries.append(
            (label, float(spent) if isinstance(spent, (int, float)) else -1.0, cached)
        )

    @property
    def spend_state(self) -> str:
        """Which of the four things the two bracketing readings can say."""
        return spend_state(self.first_reading, self.last_reading)

    @property
    def spent(self) -> float | None:
        """Points this run cost, or ``None`` when the readings cannot say.

        ``None`` covers two states and they are different findings, which is why
        ``spend_state`` exists beside this: nothing reported a reading at all, or
        the counter went *backwards* between the two. Never a negative number and
        never ``abs()`` -- a negative reads as a measurement, is not one, and
        clamping it would replace a wrong number with a more plausible wrong
        number, which this repository refuses by name.
        """
        if spend_state(self.first_reading, self.last_reading) in (
            SPEND_NO_READING,
            SPEND_WENT_BACKWARDS,
        ):
            return None
        assert self.first_reading is not None and self.last_reading is not None
        return round(self.last_reading - self.first_reading, 4)

    def to_json(self) -> dict:
        return {
            "limitPerHour": self.limit_per_hour,
            "pointsSpentThisRun": self.spent,
            "pointsSpentThisHour": self.last_reading,
            # Both ends of the bracket, so a run total of zero can be read as
            # "the counter never moved" rather than "the queries were free".
            "firstReading": self.first_reading,
            "lastReading": self.last_reading,
            "pointsResetIn": self.resets_in,
            # Named, so a reader of the document does not have to re-derive it from
            # the two readings beside it. Always emitted rather than only when true:
            # this block is rebuilt on every run and sits outside every settle
            # comparison, so an absent key means "written before this existed"
            # rather than "false".
            "counterWentBackwards": self.spend_state == SPEND_WENT_BACKWARDS,
            "queries": len([entry for entry in self.entries if not entry[2]]),
            "cacheHits": len([entry for entry in self.entries if entry[2]]),
            "note": (
                "Points are read back from rateLimitData rather than predicted: "
                "Warcraft Logs does not publish a cost formula. The run total is a "
                "measurement; attributing it to individual queries can lag by one "
                "response."
            ),
        }


def spend_sentence(ledger: PointLedger) -> str:
    """One line saying what a run cost, or which kind of unmeasured it is.

    Three commands printed the identical sentence with the identical branch, and
    all three said "the hourly counter did not move" for **any** falsy value --
    which is right for ``0.0`` and was never reached for a negative one, because
    ``-1524.0`` is truthy and printed as ``-1524.0 points``. That is the shape this
    repository refuses everywhere else: a number that reads as a measurement and is
    the absence of one.
    """
    state = ledger.spend_state
    readings = f"readings {ledger.first_reading} -> {ledger.last_reading}"
    if state == SPEND_NO_READING:
        return "UNMEASURED (no rate-limit reading came back)"
    if state == SPEND_WENT_BACKWARDS:
        return (
            f"UNMEASURED (the hourly counter went BACKWARDS, {readings}) -- the two "
            "readings are not from the same hour, so their difference is not a cost"
        )
    if state == SPEND_DID_NOT_MOVE:
        return f"UNMEASURED (the hourly counter did not move, {readings})"
    spent = ledger.spent
    assert spent is not None
    return f"{spent:.1f} points"


class WarcraftLogsClient:
    def __init__(
        self,
        credentials: Credentials,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_dir: Path | None = None,
    ) -> None:
        self._credentials = credentials
        #: Per-request timeout, in seconds. A constructor parameter because callers
        #: differ: a rankings check wants to fail fast, a guild's report walk has
        #: been measured taking over a minute (see `progresssweep`).
        self._timeout = timeout
        self._token: str | None = None
        self._client = httpx.Client(timeout=timeout)
        #: Responses are cached on disk by (query, variables). Re-running a probe
        #: against the same reports then costs nothing, which is what makes it
        #: safe to iterate on the extraction without burning the hourly budget.
        #:
        #: **Coerced, because the annotation is not enforced and the failure is
        #: late.** `argparse` hands `--cache` back as a `str`, which satisfies this
        #: signature at every call site, and then raises `TypeError: unsupported
        #: operand type(s) for /` inside `_cache_path` -- three paid queries into a
        #: live run. Measured that way on 2026-09-06, run 34030450826. Coercing here
        #: makes the promise real for every caller rather than for the ones that
        #: remembered `Path(...)`.
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self.ledger = PointLedger()

    def __enter__(self) -> WarcraftLogsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        try:
            response = self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._credentials.client_id, self._credentials.client_secret),
            )
        except httpx.HTTPError as exc:
            raise WarcraftLogsError(f"token request failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise WarcraftLogsError(
                f"token request failed ({response.status_code}): {response.text[:200]}"
            )
        token = response.json().get("access_token")
        if not token:
            raise WarcraftLogsError("token response contained no access_token")
        self._token = token
        return token

    def _cache_path(self, query: str, variables: dict) -> Path | None:
        if not self._cache_dir:
            return None
        digest = hashlib.sha256(
            json.dumps({"q": query, "v": variables}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        return self._cache_dir / f"{digest}.json"

    def query(
        self,
        query: str,
        variables: dict | None = None,
        label: str = "query",
        cache: bool = True,
    ) -> dict:
        """Send one document, or serve it from the response cache.

        ``cache=False`` is for a query whose answer is *the present moment* rather
        than a fact about a report -- see ``rate_limit``. Everything else is cached
        on (query, variables), which is what makes iterating on an extraction free.
        """
        variables = variables or {}
        cached_at = self._cache_path(query, variables) if cache else None
        if cached_at and cached_at.is_file():
            payload = json.loads(cached_at.read_text(encoding="utf-8"))
            self.ledger.record(label, payload, cached=True)
            return payload

        token = self._authenticate()
        # Every httpx failure -- a read timeout above all, measured at over 60 s on
        # `reports(guildID){fights}` by the backend -- becomes a WarcraftLogsError
        # here, so a caller's `except WarcraftLogsError` around one guild's fetch
        # catches the network failing as well as the service refusing. Until this,
        # a timeout was the one failure that escaped every such handler and took
        # the whole pass down one guild in. `TimeoutException` subclasses
        # `HTTPError` in httpx, so one clause covers both.
        try:
            response = self._client.post(
                API_URL,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise WarcraftLogsError(f"request failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited(
                "rate limited by Warcraft Logs: the hourly point budget is spent. "
                "Re-run later; cached responses cost nothing."
            )
        if response.status_code != 200:
            raise WarcraftLogsError(f"query failed ({response.status_code}): {response.text[:300]}")
        # `getattr`, because a budget reading must never be the thing that kills a
        # pass -- and that rule applies to reaching the headers as much as to parsing
        # them. A response object without `.headers` at all is exactly what a test
        # double is, and this raised AttributeError on two of them before the guard
        # was moved up one level.
        self.ledger.note_response(getattr(response, "headers", None))
        payload = response.json()
        if payload.get("errors"):
            raise WarcraftLogsError(f"GraphQL errors: {payload['errors']}")
        data = payload.get("data") or {}
        self.ledger.record(label, data)

        if cached_at:
            cached_at.parent.mkdir(parents=True, exist_ok=True)
            cached_at.write_text(json.dumps(data), encoding="utf-8")
        return data

    def rate_limit(self) -> dict:
        """The current point budget, as its own query. **Never cached.**

        Taken once before and once after a pass, this brackets the whole run: the
        difference is exactly what the pass cost, with no attribution guesswork.

        The cache bypass is what makes that true, and without it the whole
        measurement is silently impossible. ``RATE_LIMIT_QUERY`` takes no variables,
        so both bracketing readings hash to the same ``(query, {})`` -- the second
        one is served from the first one's response, the two readings are equal by
        construction, and the run reports ``pointsSpentThisRun = 0`` for any pass at
        any size. Measured against a stub whose counter moved 100 -> 118: one HTTP
        call, both readings 109.0, delta 0.0. And because the cache is a *directory*
        that CI restores between runs, the first reading of a later run would be a
        number the API returned hours ago.

        A cached response is a record of *then*; this query asks about *now*. Those
        are different kinds of answer and only one of them can be stored.
        """
        data = self.query(RATE_LIMIT_QUERY, label="rateLimit", cache=False)
        return data.get("rateLimitData") or {}

    def fight_structure(self, code: str, encounter_id: int, difficulty: int) -> dict:
        """Fights, phase metadata and the report's actor/ability names, in one call."""
        data = self.query(
            FIGHT_STRUCTURE_QUERY,
            {"code": code, "encounterId": encounter_id, "difficulty": difficulty},
            label=f"fights:{code}",
        )
        return ((data.get("reportData") or {}).get("report")) or {}

    def fight_events(
        self,
        code: str,
        fight_id: int,
        data_type: str,
        hostility: str,
        start_ms: float,
        end_ms: float,
        limit: int = 10000,
        max_pages: int = 5,
        include_resources: bool = False,
    ) -> tuple[list[dict], bool]:
        """Every event of one type for one fight, and whether the fetch was cut short.

        Returns ``(events, truncated)``. Truncation is reported rather than
        silently accepted: a target-count timeline built from the first page of a
        long fight would show adds arriving and never leaving.

        ``include_resources`` selects a *different document*
        (``EVENTS_WITH_RESOURCES_QUERY``), which is the only way to get ``x``/``y``
        out of this API -- see that document's comment for why it is a second
        document and not a variable on the first. Default False, so every existing
        caller keeps its cache entries and pays nothing for a field it does not read.
        """
        document = EVENTS_WITH_RESOURCES_QUERY if include_resources else EVENTS_QUERY
        suffix = ":res" if include_resources else ""
        collected: list[dict] = []
        cursor = start_ms
        for page in range(max_pages):
            data = self.query(
                document,
                {
                    "code": code,
                    "fightId": fight_id,
                    "dataType": data_type,
                    "hostility": hostility,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
                label=f"events{suffix}:{data_type}:{code}:{fight_id}:p{page}",
            )
            events = ((data.get("reportData") or {}).get("report") or {}).get("events") or {}
            rows = events.get("data")
            if isinstance(rows, str):
                rows = json.loads(rows)
            collected.extend(row for row in (rows or []) if isinstance(row, dict))

            nxt = events.get("nextPageTimestamp")
            if not isinstance(nxt, (int, float)):
                return collected, False
            cursor = float(nxt)
        return collected, True

    def fight_table(
        self,
        code: str,
        fight_id: int,
        data_type: str = "DamageDone",
        view_by: str = "Default",
        hostility: str = "Friendlies",
    ) -> dict | None:
        data = self.query(
            TABLE_QUERY,
            {
                "code": code,
                "fightId": fight_id,
                "dataType": data_type,
                "viewBy": view_by,
                "hostility": hostility,
            },
            label=f"table:{data_type}:{view_by}:{code}:{fight_id}",
        )
        table = ((data.get("reportData") or {}).get("report") or {}).get("table")
        if isinstance(table, str):
            table = json.loads(table)
        return table if isinstance(table, dict) else None

    def player_details(self, code: str, fight_id: int) -> object:
        """The raw ``playerDetails`` payload for one fight.

        Returned untouched rather than parsed here: the field is an untyped JSON
        scalar whose shape Warcraft Logs explicitly declines to freeze, so reading
        it is ``harvest.player_detail_rows``' job and an unexpected shape has to be
        visible there rather than swallowed in the client. Same arrangement as
        ``reports_in_window``.
        """
        data = self.query(
            PLAYER_DETAILS_QUERY,
            {"code": code, "fightId": fight_id},
            label=f"player-details:{code}:{fight_id}",
        )
        return ((data.get("reportData") or {}).get("report") or {}).get("playerDetails")

    def talent_import_codes(self, code: str, fight_id: int, actor_ids: list[int]) -> dict[int, str]:
        """``actor id -> talent loadout string`` for a whole pull, in one request.

        An actor the server answers ``null`` for is *absent from the result* rather
        than present with an empty string. The field is documented to return null
        for a non-player actor and for a pre-Dragonflight fight, and those are two
        different findings from "this player ran no talents", which is not a state
        that exists. ``harvest.validate`` reports the missing code as its own
        rejection reason.
        """
        query = talent_codes_query(actor_ids)
        data = self.query(
            query,
            {"code": code, "fightId": fight_id},
            label=f"talent-codes:{code}:{fight_id}",
        )
        fights = ((data.get("reportData") or {}).get("report") or {}).get("fights") or []
        codes: dict[int, str] = {}
        for fight in fights:
            if not isinstance(fight, dict):
                continue
            for key, value in fight.items():
                if key.startswith("a") and key[1:].isdigit() and isinstance(value, str) and value:
                    codes[int(key[1:])] = value
        return codes

    def encounter_name(self, encounter_id: int) -> str | None:
        """One encounter's name, or ``None`` when the schema has no such encounter.

        The two answers are different and must stay different: a name is what lets
        ``harvest.choose_encounter_id`` accept a PTR id's live twin, and ``None``
        is what makes it refuse. ``Encounter.name`` is `String!`, so a missing name
        can only mean a missing encounter.
        """
        data = self.query(
            ENCOUNTER_NAME_QUERY,
            {"encounterId": encounter_id},
            label=f"encounter-name:{encounter_id}",
        )
        encounter = ((data.get("worldData") or {}).get("encounter")) or {}
        name = encounter.get("name")
        return name if isinstance(name, str) and name.strip() else None

    def encounter_zone(self, encounter_id: int) -> dict:
        """The zone one encounter belongs to. `reports` is keyed on zone, not boss."""
        data = self.query(
            ENCOUNTER_ZONE_QUERY,
            {"encounterId": encounter_id},
            label=f"encounter-zone:{encounter_id}",
        )
        encounter = ((data.get("worldData") or {}).get("encounter")) or {}
        return encounter.get("zone") or {}

    def reports_in_window(
        self, zone_id: int, start_ms: int, end_ms: int, page: int = 1, limit: int = 100
    ) -> object:
        """One page of logs uploaded for a zone in a time window.

        Returns the raw pagination payload rather than a list: its envelope could not
        be introspected, so reading it is `firstkills.reports_from_payload`'s job and
        an unexpected shape has to be visible there rather than swallowed here.
        """
        data = self.query(
            REPORTS_QUERY,
            {
                "zoneId": zone_id,
                # Warcraft Logs takes these as seconds-with-millis floats in some
                # places and plain epoch ms in others. Everything else in this module
                # works in epoch ms, which is what report and fight timestamps are, so
                # that is what goes out.
                "startTime": float(start_ms),
                "endTime": float(end_ms),
                "limit": limit,
                "page": page,
            },
            label=f"reports:{zone_id}:{page}",
        )
        return ((data.get("reportData") or {}).get("reports")) or {}

    def report_kills(self, code: str) -> tuple[float, list[dict]]:
        """``(report start in epoch ms, every kill in the report)``.

        One request per report for a whole zone rather than one per report *per
        boss*: the caller filters by encounter, and the identical variables mean the
        cache answers every boss after the first.

        **The report's own start time is not optional context, it is the time base.**
        ``ReportFight.startTime`` is milliseconds *since the report began*, not an
        epoch timestamp, so a fight time used on its own is a number near zero and
        compares as older than everything. Returning the two together is what stops
        them being used apart.
        """
        data = self.query(
            REPORT_KILLS_QUERY,
            {"code": code},
            label=f"report-kills:{code}",
        )
        report = ((data.get("reportData") or {}).get("report")) or {}
        fights = report.get("fights")
        start = report.get("startTime")
        return (
            float(start) if isinstance(start, (int, float)) else 0.0,
            fights if isinstance(fights, list) else [],
        )

    def zone(self, zone_id: int, *, cache: bool = True) -> dict | None:
        """One zone by id, including zones the list does not return.

        ``cache=False`` for a caller asking what the zone is NOW -- its ``frozen``
        flag turns when the next zone opens, and a cached answer would keep saying
        live.
        """
        data = self.query(
            ZONE_BY_ID_QUERY, {"zoneId": zone_id}, label=f"zone:{zone_id}", cache=cache
        )
        return (data.get("worldData") or {}).get("zone")

    def zones(self) -> list[dict]:
        data = self.query(ZONE_QUERY, label="zones")
        return (data.get("worldData") or {}).get("zones") or []

    def encounter_rankings(
        self,
        encounter_id: int,
        difficulty: int = 5,
        metric: str = "dps",
        page: int = 1,
    ) -> dict:
        """Top parses on one encounter, unfiltered by class.

        This is the route from "which boss" to "which logs to read": ranking
        entries carry the report code and fight id of the parse they came from, so
        no report search is needed. It also means the fights analysed are top-end
        pulls, which is a bias worth stating -- see ``fightprobe``.
        """
        data = self.query(
            RANKINGS_QUERY,
            {
                "encounterId": encounter_id,
                "difficulty": difficulty,
                "metric": metric,
                "className": None,
                "specName": None,
                "page": page,
            },
            label=f"rankings:{encounter_id}",
        )
        return ((data.get("worldData") or {}).get("encounter")) or {}

    def spec_rankings(
        self,
        encounter_id: int,
        class_name: str,
        spec_name: str,
        difficulty: int = 5,
        metric: str = "dps",
        page: int = 1,
    ) -> dict:
        """Top parses for one class/spec on one encounter.

        ``characterRankings`` is an untyped JSON scalar in the WCL schema, so the shape
        is whatever the site returns; we read defensively.
        """
        data = self.query(
            RANKINGS_QUERY,
            {
                "encounterId": encounter_id,
                "difficulty": difficulty,
                "metric": metric,
                "className": class_name.replace(" ", ""),
                "specName": spec_name.replace(" ", ""),
                "page": page,
            },
            label=f"rankings:{encounter_id}:{class_name}:{spec_name}",
        )
        encounter = ((data.get("worldData") or {}).get("encounter")) or {}
        return encounter


def _ranking_entries(encounter: dict) -> list[dict]:
    """The ranking rows out of one encounter payload, decoding the JSON scalar."""
    rankings = encounter.get("characterRankings")
    if isinstance(rankings, str):
        rankings = json.loads(rankings)
    if not isinstance(rankings, dict):
        return []
    return [entry for entry in (rankings.get("rankings") or []) if isinstance(entry, dict)]


def _entry_start(entry: dict) -> float:
    """A ranking row's kill start time in epoch ms, wherever WCL put it."""
    for value in (entry.get("startTime"), (entry.get("report") or {}).get("startTime")):
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def select_report_fights(
    encounters: list[dict], limit: int, order: str = "first"
) -> list[tuple[str, int, float]]:
    """``(report code, fight id, kill start in epoch ms)`` for the kills to probe.

    One fight per report.

    One per report on purpose: two parses from the same pull describe the same
    fight, so a sample of five entries could be a sample of one kill.

    ``order`` decides *which* kills:

    * ``top`` keeps the rankings' own order, which is by damage -- the world's best
      pulls, shorter than a typical kill and with adds dying faster. What the probe
      used to take, and the wrong sample for "what does this fight normally look
      like".
    * ``first`` sorts every gathered row by kill start time and takes the earliest.
      The guilds that killed the boss first did it near the enrage, at the intended
      tuning and before gear caught up, so their kills are long and -- crucially --
      alike, which is what makes an aggregate across them mean something. WCL sorts
      rankings by damage and not by date, so this reads the ``startTime`` every row
      already carries and sorts on it; the gather just has to be wide enough to
      contain the early kills, which is why the probe hands several pages in.

    **The width of that gather is the whole constraint, and it is easy to
    misread.** "First" here means *the earliest kills among the pages handed in*,
    and those pages are the highest-damage parses. A guild that killed the boss on
    the first night with a slow, scrappy pull ranks low and can sit far past the
    window, so a narrow gather returns "the earliest of the best" rather than the
    first kills -- a plausible sample, systematically later than the one asked for,
    and invisible in the output unless the dates are published. `killed_between`
    on the observation is what makes it visible; widening ``--rankings-pages`` is
    what fixes it, and ranking pages are cheap next to the per-fight event streams.

    One thing no setting can reach: ``characterRankings`` contains *ranked* parses
    only. A kill logged privately, or one Warcraft Logs declined to rank, is not in
    this list at any depth. That is their rule, not a bound of this project.
    """
    seen: set[str] = set()
    rows: list[tuple[float, str, int]] = []
    for encounter in encounters:
        for entry in _ranking_entries(encounter):
            report = entry.get("report") or {}
            code, fight_id = report.get("code"), report.get("fightID")
            if not isinstance(code, str) or not isinstance(fight_id, int) or code in seen:
                continue
            seen.add(code)
            rows.append((_entry_start(entry), code, fight_id))

    if order == "first":
        # A zero (no timestamp) sorts to the front and would masquerade as the
        # earliest kill, so those go last rather than first.
        rows.sort(key=lambda row: (row[0] == 0.0, row[0]))
    return [(code, fight_id, started) for started, code, fight_id in rows[:limit]]


def _earliest_first(started: float) -> tuple[bool, float]:
    """Sort key that puts a row with NO timestamp last rather than first.

    A ranking row that states no start time arrives as ``0.0``, and read as a date
    that is 1970 -- so it wins every "earliest kill" comparison there is.
    ``select_report_fights`` has carried this rule since it learned to sort by date;
    it has to survive anywhere those triples are re-sorted.
    """
    return (started == 0.0, started)


def merge_kill_selections(
    *selections: Sequence[tuple[str, int, float]], limit: int
) -> list[tuple[str, int, float]]:
    """Fold several already-chosen kill lists into one, earliest first.

    ``--order public`` has two sources for the same encounter and they answer
    different questions: ``characterRankings`` holds only what Warcraft Logs
    *ranked*, while the public-report search reaches a kill nobody ranked. Taking
    one INSTEAD of the other throws away a sample that is already paid for, which
    is how one kill came to stand where thirty-six existed (#164) -- the guard was
    ``if found:``, a truthiness test where a size question was meant.

    One fight per report across ALL of them, so a log both sources found is one
    kill rather than two. Where they disagree about which fight of a report to
    take, the earlier start wins -- that is what ``order="first"`` means -- and
    ties break on the report code so a re-run picks the same kills.

    **This is deliberately not folded into ``select_report_fights``.** That one
    also serves ``order="top"``, which must keep the rankings' own damage order and
    therefore must not sort at all; and it reads encounter *payloads* where these
    are triples that have already been through a selector. Two different inputs and
    one of them must not be sorted is not one function with a flag.
    """
    best: dict[str, tuple[float, int]] = {}
    for selection in selections:
        for code, fight_id, started in selection:
            current = best.get(code)
            if current is None or _earliest_first(started) < _earliest_first(current[0]):
                best[code] = (started, fight_id)
    ordered = sorted(best.items(), key=lambda item: (_earliest_first(item[1][0]), item[0]))
    return [(code, fight_id, started) for code, (started, fight_id) in ordered[:limit]]


def top_report_fights(encounter: dict, limit: int) -> list[tuple[str, int, float]]:
    """Back-compat single-page helper: the highest parses, one fight per report."""
    return select_report_fights([encounter], limit, order="top")


#: Below this many ranked parses a row is not published at all: the median of a
#: handful of logs says nothing about how the spec performs, and putting it next to
#: a simulated number invites a comparison the sample cannot carry.
MIN_SAMPLE = 5

#: Below this many, the 95th percentile is an extrapolation from the single best
#: parse rather than an estimate, so it is omitted rather than guessed. The previous
#: index arithmetic returned the *minimum* at n=2 -- a "95th percentile" below the
#: median, which shipped.
MIN_P95 = 20


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of an already sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def summarise_rankings(encounter: dict) -> dict | None:
    """Reduce a rankings payload to the few numbers we actually compare against."""
    rankings = encounter.get("characterRankings")
    if isinstance(rankings, str):
        rankings = json.loads(rankings)
    if not isinstance(rankings, dict):
        return None

    entries = rankings.get("rankings") or []
    amounts = [
        float(entry["amount"])
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("amount"), (int, float))
    ]
    if not amounts:
        return None

    amounts.sort()
    if len(amounts) < MIN_SAMPLE:
        # A median of two parses is not a distribution, and publishing it beside a
        # simulated figure invites a comparison the data cannot support.
        return None

    return {
        "encounterId": encounter.get("id"),
        "encounterName": encounter.get("name"),
        "sampleSize": len(amounts),
        "median": round(statistics.median(amounts), 1),
        **({"p95": round(_percentile(amounts, 0.95), 1)} if len(amounts) >= MIN_P95 else {}),
        "max": round(amounts[-1], 1),
    }


def cmd_verify(args: argparse.Namespace) -> int:
    """Fetch rankings for every spec in the dataset and write a comparison file.

    **What a pass costs, and why it was UNMEASURED until 2026-09-12.** 26 distinct
    (class, spec) pairs x 8 encounters is **208 ranking queries** every Monday, and
    the client was built with no cache directory, the workflow restored none, no
    bracketing reading was taken, and `RANKINGS_QUERY` -- alone with `ZONE_QUERY`
    among this module's thirteen documents -- carried no `rateLimitData` at all. So
    the ledger ended a whole run at `firstReading None`: the only scheduled,
    points-spending fetcher in the project with no measurement of any kind.

    **`--cache` is for iterating locally, and the workflow deliberately restores
    none.** This is where a ranking differs from a report, and the difference is
    measured rather than argued: `spec_rankings` sends `(encounterId, difficulty,
    metric, className, specName, page)` and nothing in that varies with time, so the
    cache key of a given spec's ranking is **byte-identical week to week**. A report's
    events are immutable, so `fight-probe`'s restored cache is as good as a fresh
    fetch; a *ranking* is the thing this pass exists to re-read, so a restored cache
    would serve last week's medians under this week's `generatedAt` and the weekly
    run would become a no-op that looks like a measurement.

    Within one run the disk cache saves nothing either -- the in-memory `cache` dict
    below already fetches each (class, spec, encounter) exactly once. So the flag's
    whole value is offline: pay for a pass once, then re-run `summarise_rankings`
    and the analysis over it for free.
    """
    # Absent stays absent: `WarcraftLogsClient` coerces a string, and a falsy value
    # must not become `Path(".")` and start caching into the working directory.
    cache_dir = Path(args.cache) if getattr(args, "cache", None) else None
    # The dataset is namespaced by tier; verify whichever tier was asked for, or the
    # current one if not told.
    root = Path(args.data)
    tiers_path = root / "tiers.json"
    if not tiers_path.is_file():
        log.error("no tier index at %s -- run `wowdps build` first", tiers_path)
        return 1

    tier = args.tier
    if not tier or tier == "latest":
        tier = json.loads(tiers_path.read_text(encoding="utf-8"))["current"]

    data_dir = root / tier
    manifest_path = data_dir / "index.json"
    if not manifest_path.is_file():
        log.error("no dataset manifest at %s -- run `wowdps build --tier %s`", manifest_path, tier)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        log.error("%s", exc)
        return 1

    encounter_ids: list[int] = args.encounter or []
    comparisons: list[dict] = []
    # Rankings that exist but are too thin to publish. Counted rather than dropped
    # in silence: "this spec has no comparison" and "this spec has too few logs to
    # compare" are different statements, and the second is the useful one.
    thin = 0

    # Which bosses this tier's numbers are compared against comes from the tier's
    # own fight profiles, not from "the newest zone Warcraft Logs is ranking".
    #
    # Those two are the same raid for most of a season and different raids for the
    # week either side of a turn, which is precisely when somebody runs this. The
    # published MID2 comparison is what that costs: 192 rows of Season 2 sim output
    # against Season 1 kills, under a Season 2 heading, with nothing in the file
    # saying so. `fight_profiles.json` is the one registry of which bosses a season
    # has -- the Fights view already reads it -- so this reads it too and the two
    # views can no longer disagree about what a season is.
    #
    # A tier with no fight profiles is a refusal rather than a fallback. Falling
    # back to the newest live zone is exactly the bug: a raid that has not opened
    # yet has no kills, and comparing against another season's raid instead
    # produces a full set of plausible numbers that answer a question nobody asked.
    if not encounter_ids:
        tier_profiles = fightprofile.load_profiles(tier)
        encounter_ids = sorted(tier_profiles.profiles)
        if not encounter_ids:
            log.error(
                "no fight profiles for tier %s, so there is no boss list to compare "
                "against. Seed one with `wowdps fight-zones --tier %s --seed <zone> "
                "--write`, or pass --encounter to name the bosses explicitly. Not "
                "falling back to the newest ranked zone: before a season opens that "
                "is the *previous* season's raid.",
                tier,
                tier,
            )
            return 1
        log.info(
            "comparing against %s's %d fight profile(s): %s",
            tier,
            len(encounter_ids),
            ", ".join(profile.name for _, profile in sorted(tier_profiles.profiles.items())),
        )

    # Deferred, because `fightprobe` imports this module at module level and the
    # ceiling rule must exist exactly once -- the same reason `harvest` reaches for
    # `fightprobe.check_budget` rather than carrying a second copy.
    from .fightprobe import PointBudgetExhausted, check_budget

    # Rows withheld because the *query* failed, not because the ranking was thin.
    # Counted apart, because the two are different findings and only the second is a
    # fact about the game: `withheldForSmallSample` is read as a statement about how
    # many parses Warcraft Logs holds, and folding a failed query into it makes that
    # number say something nobody measured. The published MID2 file states 358 of
    # them and cannot say whether any were failures.
    #
    # Counted in ROWS, like `thin`, rather than in queries: specs sharing a
    # (class, spec) pair share one query, so a query count and a row count are
    # different units and putting them side by side in one document invites the
    # subtraction that does not work.
    errored = 0
    errored_keys: set[tuple[str, str, int]] = set()
    #: Set when the pass stopped early. The document is all-or-nothing -- one
    #: comparison set covering the whole tier -- so a short one published under the
    #: same name is a floor wearing a measurement's clothes, and nothing is written.
    stopped: str | None = None

    with WarcraftLogsClient(credentials, cache_dir=cache_dir) as client:
        # The bracket. Taken before any ranking is fetched, so `firstReading` is the
        # counter as it stood BEFORE this pass rather than after its first query --
        # which is the one-query lag the ledger's own docstring warns about, and the
        # difference between a total and an estimate.
        #
        # A reading that will not come back is not fatal: a budget reading must never
        # be the thing that kills a pass, and an unmeasured cost is a state the
        # document can express.
        try:
            client.rate_limit()
        except WarcraftLogsError as exc:
            log.warning("could not read the point budget before the pass: %s", exc)

        # One request per spec per encounter. Specs sharing a class/spec pair but
        # differing only in hero talent resolve to the same Warcraft Logs query, so
        # results are cached per (class, spec, encounter).
        cache: dict[tuple[str, str, int], dict | None] = {}

        for spec in manifest.get("specs", []):
            if stopped:
                break
            for encounter_id in encounter_ids:
                key = (spec["class"], spec["spec"], encounter_id)
                if key not in cache:
                    try:
                        # Free: `check_budget` reads the ledger, which every ranking
                        # response now feeds. Before the point ceiling existed here a
                        # pass ran until the service refused, and the refusal was
                        # published -- see the `RateLimited` clause below.
                        #
                        # Read straight off `args`, where `--cache` above goes through
                        # `getattr`. Not an inconsistency: an absent cache means "do
                        # not cache", which is the safe direction, while an absent
                        # ceiling would mean inventing a budget nobody set. A caller
                        # that forgot it should fail loudly.
                        check_budget(client, args.point_ceiling)
                        encounter = client.spec_rankings(
                            encounter_id,
                            spec["class"],
                            spec["spec"],
                            difficulty=args.difficulty,
                            metric=args.metric,
                        )
                        cache[key] = summarise_rankings(encounter)
                    except (RateLimited, PointBudgetExhausted) as exc:
                        # **This is the one that used to publish a wrong document.**
                        # `RateLimited` subclasses `WarcraftLogsError`, so the clause
                        # below caught it, set the summary to None, and the row was
                        # counted as `withheldForSmallSample` -- once per remaining
                        # spec. Measured on 2026-09-12 against the real command with
                        # a stub that 429s halfway: exit 0, half the comparisons, and
                        # the rate limit published as "too few parses". The workflow
                        # then commits that over a good file.
                        #
                        # A budget stop is a fact about the hour, never about a
                        # ranking, so it stops the pass and writes nothing.
                        stopped = str(exc)
                        break
                    except WarcraftLogsError as exc:
                        log.warning("%s %s on %d: %s", *key, exc)
                        cache[key] = None
                        errored_keys.add(key)

                summary = cache[key]
                if not summary:
                    if key in errored_keys:
                        errored += 1
                    else:
                        thin += 1
                    continue

                sim_dps = spec.get("scenarios", {}).get("patchwerk", {}).get("dps", {}).get("1")
                if not sim_dps:
                    continue

                comparisons.append(
                    {
                        "specId": spec["id"],
                        "displayName": spec["displayName"],
                        **summary,
                        "simDps": sim_dps,
                        # >1 means logs beat the sim (external buffs, better gear,
                        # favourable mechanics); <1 means the sim is optimistic.
                        "logsToSimRatio": round(summary["median"] / sim_dps, 4),
                    }
                )

        # The other end of the bracket. Uncached like the first, so the difference
        # is this pass's own cost rather than two halves of different hours.
        try:
            client.rate_limit()
        except WarcraftLogsError as exc:
            log.warning("could not read the point budget after the pass: %s", exc)

        ledger = client.ledger

    log.info("cost: %s over %d quer(y/ies)", spend_sentence(ledger), len(ledger.entries))

    if stopped:
        # Nothing is written. The alternative -- publishing the rows that were read
        # before the budget ran out -- replaces a whole-tier comparison with a
        # partial one under the same name, and the document has no field that could
        # say so. Exit 2 is the ceiling's status throughout this project; the
        # workflow turns it into a warning and commits nothing.
        planned = len(manifest.get("specs", [])) * len(encounter_ids)
        log.error(
            "stopped %d row(s) into a planned %d: %s Nothing was written -- a partial "
            "comparison under the same name is not a smaller measurement, it is a "
            "different one. Re-run when the hour resets.",
            len(comparisons) + thin + errored,
            planned,
            stopped,
        )
        return 2

    output = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "metric": args.metric,
        "difficulty": args.difficulty,
        "note": (
            "Warcraft Logs medians reflect real raids -- movement, mechanics and "
            "imperfect play -- while sims model a stationary target. Divergence is "
            "expected and informative, not an error in either source."
        ),
        "comparisons": comparisons,
        # The readings that make the comparisons worth publishing. Derived from the
        # rows above and nothing else, so `wowdps logs-analyse` can recompute them
        # from a committed file without credentials or a second API pass.
        "analysis": logsanalysis.analyse(comparisons),
        "minSampleSize": MIN_SAMPLE,
        "withheldForSmallSample": thin,
        # Rows the query could not answer for, apart from the thin ones. Always
        # emitted rather than only when non-zero: this block is rebuilt on every run,
        # so an absent key means "written before this existed" and a zero means "no
        # query failed" -- which are different sentences.
        "withheldForQueryError": errored,
        # What the pass cost, read back rather than predicted. This is a reading of
        # the hourly meter taken when the run happened, so five of its fields differ
        # on every run by construction -- anything that ever grows a settle here must
        # exclude it, which is the `_PROVENANCE_PATHS` lesson stated before the trap
        # rather than after it.
        "cost": ledger.to_json(),
    }
    out_path = data_dir / "logs-verification.json"
    out_path.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info(
        "wrote %s (%d comparisons, %d withheld for fewer than %d parses, %d for a failed query)",
        out_path,
        len(comparisons),
        thin,
        MIN_SAMPLE,
        errored,
    )
    return 0
