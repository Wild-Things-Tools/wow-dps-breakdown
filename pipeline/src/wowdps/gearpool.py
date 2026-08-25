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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .equipment import DiscoveredItem, GearItem
from .lootsources import ItemDrop, LootIndex, Rotation

#: A companion instance needs at least this many of the tier's equipped items to
#: count as part of the same raid. One is enough *because* the expansion test below
#: does the real work: a tier's raid content is in one expansion, and a profile that
#: equips a legacy item (MID2's Arcane Mage wears a Legion ring) places it in an old
#: raid in a different expansion, where this cannot reach it.
MIN_COMPANION_HITS = 1


def _normalise(name: str) -> str:
    """Fold a name to something two sources can agree on."""
    return re.sub(r"[^a-z0-9]+", "", name.lower().replace("&", "and"))


#: Equipment lines in a simc profile. Only real gear slots -- a profile also carries
#: `id=` inside other options, and reading those would place items nobody wears.
#:
#: **Every alias simc accepts, because it accepts several per slot and this list had
#: picked the wrong one for two of them.** Measured on 2026-08-23 against simc's
#: midnight branch: `player.cpp` registers `shoulders` *and* `shoulder`, `wrists`
#: *and* `wrist`, `hands`/`hand`, `legs`/`leg`, `feet`/`foot`, `finger1`/`ring1` --
#: and every shipped MID2 profile writes the plural (`shoulders=ornaments_of_the_
#: eternal_coil,...`, `wrists=martyrs_bindings,...`, checked in five class profiles).
#: So the singular-only alternation here matched neither spelling of the shoulders
#: line nor of the wrists line, and `equipped_item_ids` -- the authority for which
#: raid a tier belongs to -- has been silently dropping two of every profile's
#: sixteen slots.
#:
#: A reader accepts every spelling; an emitter writes the one simc ships. The
#: opposite confusion (a slot table carrying only the plural, or only the singular)
#: is easy to reintroduce, so it is named in CLAUDE.md.
_EQUIP_LINE = re.compile(
    r"^(?:head|neck|shoulders|shoulder|back|chest|wrists|wrist|hands|hand|waist"
    r"|legs|leg|feet|foot|finger[12]|ring[12]|trinket[12]|main_hand|off_hand"
    r"|tabard|shirt)\s*=.*?\bid=(\d+)",
    re.MULTILINE,
)


def equipped_item_ids(simc_dir: Path, tier: str) -> frozenset[int]:
    """Every item id the tier's own simc profiles wear.

    This is the authority for *which raid belongs to this tier*, and getting that
    wrong is what the first version did. It keyed on the tier's boss list from
    ``fight_profiles.json``, which sounds like the same question and is not: that list
    is what Warcraft Logs currently has kills for, so during the week before a season
    turns it describes the raid that is **ending**. MID2's profiles gear for The
    Venomous Abyss and The Tidebound Grotto while the logs are still full of The
    Voidspire, and matching bosses duly named the wrong raid and called the correct
    pool wrong.

    What a tier *is*, for this project, is the set of profiles simc ships under that
    name. So the raid is the one that drops what those profiles wear -- derived from
    simc, like every other number here, and immune to the season boundary.
    """
    found: set[int] = set()
    directory = simc_dir / "profiles" / tier
    if not directory.is_dir():
        return frozenset()
    for path in sorted(directory.glob("*.simc")):
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(int(match) for match in _EQUIP_LINE.findall(text))
    return frozenset(found)


@dataclass(frozen=True)
class InstanceHit:
    """One journal instance, and how much of the tier's gear it accounts for."""

    instance_id: int
    instance: str
    expansion: str | None
    hits: int
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class TierRaid:
    """The raid instances this tier's profiles are geared from.

    Plural on purpose. MID2's raid is *two* journal instances -- The Venomous Abyss
    and The Tidebound Grotto -- and a single-instance answer silently drops whatever
    the second one contributes.
    """

    instances: tuple[InstanceHit, ...] = ()
    #: Instances that dropped some of the tier's gear but were excluded for being in
    #: another expansion -- almost always a legacy item a profile still wears.
    legacy: tuple[InstanceHit, ...] = ()

    @property
    def instance_ids(self) -> frozenset[int]:
        return frozenset(hit.instance_id for hit in self.instances)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(hit.instance for hit in self.instances)

    def to_json(self) -> dict:
        return {
            "instances": [
                {
                    "instanceId": hit.instance_id,
                    "instance": hit.instance,
                    "expansion": hit.expansion,
                    "equippedItems": hit.hits,
                }
                for hit in self.instances
            ],
            "legacy": [
                {"instance": hit.instance, "expansion": hit.expansion, "equippedItems": hit.hits}
                for hit in self.legacy
            ],
        }


