"""One key space for every Warcraft Logs fetch, instead of four response caches.

The key is ``(kind, natural key, schema version)`` -- **never the query text**.

That sentence is the whole module. Today's cache keys on
``sha256(json({q: document, v: variables}))`` (`warcraftlogs._cache_path`), which
binds an entry to the *command* rather than to the *thing*, and every measured
duplication in `docs/warcraftlogs-konzept.md` follows from it: two documents that
ask the same question of the same encounter are two entries, and a document whose
text is reformatted is a cold cache.

The payoff is **not** points. It is being able to change an extraction without
paying for the fetches again, and -- the half that is immediate -- letting a lean
document be answered out of a rich one.

## What an entry carries, and why each field is there

``schemaVersion``
    Bumped when the *envelope* changes. An entry from an older schema is a miss,
    which is how a rule change (a different TTL, a different variant vocabulary)
    is retired rather than silently reinterpreted.

``variant``
    The field groups this entry's document actually selected. A read is a hit only
    when what it needs is a **subset** of what the entry carries. That is what
    makes `ENCOUNTER_NAME_QUERY` answerable out of an `ENCOUNTER_ZONE_QUERY`
    payload: both write `worldData.encounter.name` in the same place, and the zone
    document selects strictly more. It is also the `filterVersion` of the concept
    under a name that says what it holds -- a filtered field and a field the API
    never sent are indistinguishable downstream, so the entry has to state which
    it is.

``budget``
    What the fetch behind this entry was *willing to read*. A page-limited answer
    is a **prefix that looks like a complete response**, which this repository has
    paid for twice (`eventBudget`, `searchBudget`). An entry whose budget is below
    the one now asked for is a miss. **Unknown is not zero**: an entry with no
    budget cannot answer a request that states one.

``expiresAt``
    Absolute, computed at write time from the caller's TTL rather than re-derived
    at read time. An entry then keeps the rule it was written under, and changing
    the rule is a `schemaVersion` bump rather than a silent reinterpretation of
    everything already on disk. ``None`` means immutable: a report's events do not
    change.

    The one kind that is *not* immutable and looks like it is: ``encounter/<id>``.
    `frozen` turns when the next zone opens -- `warcraftlogs.zone` already takes a
    `cache=False` for exactly that -- and the PTR/live twin answer hangs on
    `has_ranked_parses`, which flips at a season start. A frozen entry there would
    read the previous season for ever, which is the error this project has made
    twice ("The nine bosses filed under MID2 are last season's raid").

## What this module deliberately does NOT do

- **It does not filter.** The stored value is the raw GraphQL ``data`` block, byte
  for byte what the response carried. Filtering before caching turns a response
  cache into an extract, and then a removed field cannot be told from a field the
  API never sent -- the failure this project has paid for four times
  (`hostilityType`, `includeResources`, `zoneID: 0`, `includeCombatantInfo`).
  Keeping personal data out of what gets **published** is the artifact exclusion's
  job, one layer up, and it is done.
- **It does not replace `is_complete`.** Whether an encounter is touched at all is
  decided against the payload before a query is sent, and that stays -- otherwise a
  `schemaVersion` bump produces a miss nobody ever fills, because `is_complete`
  goes on saying "done".
- **It is not a new medium.** The backing store is the same directory the response
  cache already uses. Legacy entries are 32-hex files at its root; store entries
  live under a kind directory, so the two cannot collide and a restored cache keeps
  serving whatever has not moved over yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

#: Bumped when the ENVELOPE changes -- the fields below, their meaning, or the
#: variant vocabulary. Not when a document's text changes: that is the whole point
#: of keying on the thing rather than on the command.
SCHEMA_VERSION = 1

#: How long an `encounter/<id>` answer may be reused, by the state it reported.
#:
#: The two figures are `progresssweep.skip_reason`'s, and they are its figures
#: because they answer the same question: how long is it safe to believe what a
#: zone said about itself. A frozen zone has stopped moving in every way that
#: matters here; a live one has not.
FROZEN_TTL = timedelta(hours=168)
LIVE_TTL = timedelta(hours=6)

#: Everything else. `None` is not "a long time" -- it is a claim that the thing
#: cannot change, and it may only be used where that holds: a report's fights, its
#: events, its tables. A report can be deleted or made private, which is a miss of
#: a different kind and not something a TTL repairs.
IMMUTABLE: timedelta | None = None

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _safe_part(part: str) -> str:
    """One path segment, or a hashed stand-in when the input is not one.

    A report code is alphanumeric and an id is an integer, so in practice nothing
    is rewritten. The stand-in exists so a surprising input -- a code with a slash,
    an empty string -- cannot escape the directory or collide with a neighbour: the
    hash is of the ORIGINAL, so two different surprising inputs stay different.
    """
    text = str(part)
    if _SAFE.fullmatch(text) and ".." not in text:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"_{digest}"


@dataclass(frozen=True)
class Key:
    """What is being asked for, as a thing rather than as a command.

    ``parts`` is the natural key with its kind in front, e.g.
    ``("encounter", "3421")`` or ``("report", "abc", "f", "12", "table",
    "DamageDone", "Source")``. ``variant`` is what the caller needs the entry to
    carry; ``budget`` what it is willing to have read.
    """

    parts: tuple[str, ...]
    variant: frozenset[str] = frozenset()
    budget: int | None = None

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("a store key needs at least a kind")

    @property
    def kind(self) -> str:
        return self.parts[0]

    def path(self) -> Path:
        """The relative file this key names.

        The variant is NOT in the path, and that is the point: one encounter has
        one entry, which a richer document may widen. Putting the variant in the
        path would reproduce the defect this module replaces, one level up.
        """
        segments = [_safe_part(p) for p in self.parts]
        segments[-1] += ".json"
        return Path(*segments)


@dataclass
class Entry:
    """A stored answer and the four things that decide whether it may be used."""

    data: dict
    variant: frozenset[str]
    budget: int | None
    fetched_at: datetime
    expires_at: datetime | None

    def satisfies(self, key: Key, *, now: datetime) -> str | None:
        """``None`` when this entry answers ``key``, else why it does not.

        A sentence rather than a boolean, because a cold pass has to be explicable:
        "the schema moved" and "the budget was smaller" are different findings and
        only one of them is a reason to change anything.
        """
        if self.expires_at is not None and now >= self.expires_at:
            return "expired"
        if not key.variant <= self.variant:
            return "variant"
        if key.budget is not None and (self.budget is None or self.budget < key.budget):
            # Unknown is not zero and it is not enough either: an entry that does
            # not say how much it read cannot answer a caller that says how much it
            # needs. The safe direction is to pay again.
            return "budget"
        return None


@dataclass
class WclStore:
    """The key space, backed by a directory of JSON files.

    ``root`` is the same directory the response cache already uses. Nothing is
    held in memory between calls: a store is cheap to construct and two of them
    over one directory agree, which is what lets a test drive it without a client.
    """

    root: Path
    misses: Counter = field(default_factory=Counter)
    hits: int = 0

    def get(self, key: Key, *, now: datetime | None = None) -> dict | None:
        """The stored ``data`` for ``key``, or ``None`` with the reason counted."""
        now = now or datetime.now(UTC)
        path = self.root / key.path()
        if not path.is_file():
            self.misses[f"{key.kind}:absent"] += 1
            return None
        entry = self._read(path)
        if entry is None:
            self.misses[f"{key.kind}:unreadable"] += 1
            return None
        reason = entry.satisfies(key, now=now)
        if reason:
            self.misses[f"{key.kind}:{reason}"] += 1
            return None
        self.hits += 1
        return entry.data

    def put(
        self,
        key: Key,
        data: dict,
        *,
        ttl: timedelta | None,
        now: datetime | None = None,
    ) -> None:
        """Record ``data`` under ``key``.

        ``ttl`` is required rather than defaulted, because the two answers --
        "this cannot change" and "this changes and nobody has said how fast" --
        are not the same and a default would pick one of them silently. Pass
        ``IMMUTABLE`` to mean the first.

        **A narrower write is decided by immutability, and that is the one rule
        here that is not obvious.** Two payloads are never merged -- that is the
        "a filtered field and a field the API never sent look alike" trap, and it
        is the reason this store holds raw ``data`` at all. So a write whose
        variant is *narrower* than what is already on disk has to pick one:

        - on an **immutable** key the wider entry is kept and the write skipped.
          Older is as good as newer by definition, so throwing away a paid-for
          field to store a fresher copy of a subset of it is a pure loss. This is
          the talent-code case: a fight asked for twelve actors once and two
          later.
        - on a **mutable** key the new payload replaces it, narrower variant and
          all. An entry may never claim to carry a field its payload does not
          have, and the older *value* is the thing in question there. The cost is
          one re-fetch when somebody next wants the wider variant, which is the
          right direction to be wrong in.
        """
        now = now or datetime.now(UTC)
        path = self.root / key.path()
        variant = key.variant
        budget = key.budget
        existing = self._read(path) if path.is_file() else None
        if existing is not None and existing.variant - variant:
            if ttl is None:
                log.debug(
                    "store: keeping the wider %s (%s covers %s)",
                    "/".join(key.parts),
                    sorted(existing.variant),
                    sorted(variant),
                )
                return
            log.debug(
                "store: %s rewritten narrower (%s -> %s)",
                "/".join(key.parts),
                sorted(existing.variant),
                sorted(variant),
            )
        elif existing is not None:
            variant = variant | existing.variant
            if existing.budget is not None and budget is not None:
                budget = max(budget, existing.budget)

        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "kind": key.kind,
            "key": list(key.parts),
            "variant": sorted(variant),
            "budget": budget,
            "fetchedAt": now.isoformat(),
            "expiresAt": (now + ttl).isoformat() if ttl is not None else None,
            "data": data,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written through a temporary file in the same directory: a run killed
        # mid-write otherwise leaves half a JSON document that reads as a corrupt
        # entry for ever, and `_read` would count it as `unreadable` on every
        # future pass rather than as the absent entry it should be.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    def describe(self) -> str:
        """One line for a run's log: what the store served and what it could not."""
        if not self.hits and not self.misses:
            return "store: nothing asked"
        parts = [f"{self.hits} hit(s)"]
        for reason, count in sorted(self.misses.items()):
            parts.append(f"{reason} {count}")
        return "store: " + ", ".join(parts)

    def _read(self, path: Path) -> Entry | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        fetched = _parse_time(payload.get("fetchedAt"))
        if fetched is None:
            return None
        return Entry(
            data=data,
            variant=frozenset(payload.get("variant") or ()),
            budget=payload.get("budget") if isinstance(payload.get("budget"), int) else None,
            fetched_at=fetched,
            expires_at=_parse_time(payload.get("expiresAt")),
        )


