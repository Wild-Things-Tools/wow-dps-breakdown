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

The public client endpoint is rate limited to 3600 points per hour, and a rankings
query costs well under a point per call, so a nightly verification pass over a raid
tier is comfortably inside budget.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
from dataclasses import dataclass
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


class WarcraftLogsClient:
    def __init__(self, credentials: Credentials, timeout: float = 30.0) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._token: str | None = None
        self._client = httpx.Client(timeout=timeout)

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

    def query(self, query: str, variables: dict | None = None) -> dict:
        token = self._authenticate()
        response = self._client.post(
            API_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 429:
            raise WarcraftLogsError("rate limited by Warcraft Logs (3600 points/hour)")
        if response.status_code != 200:
            raise WarcraftLogsError(f"query failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            raise WarcraftLogsError(f"GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    def zones(self) -> list[dict]:
        data = self.query(ZONE_QUERY)
        return (data.get("worldData") or {}).get("zones") or []

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
        )
        encounter = ((data.get("worldData") or {}).get("encounter")) or {}
        return encounter


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
    return {
        "encounterId": encounter.get("id"),
        "encounterName": encounter.get("name"),
        "sampleSize": len(amounts),
        "median": round(statistics.median(amounts), 1),
        "p95": round(amounts[int(len(amounts) * 0.95) - 1], 1),
        "max": round(amounts[-1], 1),
    }


def cmd_verify(args: argparse.Namespace) -> int:
    """Fetch rankings for every spec in the dataset and write a comparison file."""
    data_dir = Path(args.data)
    manifest_path = data_dir / "index.json"
    if not manifest_path.is_file():
        log.error("no dataset manifest at %s -- run `wowdps build` first", manifest_path)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        log.error("%s", exc)
        return 1

    encounter_ids: list[int] = args.encounter or []
    comparisons: list[dict] = []

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
    }
    out_path = data_dir / "logs-verification.json"
    out_path.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")
    log.info("wrote %s (%d comparisons)", out_path, len(comparisons))
    return 0
