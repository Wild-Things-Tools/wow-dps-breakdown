"""Which bosses belong to which season, read out of Warcraft Logs' own zone list.

``fight_profiles.json`` is keyed by tier (``MID1``, ``MID2``), and every encounter
under a tier is identified by a **Warcraft Logs** encounter id. Both halves of that
were hand-typed, and one of them was wrong: the nine encounters filed under ``MID2``
are the bosses of the raid that Warcraft Logs had kills for when the file was
written, which in the week before a season turns is the raid that is *ending*. It
is the same trap ``gearpool.identify_tier_raid`` fell into and for the same reason,
one API further out -- see the "Which raid belongs to a tier" note in CLAUDE.md.

The fix is the same shape as the one that worked there: stop typing the list.
``worldData.zones`` names every raid Warcraft Logs knows, its encounters and their
ids, in one query. So a new season's boss list is a fetch rather than an edit, and
the encounter ids come from the service that will be asked about them rather than
from a person reading them off a website.

Two things this module deliberately does not do.

**It does not decide which zone is which tier.** Warcraft Logs has no idea what
``MID2`` means -- that is simc's word for a profile directory, and nothing in the
zone list joins to it. What the zone list does carry is order and a ``frozen``
flag, so "the newest zone still being logged" is derivable and is *offered* as a
suggestion with its evidence. Writing it takes ``--tier`` and ``--zone`` from a
person. A wrong guess here would silently re-label a whole season's fights, which
is exactly the failure being repaired.

**It never overwrites a fact.** Seeding a tier adds the encounters the zone
carries; an encounter already in the file keeps everything asserted about it, name
included. The owner's hand facts outrank a zone listing the same way they outrank a
probe measurement.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Encounter:
    """One boss, as Warcraft Logs names it."""

    encounter_id: int
    name: str


@dataclass(frozen=True)
class Zone:
    """One raid, as Warcraft Logs lists it.

    ``frozen`` is Warcraft Logs' own flag for "rankings are closed here", which is
    what a zone becomes when its season ends. It is the only signal in the payload
    that separates a raid being logged now from one that is finished, so it is what
    the suggestion below rests on.
    """

    zone_id: int
    name: str
    frozen: bool
    encounters: tuple[Encounter, ...]

    @property
    def encounter_ids(self) -> frozenset[int]:
        return frozenset(entry.encounter_id for entry in self.encounters)


def parse_zones(payload: list[dict]) -> list[Zone]:
    """Normalise ``worldData.zones``, preserving the order the service returned.

    That order is load-bearing: Warcraft Logs lists zones oldest first, and it is
    the only thing in the payload that says which of two unfrozen zones is the
    newer. A zone with no encounters is kept rather than dropped -- an announced
    raid whose encounters are not populated yet is a real state on the days either
    side of a season turn, and it is the state a reader most needs to see.
    """
    zones: list[Zone] = []
    for raw in payload or []:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        encounters = tuple(
            Encounter(encounter_id=int(entry["id"]), name=str(entry.get("name") or entry["id"]))
            for entry in (raw.get("encounters") or [])
            if isinstance(entry, dict) and entry.get("id") is not None
        )
        zones.append(
            Zone(
                zone_id=int(raw["id"]),
                name=str(raw.get("name") or raw["id"]),
                frozen=bool(raw.get("frozen")),
                encounters=encounters,
            )
        )
    return zones


@dataclass(frozen=True)
class Placement:
    """Where one tier's currently-filed encounters actually live.

    ``zones`` is a list because a raid can span two of them -- MID2's gear side
    does exactly that on the Blizzard journal (The Venomous Abyss plus The
    Tidebound Grotto), so a single-zone answer would quietly drop whatever the
    second contributes.
    """

    tier: str
    filed: tuple[int, ...]
    zones: tuple[tuple[Zone, int], ...] = ()
    unplaced: tuple[int, ...] = ()

    @property
    def zone_names(self) -> str:
        if not self.zones:
            return "no zone"
        return ", ".join(f"{zone.name} ({hits})" for zone, hits in self.zones)


def locate(tier: str, encounter_ids: list[int], zones: list[Zone]) -> Placement:
    """Which zone(s) the encounter ids already filed under a tier belong to.

    This is the check that catches a mis-filing without anybody having to assert
    the right answer first: the ids in the file are Warcraft Logs ids, so the
    service can say which raid they came from, and a tier whose bosses all sit in
    a frozen zone is a tier describing a season that has ended.
    """
    filed = tuple(sorted(encounter_ids))
    hits: list[tuple[Zone, int]] = []
    placed: set[int] = set()
    for zone in zones:
        overlap = zone.encounter_ids & set(filed)
        if overlap:
            hits.append((zone, len(overlap)))
            placed |= overlap
    hits.sort(key=lambda pair: pair[1], reverse=True)
    return Placement(
        tier=tier,
        filed=filed,
        zones=tuple(hits),
        unplaced=tuple(sorted(set(filed) - placed)),
    )


@dataclass(frozen=True)
class Suggestion:
    """A zone offered for a tier, with the reasoning stated rather than applied."""

    zone: Zone | None
    reason: str


def suggest_current_zone(zones: list[Zone]) -> Suggestion:
    """The newest zone still being logged, which is the current season's raid.

    Offered, never applied. The join from a Warcraft Logs zone to a simc tier name
    does not exist in any payload -- ``MID2`` is a directory in simc's profiles and
    means nothing to Warcraft Logs -- so this is an inference over ordering and the
    ``frozen`` flag, and it is wrong for exactly one day either side of a season
    turn, when the new zone exists and has no kills in it yet.
    """
    live = [zone for zone in zones if not zone.frozen]
    if not live:
        return Suggestion(None, "every zone Warcraft Logs lists is frozen")
    newest = live[-1]
    if len(live) == 1:
        return Suggestion(newest, f"the only zone not frozen is {newest.name}")
    others = ", ".join(zone.name for zone in live[:-1])
    return Suggestion(
        newest,
        f"the newest of {len(live)} unfrozen zones is {newest.name}; the others are {others}",
    )


@dataclass
class SeedResult:
    """What seeding a tier from a zone changed, itemised."""

    tier: str
    zone: Zone
    added: list[Encounter] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    absent: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added)


def seed_tier(raw: dict, tier: str, zone: Zone, difficulty: int = 5) -> SeedResult:
    """Add a zone's encounters to a tier in a decoded ``fight_profiles.json``.

    Mutates ``raw`` and reports what it did. Three rules, all of which exist so a
    re-run is safe:

    - an encounter already filed under the tier is **left exactly as it is**, facts,
      name and all. A zone listing is not evidence about a boss somebody has played;
    - an encounter the tier has and the zone does not is **kept and reported**, not
      deleted. It may be a boss the zone list has not caught up with, and dropping
      it would take its hand facts with it;
    - encounters are written in id order, so the file does not churn on a re-run.
    """
    tiers = raw.setdefault("tiers", {})
    entry = tiers.setdefault(tier, {"difficulty": difficulty, "encounters": []})
    existing = entry.setdefault("encounters", [])
    have = {int(item["encounterId"]) for item in existing if item.get("encounterId") is not None}

    result = SeedResult(tier=tier, zone=zone)
    for encounter in zone.encounters:
        if encounter.encounter_id in have:
            result.kept.append(encounter.encounter_id)
            continue
        existing.append(
            {"encounterId": encounter.encounter_id, "name": encounter.name, "facts": {}}
        )
        result.added.append(encounter)

    result.absent = sorted(have - zone.encounter_ids)
    existing.sort(key=lambda item: int(item["encounterId"]))
    return result


def move_tier(raw: dict, source: str, destination: str) -> int:
    """Re-file a whole tier's encounters under another tier name.

    The repair for a boss list written under the wrong season. It moves the
    encounters *with their facts*, because the facts are about the fight and the
    fight has not changed -- only the label on it was wrong. A destination that
    already carries an encounter keeps its own copy, so this can never overwrite an
    assertion; that encounter is left behind under the source and counted.
    """
    tiers = raw.get("tiers") or {}
    if source not in tiers:
        raise KeyError(f"no tier {source!r} in the profile file")

    entry = tiers[source]
    target = tiers.setdefault(
        destination, {"difficulty": entry.get("difficulty", 5), "encounters": []}
    )
    target.setdefault("encounters", [])
    have = {
        int(item["encounterId"])
        for item in target["encounters"]
        if item.get("encounterId") is not None
    }

    moved = 0
    remaining = []
    for item in entry.get("encounters") or []:
        if int(item["encounterId"]) in have:
            remaining.append(item)
            continue
        target["encounters"].append(item)
        moved += 1

    entry["encounters"] = remaining
    if not remaining:
        tiers.pop(source)
    target["encounters"].sort(key=lambda item: int(item["encounterId"]))
    return moved


def _data_file() -> Path:
    return Path(__file__).with_name("data") / "fight_profiles.json"


def write_profiles(raw: dict, path: Path | None = None) -> Path:
    """Write the profile file back, in the shape it is committed in."""
    target = path or _data_file()
    target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return target
