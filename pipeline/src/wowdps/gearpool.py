"""Build a slot's item pool from Blizzard's journal, rather than inferring it.

``equipment.py`` documents the inference this module exists to replace: the pool
file's trinkets were selected structurally -- "epic trinkets at base item level 219
are the raid's, rare-base ones at 108 are the dungeons'" -- because simc ships no
drop source for anything. That rule is a proxy for the wrong thing in *both*
directions, and both directions were measured against simc 1210-01 on
2026-08-15:

- **It over-collects.** All twenty-five Midnight dungeon trinkets share one base
  item level and one quality, whether or not their dungeon runs this season. The
  three the owner named as last season's -- Emberwing Feather (250144),
  Soulcatcher's Charm (250223), Heart of Wind (250256) -- are field-for-field
  identical to the ones that are current: ``ilevel 108, quality 3, req_level 78``,
  same flag words, same everything. **Nothing in simc's item table separates
  them**, so no cleverer heuristic over that table can either.
- **It under-collects.** Midnight Season 2's rotation runs three dungeons from
  older expansions (Kings' Rest, Temple of Sethraliss, Ruby Life Pools). Their
  trinkets carry ids and base item levels from a different block entirely, so a
  rule keyed on this expansion's numbers misses every one of them -- three of the
  eight farmable dungeons contributing nothing to a pool that claims to be what
  the character can obtain.

The fix is not a longer list of exceptions. A blacklist answers one item at a time,
goes stale the moment a season turns, and cannot ever solve the second failure --
you cannot name your way *into* a pool. What is needed is the fact itself: which
encounter drops this item, in which instance, and does that instance run this
season. Blizzard publishes all three, and ``lootsources.py`` already reads them.

So this module joins the two halves that were never joined:

    simc's item table  --  what an item *is* (stats, slug, base item level)
    Blizzard's journal --  where an item *comes from* (encounter, instance, kind)

and the pool is the intersection, per season:

    raid pool         items dropped by the encounters of *this tier's raid*
    mythicplus pool   items dropped by the encounters of *this season's rotation*

Neither half is asserted. The raid is not a hard-coded instance id -- it is found
by matching the tier's own boss list (``fight_profiles.json``, the same names the
Fights view publishes) against the journal's encounter names, so a new tier needs
no edit here. The rotation comes from ``Rotation``, which ``lootsources.derive_rotation``
reads out of the season's own leaderboards.

What this module deliberately does *not* do is guess. An item simc has never heard
of cannot be simulated and is skipped; an item the journal never placed is reported
as unplaced rather than quietly kept or quietly dropped; and a walk that stopped
early is refused outright, because "no encounter drops this" and "the walk never got
there" are different sentences that must not produce the same pool.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .equipment import DiscoveredItem, GearItem
from .lootsources import ItemDrop, LootIndex, Rotation

#: How much of a tier's boss list an instance has to account for before it is
#: accepted as that tier's raid. A raid whose encounters match half the tier's
#: bosses is the tier's raid with some names spelled differently; one that matches
#: two of nine is a coincidence, and taking it would build the candidate pool out of
#: the wrong raid entirely -- the single most damaging way this could fail, because
#: the result would look perfectly well-formed.
MIN_RAID_ENCOUNTER_MATCH = 0.5


def _normalise(name: str) -> str:
    """Fold a boss or instance name to something two sources can agree on.

    Warcraft Logs and the Encounter Journal name the same boss slightly differently
    -- ``&`` against ``and``, an epithet present in one and absent in the other --
    so the comparison is made on letters and digits alone.
    """
    return re.sub(r"[^a-z0-9]+", "", name.lower().replace("&", "and"))


def _names_agree(left: str, right: str) -> bool:
    """Do these two names refer to the same encounter?

    Containment rather than equality, because one source routinely carries an
    epithet the other drops (``Chimaerus`` against ``Chimaerus, the Undreamt God``).
    The four-character floor keeps a short name from matching most of the game.
    """
    a, b = _normalise(left), _normalise(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a in b or b in a)


@dataclass(frozen=True)
class RaidMatch:
    """The journal instance identified as one tier's raid, and how well it fits."""

    instance_id: int
    instance: str
    expansion: str | None
    #: Tier boss names this instance accounted for, and the ones it did not. The
    #: second is published rather than swallowed: a raid that matches seven of nine
    #: is almost certainly right *and* is telling you two names have drifted.
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def share(self) -> float:
        total = len(self.matched) + len(self.missing)
        return len(self.matched) / total if total else 0.0

    def to_json(self) -> dict:
        return {
            "instanceId": self.instance_id,
            "instance": self.instance,
            "expansion": self.expansion,
            "matched": list(self.matched),
            "missing": list(self.missing),
            "share": round(self.share, 3),
        }


