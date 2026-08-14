"""Blizzard Game Data API client: the one source that knows where an item drops.

simc ships no item source of any kind -- no journal, no instance, no encounter --
which is documented at length in ``equipment.py`` and in CLAUDE.md. Blizzard's own
Game Data API ships exactly that: ``journal-encounter/{id}`` returns the items an
encounter drops, which is the ``JournalEncounterItem`` join simc's *extraction*
toolchain reads and its *shipped* data throws away. So the two facts the loot sweep
was asserting by hand -- which pool an item belongs to, and which dungeons this
Mythic+ season runs -- become derivable.

Credentials are OAuth2 client credentials, free from https://develop.battle.net::

    BLIZZARD_CLIENT_ID=...
    BLIZZARD_CLIENT_SECRET=...

Namespace and locale
--------------------
Every Game Data request needs a namespace, and picking the wrong one is a 404 rather
than a wrong answer, which is at least loud. Two are used here:

* ``static-{region}`` for the journal (expansions, instances, encounters). Static
  data changes only when the game patches. The API answers ``static-us`` with
  whatever build is live -- responses echo back a versioned form such as
  ``static-11.2.7_64397-us`` -- so pinning a build is neither necessary nor wise.
* ``dynamic-{region}`` for Mythic Keystone seasons, dungeons and leaderboards.
  These change within a patch, which is the whole reason the season rotation is
  worth deriving.

Locale is ``en_US`` by default and that is not cosmetic: item and instance names are
the join key against simc's English item table, and against a pool file written in
English. A German locale would return names that match nothing.

Rate limits, unlike Warcraft Logs', are published
-------------------------------------------------
36,000 requests an hour and 100 a second per client. That is a *request* count, not
an opaque point budget, so a pass can be predicted rather than measured -- but the
ledger still counts what actually went out, because a prediction that is never
checked is a belief. The client paces itself well under the per-second cap and
retries a 429 with the server's own ``Retry-After``.

Everything is cached on disk by (path, params), the same way the Warcraft Logs
client caches by (query, variables): a full walk costs about 1,500 requests, and
iterating on the extraction over a warm cache costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.battle.net/token"

#: Per-client caps Blizzard publishes. Held here so the ledger can say how much of
#: the budget a pass used rather than leaving the reader to look it up.
REQUESTS_PER_HOUR = 36_000
REQUESTS_PER_SECOND = 100

#: What the client actually paces itself at. A fifth of the cap: a full walk still
#: finishes in a couple of minutes, and nothing about this job is urgent enough to
#: sit on the limit and find out how Blizzard measures a second.
DEFAULT_RATE = 20.0

#: Regions that serve the retail Game Data API. ``us`` by default because the
#: locale that goes with it is the one whose item names match simc's.
REGIONS = ("us", "eu", "kr", "tw")

_ID_IN_HREF = re.compile(r"/([a-z-]+)/(\d+)(?:\?|$)")


class BlizzardError(RuntimeError):
    pass


class RequestBudgetExhausted(RuntimeError):
    """Raised to end a pass cleanly when ``--max-requests`` is reached."""


@dataclass
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> Credentials:
        client_id = os.environ.get("BLIZZARD_CLIENT_ID")
        client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise BlizzardError(
                "BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET must be set. Create a "
                "client for free at https://develop.battle.net/access/clients"
            )
        return cls(client_id=client_id, client_secret=client_secret)


@dataclass(frozen=True)
class Failure:
    """One request that did not come back, kept rather than raised.

    A missing encounter must not end a walk of fifteen hundred of them, but it must
    not vanish either: an item that goes unplaced because its encounter 404'd is a
    different statement from one that no encounter drops, and only the ledger can
    tell them apart.
    """

    path: str
    status: int
    detail: str


@dataclass
class RequestLedger:
    """What the pass actually cost, in the unit Blizzard meters.

    Warcraft Logs needs its cost measured because the cost function is unpublished.
    Here it is published, so this is a check rather than a discovery -- but it is
    still counted, because "about fifteen hundred requests" is an estimate and this
    is a number.
    """

    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    by_endpoint: Counter[str] = field(default_factory=Counter)
    failures: list[Failure] = field(default_factory=list)

    def record(self, endpoint: str, cached: bool) -> None:
        self.by_endpoint[endpoint] += 1
        if cached:
            self.cache_hits += 1
        else:
            self.requests += 1

    def fail(self, path: str, status: int, detail: str) -> None:
        self.failures.append(Failure(path=path, status=status, detail=detail))

    @property
    def hourly_share(self) -> float:
        return self.requests / REQUESTS_PER_HOUR

    def to_json(self) -> dict:
        return {
            "requests": self.requests,
            "cacheHits": self.cache_hits,
            "retries": self.retries,
            "byEndpoint": dict(sorted(self.by_endpoint.items())),
            "limitPerHour": REQUESTS_PER_HOUR,
            "limitPerSecond": REQUESTS_PER_SECOND,
            "shareOfHourlyLimit": round(self.hourly_share, 4),
            "failures": [
                {"path": f.path, "status": f.status, "detail": f.detail} for f in self.failures
            ],
            "note": (
                "Blizzard meters requests, not points, and publishes the caps "
                f"({REQUESTS_PER_HOUR}/hour, {REQUESTS_PER_SECOND}/second). Cache hits "
                "cost nothing and are counted separately."
            ),
        }


def id_from_href(href: str | None, expect: str | None = None) -> int | None:
    """The numeric id at the end of a Game Data ``key.href``.

    Worth doing rather than reading the sibling ``id`` field: a keystone dungeon's
    ``dungeon`` node carries both an ``id`` and an href pointing at
    ``/data/wow/journal-instance/1303``, and only the href says *which kind* of id
    it is. ``expect`` asserts that, so a schema change that repoints the link at
    something else is a missing rotation entry in the report rather than a silently
    wrong join.
    """
    if not href:
        return None
    match = _ID_IN_HREF.search(href)
    if not match:
        return None
    kind, value = match.group(1), match.group(2)
    if expect and kind != expect:
        return None
    return int(value)


class BlizzardClient:
    """Thin typed surface over the endpoints this project needs.

    Deliberately not a general client. Each method is one endpoint with its
    namespace already correct, because the namespace is the part that is easy to get
    wrong and impossible to get wrong twice in the same place.
    """

    def __init__(
        self,
        credentials: Credentials,
        region: str = "us",
        locale: str = "en_US",
        timeout: float = 30.0,
        cache_dir: Path | None = None,
        rate_per_second: float = DEFAULT_RATE,
        max_requests: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if region not in REGIONS:
            raise BlizzardError(f"unknown region {region!r}; expected one of {', '.join(REGIONS)}")
        self.region = region
        self.locale = locale
        self._credentials = credentials
        self._token: str | None = None
        # ``transport`` exists for the tests. Credentials are Actions secrets, so a
        # development checkout cannot reach the API at all, and the namespace and
        # parameter handling -- the parts that are easy to get wrong -- would
        # otherwise be exercised only in CI.
        self._client = httpx.Client(
            timeout=timeout,
            base_url=f"https://{region}.api.blizzard.com",
            transport=transport,
        )
        self._cache_dir = cache_dir
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._last_request = 0.0
        #: A ceiling so a mistake costs a slice of the hour's budget rather than all
        #: of it. ``None`` means the published limit is the only limit.
        self._max_requests = max_requests
        self.ledger = RequestLedger()

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> BlizzardClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def static_namespace(self) -> str:
        return f"static-{self.region}"

    @property
    def dynamic_namespace(self) -> str:
        return f"dynamic-{self.region}"

    # -- plumbing ----------------------------------------------------------------

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        response = self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._credentials.client_id, self._credentials.client_secret),
        )
        if response.status_code != 200:
            raise BlizzardError(
                f"token request failed ({response.status_code}): {response.text[:200]}"
            )
        token = response.json().get("access_token")
        if not token:
            raise BlizzardError("token response contained no access_token")
        self._token = token
        return token

    def _cache_path(self, path: str, params: dict) -> Path | None:
        if not self._cache_dir:
            return None
        digest = hashlib.sha256(
            json.dumps({"p": path, "q": params}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        return self._cache_dir / f"{digest}.json"

    def _pace(self) -> None:
        if not self._interval:
            return
        wait = self._interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path: str, namespace: str, endpoint: str | None = None) -> dict | None:
        """One GET, cached, paced, and forgiving of a 404.

        Returns ``None`` when the resource is not there, rather than raising: a walk
        over every encounter in the game will meet ids the index lists and the
        detail endpoint does not serve, and one of those must not cost the pass. The
        failure lands in the ledger either way.
        """
        endpoint = endpoint or _endpoint_label(path)
        params = {"namespace": namespace, "locale": self.locale}

        cached_at = self._cache_path(path, params)
        if cached_at and cached_at.is_file():
            self.ledger.record(endpoint, cached=True)
            return json.loads(cached_at.read_text(encoding="utf-8"))

        if self._max_requests is not None and self.ledger.requests >= self._max_requests:
            raise RequestBudgetExhausted(
                f"stopping at {self.ledger.requests} requests (--max-requests); "
                f"whatever has been collected is still reported"
            )

        payload = self._fetch(path, params, endpoint)
        if payload is None:
            return None

        if cached_at:
            cached_at.parent.mkdir(parents=True, exist_ok=True)
            cached_at.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _fetch(self, path: str, params: dict, endpoint: str, attempt: int = 0) -> dict | None:
        token = self._authenticate()
        self._pace()
        response = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {token}"}
        )
        self.ledger.record(endpoint, cached=False)

        if response.status_code == 429 and attempt < 3:
            # Blizzard publishes the caps, so a 429 means the pacing is wrong rather
            # than that the budget is a mystery. Honour the server's own wait.
            delay = _retry_after(response) or (2.0**attempt)
            log.warning("rate limited on %s; waiting %.1fs", path, delay)
            self.ledger.retries += 1
            time.sleep(delay)
            return self._fetch(path, params, endpoint, attempt + 1)

        if response.status_code == 401 and attempt < 1:
            # Tokens last a day; a pass that outlives one re-authenticates once.
            self._token = None
            return self._fetch(path, params, endpoint, attempt + 1)

        if response.status_code == 404:
            self.ledger.fail(path, 404, "not found")
            log.debug("404 for %s", path)
            return None

        if response.status_code != 200:
            self.ledger.fail(path, response.status_code, response.text[:200])
            raise BlizzardError(
                f"GET {path} failed ({response.status_code}): {response.text[:200]}"
            )

        return response.json()

    # -- journal (static namespace) ----------------------------------------------

    def journal_expansion_index(self) -> list[dict]:
        """Every expansion the dungeon journal knows.

        The wire field is ``tiers``, not ``expansions`` -- a name worth writing down
        because guessing it costs a round trip and a puzzled minute.
        """
        payload = self.get("/data/wow/journal-expansion/index", self.static_namespace) or {}
        return [t for t in (payload.get("tiers") or payload.get("expansions") or []) if t]

    def journal_expansion(self, expansion_id: int) -> dict | None:
        """One expansion, with its ``raids`` and ``dungeons`` already separated.

        This is the cheap half of the raid/dungeon split: fifteen requests classify
        every instance in the game, where fetching the instances themselves would be
        a thousand.
        """
        return self.get(f"/data/wow/journal-expansion/{expansion_id}", self.static_namespace)

    def journal_instance(self, instance_id: int) -> dict | None:
        return self.get(f"/data/wow/journal-instance/{instance_id}", self.static_namespace)

    def journal_encounter_index(self) -> list[dict]:
        payload = self.get("/data/wow/journal-encounter/index", self.static_namespace) or {}
        return [e for e in (payload.get("encounters") or []) if e]

    def journal_encounter(self, encounter_id: int) -> dict | None:
        """One boss, and -- the entire point of this module -- the items it drops."""
        return self.get(f"/data/wow/journal-encounter/{encounter_id}", self.static_namespace)

    # -- mythic keystone (dynamic namespace) -------------------------------------

    def mythic_keystone_season_index(self) -> dict:
        """Seasons, and ``current_season`` -- which is what makes this season-proof.

        Resolving the current season from the API rather than pinning an id is the
        difference between a job that survives a new season and one that needs a
        human on the day it starts.
        """
        return self.get("/data/wow/mythic-keystone/season/index", self.dynamic_namespace) or {}

    def mythic_keystone_season(self, season_id: int) -> dict | None:
        return self.get(f"/data/wow/mythic-keystone/season/{season_id}", self.dynamic_namespace)

    def mythic_keystone_dungeon(self, dungeon_id: int) -> dict | None:
        """One keystone dungeon, whose ``dungeon`` link points at a journal instance.

        That link is the join nothing else provides: keystone dungeon ids are
        challenge-mode ids and share nothing with journal instance ids.
        """
        return self.get(f"/data/wow/mythic-keystone/dungeon/{dungeon_id}", self.dynamic_namespace)

    def connected_realm_index(self) -> list[int]:
        payload = self.get("/data/wow/connected-realm/index", self.dynamic_namespace) or {}
        ids = [
            id_from_href(
                (realm.get("href") if isinstance(realm, dict) else None), "connected-realm"
            )
            for realm in (payload.get("connected_realms") or [])
        ]
        return sorted(realm_id for realm_id in ids if realm_id is not None)

    def mythic_leaderboard_index(self, connected_realm_id: int) -> list[dict]:
        """``current_leaderboards`` -- the dungeons this season actually runs.

        The season endpoint does *not* carry a dungeon list; it carries periods and a
        name. The leaderboard index does, because a leaderboard exists for exactly
        the dungeons the current season is running. It is per connected realm, but
        the rotation is region-wide, so any realm answers for all of them.
        """
        payload = (
            self.get(
                f"/data/wow/connected-realm/{connected_realm_id}/mythic-leaderboard/index",
                self.dynamic_namespace,
            )
            or {}
        )
        return [entry for entry in (payload.get("current_leaderboards") or []) if entry]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _endpoint_label(path: str) -> str:
    """``/data/wow/journal-encounter/123`` -> ``journal-encounter``, for the ledger."""
    parts = [part for part in path.split("/") if part]
    return parts[2] if len(parts) > 2 else path