def _parse_time(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive stamp compares as UTC rather than raising: an entry written by an
    # older build is worth reading, and guessing the local zone would be worse than
    # either reading it or dropping it.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def encounter_key(encounter_id: int, *, variant: frozenset[str]) -> Key:
    """``encounter/<id>`` -- name, zone and `frozen`, each named in the variant."""
    return Key(("encounter", str(encounter_id)), variant=variant)


def encounter_ttl(frozen: object) -> timedelta:
    """How long an encounter answer may be reused, by what it said about its zone.

    A payload that does not state `frozen` gets the LIVE figure, which is the
    shorter one: unknown is not frozen, and the cost of being wrong that way is one
    re-fetch rather than a season of reading the wrong raid.
    """
    return FROZEN_TTL if frozen is True else LIVE_TTL


def report_key(code: str, *rest: object, variant: frozenset[str] = frozenset()) -> Key:
    """``report/<code>/...`` -- everything scoped to one uploaded log."""
    return Key(("report", code, *(str(part) for part in rest)), variant=variant)


def zone_key(zone_id: int, *rest: object, variant: frozenset[str] = frozenset()) -> Key:
    """``zone/<id>/...`` -- the zone itself, and the report windows under it."""
    return Key(("zone", str(zone_id), *(str(part) for part in rest)), variant=variant)


def rankings_key(
    encounter_id: int,
    *,
    difficulty: int,
    metric: str,
    page: int,
    spec: tuple[str, str] | None = None,
) -> Key:
    """A ranking page, which is the one kind that is neither immutable nor a fact.

    It is keyed like the others and stored with a TTL, because a ranking is exactly
    what a re-read is for -- `wowdps verify` exists to re-read one. What the key
    buys here is that two *documents* asking for the same page share an entry.
    """
    parts: tuple[str, ...] = ("encounter", str(encounter_id), "rankings", metric, f"d{difficulty}")
    if spec is not None:
        parts += (f"{spec[0]}-{spec[1]}",)
    return Key((*parts, f"p{page}"))