def identify_tier_raid(index: LootIndex, encounter_names: Sequence[str]) -> RaidMatch | None:
    """Which journal instance is the raid this tier's boss list describes?

    Derived rather than configured, so that adding a tier to the project means
    adding its bosses (which the Fights view needs anyway) and nothing else. The
    encounters are read back out of the loot index -- an instance is known here by
    the drops its bosses produce, which is exactly the set the pool will be built
    from, so an instance that matched but dropped nothing could not have helped.
    """
    if not encounter_names:
        return None

    by_instance: dict[int, tuple[str, str | None, set[str]]] = {}
    for drops in index.drops.values():
        for drop in drops:
            if drop.kind != "raid" or drop.instance_id is None:
                continue
            entry = by_instance.setdefault(drop.instance_id, (drop.instance, drop.expansion, set()))
            entry[2].add(drop.encounter)

    best: RaidMatch | None = None
    for instance_id, (instance, expansion, encounters) in by_instance.items():
        matched = tuple(
            name for name in encounter_names if any(_names_agree(name, e) for e in encounters)
        )
        missing = tuple(name for name in encounter_names if name not in matched)
        candidate = RaidMatch(
            instance_id=instance_id,
            instance=instance,
            expansion=expansion,
            matched=matched,
            missing=missing,
        )
        if best is None or candidate.share > best.share:
            best = candidate

    if best is None or best.share < MIN_RAID_ENCOUNTER_MATCH:
        return None
    return best


@dataclass(frozen=True)
class PoolCandidate:
    """One item the journal placed and simc can simulate."""

    item_id: int
    name: str
    slug: str
    primary_stat: str | None
    secondary_stat: str | None
    base_ilevel: int
    base_quality: int
    #: "raid" | "mythicplus", from which half of the derivation claimed it.
    source: str
    instance: str
    instance_id: int | None
    encounter: str
    expansion: str | None

    @property
    def evidence(self) -> str:
        return f"{self.encounter}, {self.instance}"

    def to_gear_item(self, carried: GearItem | None = None) -> GearItem:
        """A pool entry, keeping the hand-written simulation fields of its predecessor.

        Gems, enchants and bonus ids are *how the item is worn*, not where it comes
        from, and nobody derives them -- measurement says the enchant on a ring is
        worth twelve times a ten-item-level step, so losing one silently would move
        every number in the comparison. Rebuilding the pool must not throw them away.
        """
        return GearItem(
            item_id=self.item_id,
            name=self.name,
            slug=self.slug,
            primary_stat=self.primary_stat,
            secondary_stat=self.secondary_stat,
            source=self.source,
            base_ilevel=self.base_ilevel,
            base_quality=self.base_quality,
            bonus_ids=carried.bonus_ids if carried else (),
            gem_ids=carried.gem_ids if carried else (),
            enchant_id=carried.enchant_id if carried else None,
            dungeon=self.instance if self.source == "mythicplus" else None,
        )

    def to_json(self, carried: GearItem | None = None) -> dict:
        out = self.to_gear_item(carried).to_json()
        out["baseIlevel"] = self.base_ilevel
        out["baseQuality"] = self.base_quality
        if self.source == "mythicplus":
            out["dungeon"] = self.instance
        out["derivedFrom"] = self.evidence
        if carried:
            if carried.bonus_ids:
                out["bonusIds"] = list(carried.bonus_ids)
            if carried.gem_ids:
                out["gemIds"] = list(carried.gem_ids)
            if carried.enchant_id is not None:
                out["enchantId"] = carried.enchant_id
        return out


