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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

RANKINGS_QUERY = """
query SpecRankings(
  $encounterId: Int!
  $difficulty: Int!
  $metric: CharacterRankingMetricType!
  $className: String
  $specName: String
  $page: Int!
) {
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

ZONE_QUERY = """
query Zones {
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

    def record(self, label: str, payload: dict, cached: bool = False) -> None:
        data = payload.get("rateLimitData") or {}
        spent = data.get("pointsSpentThisHour")
        if isinstance(spent, (int, float)):
            if self.first_reading is None:
                self.first_reading = float(spent)
            self.last_reading = float(spent)
            self.limit_per_hour = data.get("limitPerHour", self.limit_per_hour)
            self.resets_in = data.get("pointsResetIn", self.resets_in)
        self.entries.append(
            (label, float(spent) if isinstance(spent, (int, float)) else -1.0, cached)
        )

    @property
    def spent(self) -> float | None:
        """Points this run cost, or ``None`` when nothing reported a reading."""
        if self.first_reading is None or self.last_reading is None:
            return None
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
            "queries": len([entry for entry in self.entries if not entry[2]]),
            "cacheHits": len([entry for entry in self.entries if entry[2]]),
            "note": (
                "Points are read back from rateLimitData rather than predicted: "
                "Warcraft Logs does not publish a cost formula. The run total is a "
                "measurement; attributing it to individual queries can lag by one "
                "response."
            ),
        }


class WarcraftLogsClient:
    def __init__(
        self,
        credentials: Credentials,
        timeout: float = 30.0,
        cache_dir: Path | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._token: str | None = None
        self._client = httpx.Client(timeout=timeout)
        #: Responses are cached on disk by (query, variables). Re-running a probe
        #: against the same reports then costs nothing, which is what makes it
        #: safe to iterate on the extraction without burning the hourly budget.
        self._cache_dir = cache_dir
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
        response = self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._credentials.client_id, self._credentials.client_secret),
        )
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

    def query(self, query: str, variables: dict | None = None, label: str = "query") -> dict:
        variables = variables or {}
        cached_at = self._cache_path(query, variables)
        if cached_at and cached_at.is_file():
            payload = json.loads(cached_at.read_text(encoding="utf-8"))
            self.ledger.record(label, payload, cached=True)
            return payload

        token = self._authenticate()
        response = self._client.post(
            API_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 429:
            raise WarcraftLogsError(
                "rate limited by Warcraft Logs: the hourly point budget is spent. "
                "Re-run later; cached responses cost nothing."
            )
        if response.status_code != 200:
            raise WarcraftLogsError(f"query failed ({response.status_code}): {response.text[:300]}")
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
        """The current point budget, as its own query.

        Taken once before and once after a pass, this brackets the whole run: the
        difference is exactly what the pass cost, with no attribution guesswork.
        """
        return (self.query(RATE_LIMIT_QUERY, label="rateLimit").get("rateLimitData")) or {}

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
    ) -> tuple[list[dict], bool]:
        """Every event of one type for one fight, and whether the fetch was cut short.

        Returns ``(events, truncated)``. Truncation is reported rather than
        silently accepted: a target-count timeline built from the first page of a
        long fight would show adds arriving and never leaving.
        """
        collected: list[dict] = []
        cursor = start_ms
        for page in range(max_pages):
            data = self.query(
                EVENTS_QUERY,
                {
                    "code": code,
                    "fightId": fight_id,
                    "dataType": data_type,
                    "hostility": hostility,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
                label=f"events:{data_type}:{code}:{fight_id}:p{page}",
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


def top_report_fights(encounter: dict, limit: int) -> list[tuple[str, int]]:
    """``(report code, fight id)`` for the highest parses, one fight per report.

    One per report on purpose: two parses from the same pull describe the same
    fight, so a sample of five entries could easily be a sample of one kill. The
    order of ``rankings`` is the site's own ranking order, so taking the first
    distinct reports takes the top guilds' pulls.
    """
    rankings = encounter.get("characterRankings")
    if isinstance(rankings, str):
        rankings = json.loads(rankings)
    if not isinstance(rankings, dict):
        return []

    seen: set[str] = set()
    found: list[tuple[str, int]] = []
    for entry in rankings.get("rankings") or []:
        if not isinstance(entry, dict):
            continue
        report = entry.get("report") or {}
        code, fight_id = report.get("code"), report.get("fightID")
        if not isinstance(code, str) or not isinstance(fight_id, int) or code in seen:
            continue
        seen.add(code)
        found.append((code, fight_id))
        if len(found) >= limit:
            break
    return found


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
    """Fetch rankings for every spec in the dataset and write a comparison file."""
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

    with WarcraftLogsClient(credentials) as client:
        if not encounter_ids:
            zones = client.zones()
            live = [z for z in zones if not z.get("frozen")]
            if not live:
                log.error("no unfrozen zone found; pass --encounter explicitly")
                return 1
            newest = live[-1]
            encounter_ids = [e["id"] for e in newest.get("encounters", [])]
            log.info("using zone %s (%d encounters)", newest.get("name"), len(encounter_ids))

        # One request per spec per encounter. Specs sharing a class/spec pair but
        # differing only in hero talent resolve to the same Warcraft Logs query, so
        # results are cached per (class, spec, encounter).
        cache: dict[tuple[str, str, int], dict | None] = {}

        for spec in manifest.get("specs", []):
            for encounter_id in encounter_ids:
                key = (spec["class"], spec["spec"], encounter_id)
                if key not in cache:
                    try:
                        encounter = client.spec_rankings(
                            encounter_id,
                            spec["class"],
                            spec["spec"],
                            difficulty=args.difficulty,
                            metric=args.metric,
                        )
                        cache[key] = summarise_rankings(encounter)
                    except WarcraftLogsError as exc:
                        log.warning("%s %s on %d: %s", *key, exc)
                        cache[key] = None

                summary = cache[key]
                if not summary:
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
        "minSampleSize": MIN_SAMPLE,
        "withheldForSmallSample": thin,
    }
    out_path = data_dir / "logs-verification.json"
    out_path.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info(
        "wrote %s (%d comparisons, %d withheld for fewer than %d parses)",
        out_path,
        len(comparisons),
        thin,
        MIN_SAMPLE,
    )
    return 0