def identify_tier_raid(index: LootIndex, equipped: Iterable[int]) -> TierRaid | None:
    """Which raid instances is this tier geared from?

    Counts, per raid-kind instance, how many of the tier's equipped items it drops.
    The instance with the most is the raid; any other instance **in the same
    expansion** that also dropped some of the tier's gear joins it, which is what
    keeps a two-instance raid whole. An instance in a different expansion is reported
    as legacy rather than joined -- that is a profile still wearing an old item, not a
    second half of this tier's raid.
    """
    wanted = set(equipped)
    if not wanted:
        return None

    tally: dict[int, tuple[str, str | None, set[int]]] = {}
    for item_id in wanted:
        for drop in index.drops.get(item_id) or []:
            if drop.kind != "raid" or drop.instance_id is None:
                continue
            entry = tally.setdefault(drop.instance_id, (drop.instance, drop.expansion, set()))
            entry[2].add(item_id)

    if not tally:
        return None

    hits = [
        InstanceHit(instance_id=iid, instance=name, expansion=expansion, hits=len(ids))
        for iid, (name, expansion, ids) in tally.items()
    ]
    hits.sort(key=lambda hit: (-hit.hits, hit.instance))
    best = hits[0]

    kept = [best]
    legacy = []
    for hit in hits[1:]:
        same_expansion = _normalise(hit.expansion or "") == _normalise(best.expansion or "")
        if same_expansion and hit.hits >= MIN_COMPANION_HITS:
            kept.append(hit)
        else:
            legacy.append(hit)
    return TierRaid(instances=tuple(kept), legacy=tuple(legacy))


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
    raid: TierRaid | None = None
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
    equipped: Iterable[int],
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

    raid = identify_tier_raid(index, equipped)
    if raid is None:
        warnings.append(
            f"no raid in the journal drops anything {tier}'s profiles wear, so the raid "
            "pool cannot be built"
        )
    elif raid.legacy:
        # Not a warning: a profile wearing an old item is ordinary, and the instance
        # that drops it is reported so nobody has to wonder why it is not in the pool.
        pass

    rotation_ids = rotation.instance_ids
    if not rotation_ids:
        warnings.append(
            "no dungeon rotation could be derived, so the Mythic+ pool would be every "
            "dungeon in the game rather than this season's"
        )

    raid_ids = raid.instance_ids if raid else frozenset()

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
        where = ", ".join(f"{hit.instance} ({hit.hits} equipped)" for hit in build.raid.instances)
        lines.append(f"  raid        {where}  -> {len(raid_items)} item(s)")
        for hit in build.raid.legacy:
            lines.append(
                f"              legacy, not joined: {hit.instance} "
                f"({hit.expansion}, {hit.hits} equipped)"
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

    from . import equipment
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

    equipped = equipped_item_ids(Path(args.simc_source), args.tier)
    if not equipped:
        log.error(
            "no profiles found under %s/profiles/%s, so this tier's raid cannot be "
            "identified -- it is the raid that drops what these profiles wear",
            args.simc_source,
            args.tier,
        )
        return 1
    log.info("%s's profiles wear %d distinct items", args.tier, len(equipped))

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

    build = build_pool(args.tier, args.slot, index, rotation, discovered, equipped)
    # Read back through load_pools so the comparison sees exactly what the sweep
    # sees -- but a slot seeded a moment ago is not on disk yet, and asking for it
    # would crash where the honest answer is "there was nothing here before".
    loaded = equipment.load_pools(args.tier, pool_path).slots
    previous = loaded[args.slot].items if args.slot in loaded else ()

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