@dataclass(frozen=True)
class RejectedItem:
    """An item simc knows about that did not make the pool, and why not."""

    item_id: int
    name: str
    reason: str
    detail: str = ""


@dataclass
class PoolBuild:
    """A derived pool for one slot, plus everything it decided not to include."""

    tier: str
    slot: str
    items: tuple[PoolCandidate, ...] = ()
    raid: RaidMatch | None = None
    rotation: Rotation | None = None
    #: Placed by the journal, but outside this season -- the population the old
    #: heuristic could not tell from the pool. Published because "we dropped these
    #: three and here is the dungeon each one comes from" is a checkable claim
    #: where a bare list of ids is something to take on faith.
    out_of_season: tuple[RejectedItem, ...] = ()
    #: simc has the item; no encounter in the walked scope drops it. Not evidence
    #: of anything on its own -- most trinkets in the table are from old content.
    unplaced: tuple[RejectedItem, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """Is this build safe to write over a curated pool?"""
        return not self.warnings and bool(self.items)

    def by_source(self, source: str) -> tuple[PoolCandidate, ...]:
        return tuple(item for item in self.items if item.source == source)

    def to_json(self) -> dict:
        return {
            "tier": self.tier,
            "slot": self.slot,
            "raid": self.raid.to_json() if self.raid else None,
            "rotation": self.rotation.to_json() if self.rotation else None,
            "counts": {
                "raid": len(self.by_source("raid")),
                "mythicplus": len(self.by_source("mythicplus")),
                "outOfSeason": len(self.out_of_season),
                "unplaced": len(self.unplaced),
            },
            "outOfSeason": [
                {"id": r.item_id, "name": r.name, "reason": r.reason, "detail": r.detail}
                for r in self.out_of_season
            ],
            "warnings": list(self.warnings),
        }


def _pick_drop(drops: Sequence[ItemDrop], instance_ids: frozenset[int]) -> ItemDrop | None:
    """The drop that places an item inside a set of instances, if any.

    An item can drop from several encounters -- a dungeon trinket from a boss and a
    chest, a raid trinket from two bosses. Any one of them places it; the first is
    kept purely so the evidence string is stable between runs.
    """
    for drop in drops:
        if drop.instance_id in instance_ids:
            return drop
    return None


def build_pool(
    tier: str,
    slot: str,
    index: LootIndex,
    rotation: Rotation,
    discovered: Sequence[DiscoveredItem],
    encounter_names: Sequence[str],
) -> PoolBuild:
    """Join simc's item table against the journal to produce one slot's pool.

    Pure: every input is data, so the whole derivation is testable against
    hand-written payloads without credentials, which is the only way it *was*
    tested before the first credentialed run.
    """
    warnings: list[str] = []

    if index.truncated:
        # Refused rather than reported. A partial index cannot distinguish "this
        # item is out of season" from "the walk stopped before reaching its
        # dungeon", and the resulting pool would be wrong in the one direction
        # nobody would notice -- quietly too small, with a plausible shape.
        warnings.append(
            "the journal walk stopped early, so an item missing from it may only be "
            "unread; refusing to build a pool from a partial index"
        )

    raid = identify_tier_raid(index, encounter_names)
    if raid is None:
        warnings.append(
            f"no journal raid accounted for at least {MIN_RAID_ENCOUNTER_MATCH:.0%} of "
            f"the {len(encounter_names)} boss names for {tier}; the raid pool cannot be built"
        )
    elif raid.missing:
        warnings.append(
            f"{raid.instance} matched {len(raid.matched)} of "
            f"{len(raid.matched) + len(raid.missing)} {tier} bosses; unmatched: "
            + ", ".join(raid.missing)
        )

    rotation_ids = rotation.instance_ids
    if not rotation_ids:
        warnings.append(
            "no dungeon rotation could be derived, so the Mythic+ pool would be every "
            "dungeon in the game rather than this season's"
        )

    raid_ids = frozenset({raid.instance_id}) if raid else frozenset()

    items: list[PoolCandidate] = []
    out_of_season: list[RejectedItem] = []
    unplaced: list[RejectedItem] = []

    for item in discovered:
        drops = index.drops.get(item.item_id) or []
        if not drops:
            unplaced.append(
                RejectedItem(item_id=item.item_id, name=item.name, reason="no journal drop")
            )
            continue

        placed = _pick_drop(drops, raid_ids)
        source = "raid"
        if placed is None:
            placed = _pick_drop(drops, rotation_ids)
            source = "mythicplus"

        if placed is None:
            where = drops[0]
            out_of_season.append(
                RejectedItem(
                    item_id=item.item_id,
                    name=item.name,
                    reason="outside this season",
                    detail=where.summary(),
                )
            )
            continue

        items.append(
            PoolCandidate(
                item_id=item.item_id,
                name=item.name,
                slug=item.slug,
                primary_stat=item.primary_stat,
                secondary_stat=item.secondary_stat,
                base_ilevel=item.base_ilevel,
                base_quality=item.base_quality,
                source=source,
                instance=placed.instance,
                instance_id=placed.instance_id,
                encounter=placed.encounter,
                expansion=placed.expansion,
            )
        )

    items.sort(key=lambda c: (c.source != "raid", c.item_id))
    return PoolBuild(
        tier=tier,
        slot=slot,
        items=tuple(items),
        raid=raid,
        rotation=rotation,
        out_of_season=tuple(sorted(out_of_season, key=lambda r: r.item_id)),
        unplaced=tuple(sorted(unplaced, key=lambda r: r.item_id)),
        warnings=tuple(warnings),
    )


def render(build: PoolBuild, previous: Sequence[GearItem] = ()) -> list[str]:
    """What the command prints. The diff against the curated pool is the point."""
    lines: list[str] = []
    raid_items = build.by_source("raid")
    dungeon_items = build.by_source("mythicplus")

    lines.append(f"{build.tier} {build.slot}: derived from Blizzard's journal")
    if build.raid:
        lines.append(
            f"  raid        {build.raid.instance} "
            f"(matched {len(build.raid.matched)} of "
            f"{len(build.raid.matched) + len(build.raid.missing)} bosses)  "
            f"-> {len(raid_items)} item(s)"
        )
    else:
        lines.append("  raid        not identified")
    if build.rotation and build.rotation.dungeons:
        lines.append(
            f"  rotation    {len(build.rotation.dungeons)} dungeon(s): "
            + ", ".join(build.rotation.names)
        )
    lines.append(f"  mythicplus  {len(dungeon_items)} item(s)")

    if build.out_of_season:
        lines.append("")
        lines.append(f"  dropped, outside this season ({len(build.out_of_season)}):")
        for rejected in build.out_of_season:
            lines.append(f"    {rejected.item_id:>7}  {rejected.name}  <- {rejected.detail}")

    known = {item.item_id for item in previous}
    derived_ids = {item.item_id for item in build.items}
    if previous:
        gained = sorted(derived_ids - known)
        lost = sorted(known - derived_ids)
        lines.append("")
        lines.append(
            f"  against the curated pool: {len(gained)} added, {len(lost)} removed, "
            f"{len(derived_ids & known)} unchanged"
        )
        by_id = {item.item_id: item for item in build.items}
        for item_id in gained:
            candidate = by_id[item_id]
            lines.append(f"    +  {item_id:>7}  {candidate.name}  <- {candidate.evidence}")
        was = {item.item_id: item for item in previous}
        for item_id in lost:
            lines.append(f"    -  {item_id:>7}  {was[item_id].name}")

    for warning in build.warnings:
        lines.append(f"  WARNING  {warning}")
    return lines


# --------------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------------


def _rebuild_slot(slot_entry: dict, build: PoolBuild, previous: Sequence[GearItem]) -> dict:
    """The slot's pool file entry, with its item list replaced by the derived one.

    Everything about the slot other than the items is left exactly as written: the
    baseline/candidate sources are a statement about how the comparison is run, not
    about what drops, and rebuilding a pool is not an occasion to reinterpret them.
    """
    carried = {item.item_id: item for item in previous}
    rebuilt = dict(slot_entry)
    rebuilt["items"] = [item.to_json(carried.get(item.item_id)) for item in build.items]
    rebuilt["derivedPool"] = {
        "raid": build.raid.to_json() if build.raid else None,
        "rotation": [
            {"instanceId": d.instance_id, "instance": d.instance or d.name}
            for d in (build.rotation.dungeons if build.rotation else ())
        ],
        "outOfSeason": [
            {"id": r.item_id, "name": r.name, "detail": r.detail} for r in build.out_of_season
        ],
    }
    # The structural inference this replaced described how the pool was *chosen*.
    # Leaving that sentence beside a derived list would be the file asserting
    # something about itself that stopped being true.
    rebuilt.pop("notInRotation", None)
    rebuilt.pop("notInRotationNote", None)
    rebuilt["note"] = (
        "Derived by `wowdps gear-pool` from Blizzard's Encounter Journal joined against "
        "simc's item table: the raid pool is this tier's raid, the Mythic+ pool this "
        "season's dungeon rotation. `derivedFrom` on each item names the encounter and "
        "instance it was read from. Do not hand-edit the membership -- re-run the command."
    )
    return rebuilt


def cmd_gear_pool(args) -> int:  # noqa: ANN001 -- argparse namespace, as every cmd_ takes
    """Rebuild one slot's pool for one tier from the journal.

    Needs the same credentials as ``loot-sources`` and simc's source tree, because
    it is exactly the join of those two things. Prints the diff against the curated
    pool and writes nothing without ``--write``: replacing the membership moves every
    number the gear sweep publishes, so it is a decision somebody makes after reading
    what changed, not a side effect of running a command.
    """
    import argparse  # noqa: F401 -- documents the parameter's type for readers
    import json
    import logging
    from pathlib import Path

    from . import equipment, fightprofile
    from .blizzard import BlizzardClient, BlizzardError, Credentials, RequestBudgetExhausted
    from .lootsources import derive_index, derive_rotation

    log = logging.getLogger(__name__)

    try:
        credentials = Credentials.from_env()
    except BlizzardError as exc:
        log.error("%s", exc)
        return 1

    pool_path = Path(args.pools) if args.pools else equipment.pools_file()
    raw = json.loads(pool_path.read_text(encoding="utf-8"))
    tier_entry = (raw.get("tiers") or {}).get(args.tier)
    if tier_entry is None:
        log.error("no gear pools defined for tier %s in %s", args.tier, pool_path)
        return 1
    # A slot with no entry yet is seeded rather than refused. The whole point of
    # deriving the pool is that nobody has to enumerate one by hand first -- the
    # journal knows what drops in this tier's raid and this season's dungeons, and
    # that is the same answer for a ring as for a trinket. The comparison's shape
    # (baseline from what is farmable, candidates from the raid) is a property of the
    # question, not of the slot, so it carries over.
    slots = tier_entry.setdefault("slots", {})
    if args.slot not in slots:
        log.info("tier %s has no %s pool yet; deriving one", args.tier, args.slot)
        slots[args.slot] = {
            "baselineSource": "mythicplus",
            "candidateSource": "raid",
            "items": [],
        }

    slot = equipment.SLOTS_BY_ID[args.slot]
    discovered = equipment.discover_items(Path(args.simc_source), slot.inventory_type)
    if not discovered:
        log.error("no %s items found in %s", args.slot, args.simc_source)
        return 1

    tier_profiles = fightprofile.load_profiles(args.tier)
    encounter_names = tuple(profile.name for profile in tier_profiles.profiles.values())
    if not encounter_names:
        log.error(
            "tier %s has no fight profiles, so its raid cannot be identified; add its "
            "encounters to fight_profiles.json first",
            args.tier,
        )
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with BlizzardClient(
        credentials,
        region=args.region,
        locale=args.locale,
        cache_dir=Path(args.cache) if args.cache else out_dir / "cache",
        rate_per_second=args.rate,
        max_requests=args.max_requests,
    ) as client:
        try:
            rotation = derive_rotation(client)
        except RequestBudgetExhausted as exc:
            log.error("%s", exc)
            return 2
        except BlizzardError as exc:
            log.error("%s", exc)
            return 1
        index = derive_index(
            client,
            expansions=tuple(args.expansion or ()),
            extra_instances=rotation.instance_ids,
        )
        cost = client.ledger.to_json()

    build = build_pool(args.tier, args.slot, index, rotation, discovered, encounter_names)
    previous = equipment.load_pools(args.tier, pool_path).slots[args.slot].items

    transcript = render(build, previous)
    transcript.append("")
    transcript.append(
        f"  {cost['requests']} requests ({cost['cacheHits']} served from cache), "
        f"{cost['shareOfHourlyLimit']:.1%} of the {cost['limitPerHour']}/hour limit"
    )
    text = "\n".join(transcript)
    print(text)
    (out_dir / f"gear-pool-{args.tier}-{args.slot}.txt").write_text(text + "\n", encoding="utf-8")
    (out_dir / f"gear-pool-{args.tier}-{args.slot}.json").write_text(
        json.dumps({"build": build.to_json(), "cost": cost}, indent=1) + "\n", encoding="utf-8"
    )

    if not build.usable:
        log.error("refusing to write a pool that carries warnings; see the report above")
        return 2
    if not args.write:
        log.info("--write not given: %s unchanged", pool_path)
        return 0

    tier_entry["slots"][args.slot] = _rebuild_slot(tier_entry["slots"][args.slot], build, previous)
    pool_path.write_text(json.dumps(raw, indent=1) + "\n", encoding="utf-8")
    log.info(
        "wrote %s: %d raid, %d mythicplus",
        pool_path,
        len(build.by_source("raid")),
        len(build.by_source("mythicplus")),
    )
    return 0


def add_arguments(parser) -> None:  # noqa: ANN001 -- argparse parser
    parser.add_argument("--tier", default="MID2", help="tier whose pool is rebuilt")
    parser.add_argument("--slot", default="trinket", help="which slot's pool to rebuild")
    parser.add_argument(
        "--simc-source", required=True, help="simc source checkout, for the item table"
    )
    parser.add_argument("--pools", help="alternative gear_pools.json to read and write")
    parser.add_argument("--out", default="loot-sources", help="where the report is written")
    parser.add_argument("--cache", help="response cache directory (default: <out>/cache)")
    parser.add_argument("--region", default="eu")
    parser.add_argument("--locale", default="en_US")
    parser.add_argument("--rate", type=float, default=20.0, help="requests per second")
    parser.add_argument("--max-requests", type=int, default=4000)
    parser.add_argument(
        "--expansion",
        action="append",
        help="restrict the journal walk to these expansions (repeatable). Cheaper and "
        "narrower: an item outside the scope comes back unplaced, never misplaced.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the slot's item list. Without it the diff is printed and nothing "
        "is written -- the membership moves every published number.",
    )
