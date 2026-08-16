"""Equipment slots, item pools and item levels for the gear-comparison sweep.

The question this answers is a loot-council question, not a theorycrafting one:
*given the two Mythic+ trinkets a character already has, is this raid drop an
upgrade, and by how much?* Answering it needs three things simc's data supplies
directly -- the items, their stats and their effects -- and one it does not: which
instance an item drops from.

Nothing here is trinket-specific by design. A slot is a label plus the simc option
names of its sockets, so necks, rings and weapons are new ``EquipmentSlot`` entries
and new pool rows, not a new module.

What simc's offline data can and cannot tell us
-----------------------------------------------
**Enumerating items works.** ``engine/dbc/generated/item_data.inc`` carries every
item with its inventory type (12 = trinket), base item level, base quality and stat
allocation, and ``item_effect.inc`` says which items have an on-use or equip effect
at all. ``discover_items`` reads both.

**Naming the source does not.** simc's *extraction* toolchain reads
``JournalEncounterItem`` (dbc_extract3), but the only thing that survives into the
shipped ``.inc`` files is a three-bit ``type_flags`` for Raid Finder / Heroic, which
is 0 for every Midnight trinket. There is no instance, no journal, no encounter. So
"this trinket drops in the raid" cannot be *derived*; it has to be asserted.

It is asserted here, in ``data/gear_pools.json``, from a structural reading of the
item table that is at least reproducible:

* Trinkets at **base item level 219, quality 4 (epic), ids 270160-270175** -- one
  contiguous block of fifteen epics covering every primary/secondary archetype
  exactly once or twice. That shape is a raid loot table.
* Trinkets at **base item level 108, quality 3 (rare)** -- twenty-five items. Rare
  base quality upgraded to epic by bonus id is the dungeon pattern, and twenty-five
  is roughly three per dungeon.

Both readings are inference. They are recorded as data with a ``source`` field so a
human -- or a future import from an item database that does carry drop sources --
can correct one row without touching any code. ``wowdps gear-candidates`` prints the
enumeration with everything except ``source`` filled in, which is the shape of that
correction.

Item levels
-----------
Measured, not remembered. Equipping one trinket under each bonus id of the Midnight
upgrade family (item_bonus type 34, values 618/978) and reading the item level back
out of simc's own report gives the ladder::

    12849 12850 12851 12852 12853 12854 12855 12856 13848
      318   321   324   328   331   334   337   340   344

The MID2 profiles equip exactly two of these for epics: 334 and 344 (331 is the
crafted cap). 344 is the top of the ladder. simc's data does not name upgrade tracks
-- "Hero", "Myth" are Blizzard's words and appear nowhere in it -- so the two levels
are carried here as data with the evidence attached rather than as a hard-coded
belief about which track is which.

``ilevel=`` is sufficient on its own: a profileset that sets
``trinket1=,id=250215,ilevel=334`` returned DPS identical to the last digit to one
that also passed the profile's full ``bonus_id=12854/13440``. That holds for
trinkets because their bonus ids only add quality, sockets and flavour text. It will
*not* hold for rings and necks, where bonus ids add sockets that carry real stats --
so the pool format keeps a per-item ``bonusIds`` field for the day that matters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

#: simc's inventory_type for a trinket. Others are listed for the slots that come next.
INVENTORY_TYPES: dict[str, int] = {
    "trinket": 12,
    "neck": 2,
    "finger": 11,
}

# item_data.inc stat rows are (item_mod_type, allocation, socket_multiplier). Only the
# type matters here: it says which primary stat the item carries, and therefore which
# specs can wear it without wasting its main stat budget.
_PRIMARY_STATS: dict[int, str] = {
    3: "agility",
    4: "strength",
    5: "intellect",
    71: "str_agi_int",
    72: "str_agi",
    73: "agi_int",
    74: "str_int",
}

_SECONDARY_STATS: dict[int, str] = {
    32: "crit",
    36: "haste",
    40: "versatility",
    49: "mastery",
    61: "speed",
    62: "leech",
    63: "avoidance",
}

#: Which primary stats a spec of each primary can actually use. A combined stat
#: ("str_agi_int") resolves to whatever the wearer uses, so it is eligible for all;
#: a pure Strength trinket on a Mage is a wasted item budget, not a choice.
_ELIGIBLE: dict[str, frozenset[str]] = {
    "intellect": frozenset({"intellect", "str_agi_int", "agi_int", "str_int"}),
    "agility": frozenset({"agility", "str_agi_int", "agi_int", "str_agi"}),
    "strength": frozenset({"strength", "str_agi_int", "str_agi", "str_int"}),
}


@dataclass(frozen=True)
class EquipmentSlot:
    """One wearable slot and the simc options that fill it.

    ``sockets`` is ordered: the sweep treats the first as the one it keeps and the
    second as the one a candidate replaces, which is what makes "does this drop go
    in over my worse trinket" the question being answered.
    """

    id: str
    label: str
    sockets: tuple[str, ...]
    inventory_type: int

    @property
    def is_paired(self) -> bool:
        return len(self.sockets) > 1


TRINKET = EquipmentSlot(
    id="trinket",
    label="Trinket",
    sockets=("trinket1", "trinket2"),
    inventory_type=INVENTORY_TYPES["trinket"],
)

# Not swept yet, but the format and the sweep are already slot-generic: adding a
# pool for one of these to data/gear_pools.json is the whole of the work.
NECK = EquipmentSlot(id="neck", label="Neck", sockets=("neck",), inventory_type=2)
FINGER = EquipmentSlot(id="finger", label="Ring", sockets=("finger1", "finger2"), inventory_type=11)

ALL_SLOTS: tuple[EquipmentSlot, ...] = (TRINKET, NECK, FINGER)
SLOTS_BY_ID: dict[str, EquipmentSlot] = {s.id: s for s in ALL_SLOTS}


@dataclass(frozen=True)
class ItemLevel:
    """One item level the sweep runs candidates at."""

    id: str
    label: str
    ilevel: int
    #: Where the number comes from, carried into the dataset so the site can say so.
    evidence: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "ilevel": self.ilevel,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DerivedSource:
    """Where one item drops, according to Blizzard's Game Data API.

    Written by ``wowdps loot-sources`` and never by hand. It sits *beside* the
    asserted ``source`` rather than replacing it, because the two disagreeing is a
    finding: the structural inference that produced ``source`` is wrong in both
    directions, and which rows it got wrong is exactly what nobody could check
    before. ``lootsources.py`` prints the disagreements; a human resolves them.
    """

    #: "raid" | "mythicplus", from the journal's own raid/dungeon split. ``None``
    #: when the item drops in both, which is a fact rather than a failure.
    source: str | None
    encounter: str
    encounter_id: int | None
    instance: str
    instance_id: int | None
    expansion: str | None
    #: Whether the instance is one this Mythic+ season runs. ``None`` means no
    #: rotation was derived, which is not the same as "no".
    in_rotation: bool | None

    @classmethod
    def from_json(cls, raw: dict) -> DerivedSource:
        return cls(
            source=raw.get("source"),
            encounter=raw.get("encounter", ""),
            encounter_id=raw.get("encounterId"),
            instance=raw.get("instance", ""),
            instance_id=raw.get("instanceId"),
            expansion=raw.get("expansion"),
            in_rotation=raw.get("inRotation"),
        )


@dataclass(frozen=True)
class GearItem:
    """One candidate item."""

    item_id: int
    name: str
    slug: str
    #: "intellect" | "agility" | "strength" | a combined form | None for
    #: secondary-only trinkets, which every spec can use.
    primary_stat: str | None
    #: The secondary the item allocates, if any. Display only.
    secondary_stat: str | None
    #: "raid" | "mythicplus". Asserted by hand -- see the module docstring. Compare
    #: against ``derived`` rather than replacing one with the other.
    source: str
    base_ilevel: int
    base_quality: int
    #: Extra bonus ids to pass alongside ``ilevel``.
    #:
    #: Empty everywhere, and measurement says it can stay that way. It was kept for
    #: the day rings and necks joined the sweep, on the assumption that their socket
    #: bonuses live here -- that assumption is wrong. Arcane Mage, MID2, one target,
    #: 1000 deterministic iterations, one ring: passing the profile's own
    #: ``bonus_id=12854/13440/13668`` alongside an explicit ``ilevel`` returned DPS
    #: **identical to the last digit** to passing no bonus ids at all, both with a
    #: gem and without one. What a socket is worth arrives through ``gem_id``.
    bonus_ids: tuple[int, ...] = ()
    #: The gem in the item's socket, and the enchant on it. Not decoration: on the
    #: same run the enchant was worth **+1.09%** and the gem **+0.44%** (together
    #: +1.55%, so they add), against **+0.09%** for the whole ten-item-level step
    #: from 334 to 344. A ring comparison that carries item level and drops these is
    #: measuring the wrong thing by an order of magnitude.
    gem_ids: tuple[int, ...] = ()
    enchant_id: int | None = None
    #: Which dungeon drops it, when somebody has said so by hand. ``None`` means
    #: unknown. Superseded in practice by ``derived.instance`` once a
    #: ``loot-sources`` pass has run, which is the point -- this field is the
    #: fallback for a fact nobody can derive, not the intended source.
    dungeon: str | None = None
    #: What the Game Data API says, when a ``loot-sources`` pass has run.
    derived: DerivedSource | None = None

    @property
    def source_disagrees(self) -> bool:
        """Does the API contradict the hand-written source?

        Only ever *reported*. The sweep keeps using ``source``, because a pool that
        silently changed shape between two runs would move every number in the
        published comparison with no note saying why.
        """
        return bool(self.derived and self.derived.source and self.derived.source != self.source)

    def usable_by(self, primary: str) -> bool:
        if self.primary_stat is None:
            return True  # secondary-only: no primary budget to waste
        return self.primary_stat in _ELIGIBLE.get(primary, frozenset())

    def simc_item(self, socket: str, ilevel: int) -> str:
        """``trinket1=,id=270164,ilevel=344`` -- the form simc's own test profiles use.

        Gems and the enchant come after the item level, in simc's own profile order.
        A slot that carries them must carry them on *both* sides of a comparison:
        against an unenchanted baseline every candidate wins by the enchant.
        """
        parts = [f"{socket}=,id={self.item_id}"]
        if self.bonus_ids:
            parts.append("bonus_id=" + "/".join(str(b) for b in self.bonus_ids))
        parts.append(f"ilevel={ilevel}")
        if self.gem_ids:
            parts.append("gem_id=" + "/".join(str(g) for g in self.gem_ids))
        if self.enchant_id is not None:
            parts.append(f"enchant_id={self.enchant_id}")
        return ",".join(parts)

    def to_json(self) -> dict:
        out: dict = {
            "id": self.item_id,
            "name": self.name,
            "slug": self.slug,
            "source": self.source,
            "primaryStat": self.primary_stat,
        }
        if self.secondary_stat:
            out["secondaryStat"] = self.secondary_stat
        return out


@dataclass(frozen=True)
class SlotPool:
    """Every candidate for one slot in one tier, plus the item levels to run them at."""

    tier: str
    slot: EquipmentSlot
    items: tuple[GearItem, ...]
    item_levels: tuple[ItemLevel, ...]
    #: Which source the baseline ("what the character already wears") is drawn from.
    baseline_source: str
    #: Which source the candidates being judged are drawn from.
    candidate_source: str
    #: The dungeons this season actually runs, when declared. Empty means "not
    #: stated", and then nothing is filtered -- see ``in_rotation``.
    rotation: tuple[str, ...] = ()
    note: str = ""

    def in_rotation(self, item: GearItem) -> bool:
        """Is this item obtainable this season?

        Two mechanisms, one of which is meant to retire the other. Once
        ``wowdps loot-sources`` has run, each item carries ``derived.inRotation``
        from Blizzard's own leaderboards and that settles it -- no rotation list,
        no per-item dungeon assignment, nothing typed in. The hand-written
        ``rotation`` below is the fallback for a pool nobody has derived yet.

        A Mythic+ season runs a fixed set of dungeons, so a trinket from a dungeon
        outside it cannot be farmed and has no business anchoring a baseline the
        loot council reasons from. The pool is selected structurally (rare-base
        trinkets at the expansion's dungeon item level), which captures *every*
        dungeon of the expansion rather than this season's rotation -- and, because
        modern rotations mix in older dungeons, may also miss obtainable trinkets
        whose ids and base item levels belong to a previous expansion entirely. The
        rule is a proxy for the wrong thing in both directions.

        With no rotation declared nothing is filtered, because dropping every item
        whose dungeon is merely unrecorded would silently empty the pool. That is
        the state to fix by naming the dungeons, not by loosening this.
        """
        # A derived answer wins outright: it comes from the journal's own loot
        # tables rather than from a name somebody typed, and it is per item, so it
        # needs neither the rotation list nor a dungeon assignment to be useful.
        #
        # Only for Mythic+ items, though. A season's rotation is a list of dungeons
        # and says nothing whatever about a raid drop, so consulting it for one can
        # only ever subtract. The derivation now leaves `in_rotation` as None for
        # anything that is not a dungeon drop; this guard is the second lock, because
        # a payload written before that fix carries False on every raid item and
        # would empty the candidate pool without changing a single visible number
        # other than all of them.
        if (
            item.source == "mythicplus"
            and item.derived is not None
            and item.derived.in_rotation is not None
        ):
            return item.derived.in_rotation

        if not self.rotation or item.source != "mythicplus":
            return True
        if not self._any_placed():
            # The rotation is named but no item has been assigned a dungeon yet.
            # Filtering here would exclude every candidate and empty the pool --
            # a worse failure than the one the rotation exists to fix.
            return True
        return item.dungeon in self.rotation

    def _any_placed(self) -> bool:
        return any(i.source == "mythicplus" and i.dungeon is not None for i in self.items)

    def rotation_is_stated(self) -> bool:
        return bool(self.rotation)

    def unplaced(self) -> list[GearItem]:
        """Rotation-relevant items nobody has assigned a dungeon to yet."""
        if not self.rotation:
            return []
        return [i for i in self.items if i.source == "mythicplus" and i.dungeon is None]

    def baseline_candidates(self, primary: str) -> list[GearItem]:
        return [
            i
            for i in self.items
            if i.source == self.baseline_source and i.usable_by(primary) and self.in_rotation(i)
        ]

    def candidates(self, primary: str) -> list[GearItem]:
        return [
            i
            for i in self.items
            if i.source == self.candidate_source and i.usable_by(primary) and self.in_rotation(i)
        ]

    def baseline_ilevel(self) -> ItemLevel:
        """The baseline is worn at the lower of the two levels.

        A character farming Mythic+ tops its dungeon trinkets out on the lower track;
        pricing the baseline at the raid's top level would flatter every raid drop.
        """
        return min(self.item_levels, key=lambda level: level.ilevel)


@dataclass
class GearPools:
    """Everything ``data/gear_pools.json`` holds, for one tier."""

    tier: str
    slots: dict[str, SlotPool] = field(default_factory=dict)
    #: The dungeons this Mythic+ season runs, as a human typed them.
    dungeon_rotation: tuple[str, ...] = ()
    #: The same list as the Game Data API reports it. Kept apart from the asserted
    #: one on purpose: the point of deriving it was to be able to check the typing,
    #: which needs both to survive.
    derived_rotation: tuple[str, ...] = ()

    def rotation_disagreement(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(asserted only, derived only)``, empty when they agree or one is absent."""
        if not self.dungeon_rotation or not self.derived_rotation:
            return (), ()
        return (
            tuple(n for n in self.dungeon_rotation if n not in self.derived_rotation),
            tuple(n for n in self.derived_rotation if n not in self.dungeon_rotation),
        )

    def source_disagreements(self) -> list[GearItem]:
        """Every item whose asserted pool the API contradicts."""
        return [
            item for pool in self.slots.values() for item in pool.items if item.source_disagrees
        ]


def pools_file() -> Path:
    """The shipped pool file. ``loot-sources`` writes it; everything else reads it."""
    return Path(str(resources.files("wowdps") / "data" / "gear_pools.json"))


def load_pools(tier: str, path: Path | None = None) -> GearPools:
    """Read the curated pool file for one tier."""
    source = path or pools_file()
    raw = json.loads(source.read_text(encoding="utf-8"))
    tiers = raw.get("tiers") or {}
    if tier not in tiers:
        known = ", ".join(sorted(tiers)) or "none"
        raise KeyError(
            f"no gear pools defined for tier {tier!r} in {source} (defined: {known}). "
            f"Run `wowdps gear-candidates --tier {tier}` to see what simc offers, then "
            f"add the pool with a source for each item."
        )

    entry = tiers[tier]
    levels = tuple(
        ItemLevel(
            id=level["id"],
            label=level["label"],
            ilevel=int(level["ilevel"]),
            evidence=level.get("evidence", ""),
        )
        for level in entry["itemLevels"]
    )

    pools: dict[str, SlotPool] = {}
    for slot_id, slot_entry in entry["slots"].items():
        slot = SLOTS_BY_ID[slot_id]
        items = tuple(
            GearItem(
                item_id=int(item["id"]),
                name=item["name"],
                slug=item["slug"],
                primary_stat=item.get("primaryStat"),
                secondary_stat=item.get("secondaryStat"),
                source=item["source"],
                base_ilevel=int(item.get("baseIlevel", 0)),
                base_quality=int(item.get("baseQuality", 0)),
                bonus_ids=tuple(item.get("bonusIds") or ()),
                gem_ids=tuple(item.get("gemIds") or ()),
                enchant_id=item.get("enchantId"),
                dungeon=item.get("dungeon"),
                derived=(DerivedSource.from_json(item["derived"]) if item.get("derived") else None),
            )
            for item in slot_entry["items"]
        )
        pools[slot_id] = SlotPool(
            tier=tier,
            slot=slot,
            items=items,
            item_levels=levels,
            baseline_source=slot_entry["baselineSource"],
            candidate_source=slot_entry["candidateSource"],
            rotation=tuple(entry.get("dungeonRotation") or ()),
            note=slot_entry.get("note", ""),
        )

    return GearPools(
        tier=tier,
        slots=pools,
        dungeon_rotation=tuple(entry.get("dungeonRotation") or ()),
        derived_rotation=tuple(entry.get("dungeonRotationDerived") or ()),
    )


# --------------------------------------------------------------------------------
# Reading a spec's primary stat off its profile
# --------------------------------------------------------------------------------

_GEAR_STAT = re.compile(r"^#\s*gear_(intellect|agility|strength)\s*=\s*([0-9.]+)", re.MULTILINE)


def primary_stat(profile_path: Path) -> str:
    """Which primary stat a profile's gear is built around.

    Read from the ``# Gear Summary`` block simc's profile generator writes, not from
    a class/spec table kept here: the profile is the authority on what it wears, and
    a table would need editing every time a spec changes hands. Specs do carry a
    little of another primary (Elemental Shaman has 94 strength from an off-hand,
    Vengeance 442 intellect), so the largest allocation wins rather than the only one.
    """
    text = profile_path.read_text(encoding="utf-8", errors="replace")
    found = [(float(value), stat) for stat, value in _GEAR_STAT.findall(text)]
    if not found:
        raise ValueError(
            f"{profile_path.name} has no '# gear_<stat>=' summary line, so its primary "
            f"stat cannot be read from the profile"
        )
    return max(found)[1]


@dataclass(frozen=True)
class SlotAdornment:
    """The gems and enchant a profile puts on one socket.

    These are properties of *how the slot is worn*, not of the item in it, which is
    why they live here rather than on ``GearItem``: a candidate nobody wears yet has
    no gem of its own, and putting it in unadorned against an adorned baseline is not
    a comparison. Measured on Arcane Mage, MID2, one target, 1000 deterministic
    iterations, one ring: the enchant is worth **+1.09%** and the gem **+0.44%**
    (together +1.55%, they add) against **+0.09%** for the whole ten-item-level step
    from 334 to 344. A ring sweep that carries item level and drops these measures the
    wrong thing by an order of magnitude, and every candidate "wins" by the enchant.

    Trinkets have neither and correctly come back empty -- that is measured too:
    passing a trinket's own bonus ids alongside an explicit item level returned DPS
    identical to the last digit.
    """

    gem_ids: tuple[int, ...] = ()
    enchant_id: int | None = None

    @property
    def is_bare(self) -> bool:
        return not self.gem_ids and self.enchant_id is None


def _gear_line(socket: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(socket)}\s*=(?P<rest>.*)$", re.MULTILINE)


def read_adornments(profile_path: Path, slot: EquipmentSlot) -> dict[str, SlotAdornment]:
    """What this profile gems and enchants each of a slot's sockets with.

    Per socket rather than per slot, because a profile may gem one ring and not the
    other, and the comparison only has to be internally consistent: whatever the
    baseline wears in a socket, the candidate replacing it wears too.
    """
    text = profile_path.read_text(encoding="utf-8", errors="replace")
    found: dict[str, SlotAdornment] = {}
    for socket in slot.sockets:
        match = _gear_line(socket).search(text)
        if not match:
            continue
        rest = match.group("rest")
        gems = re.search(r"\bgem_id=([\d/]+)", rest)
        enchant = re.search(r"\benchant_id=(\d+)", rest)
        found[socket] = SlotAdornment(
            gem_ids=tuple(int(g) for g in gems.group(1).split("/") if g) if gems else (),
            enchant_id=int(enchant.group(1)) if enchant else None,
        )
    return found


def adorn(item: GearItem, adornment: SlotAdornment | None) -> GearItem:
    """The item as this profile would wear it in that socket."""
    if adornment is None or adornment.is_bare:
        return item
    return replace(item, gem_ids=adornment.gem_ids, enchant_id=adornment.enchant_id)


# --------------------------------------------------------------------------------
# Enumerating what simc has, for regenerating the pool file
# --------------------------------------------------------------------------------

_ITEM_ROW = re.compile(r'^\s*\{ "((?:[^"\\]|\\.)*)", (.*) \},\s*$')
_EFFECT_ROW = re.compile(r"^\s*\{\s*\d+,\s*\d+,\s*(\d+),")
_STAT_ROW = re.compile(r"^\s*\{\s*(-?\d+),\s*(-?\d+),\s*[-0-9.]+f\s*\},\s*$")


@dataclass(frozen=True)
class DiscoveredItem:
    """One row of the enumeration ``wowdps gear-candidates`` prints."""

    item_id: int
    name: str
    slug: str
    base_ilevel: int
    base_quality: int
    primary_stat: str | None
    secondary_stat: str | None
    has_effect: bool


def slugify_item(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def discover_items(simc_dir: Path, inventory_type: int) -> list[DiscoveredItem]:
    """Every item of one inventory type in simc's generated tables.

    Parsing the ``.inc`` files rather than shelling out to simc because there is no
    simc command that lists items -- ``spell_query`` covers spells and effects only.
    The layout is a plain C array of ``dbc_item_data_t``, so the fields are read
    positionally against ``engine/dbc/item_data.hpp``.
    """
    generated = simc_dir / "engine" / "dbc" / "generated"
    item_text = (generated / "item_data.inc").read_text(encoding="utf-8", errors="replace")

    # Stat allocations live in one flat array the item rows index into.
    stats: list[int] = []
    for line in item_text.splitlines():
        row = _STAT_ROW.match(line)
        if row:
            stats.append(int(row.group(1)))

    with_effects: set[int] = set()
    for line in (
        (generated / "item_effect.inc").read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        row = _EFFECT_ROW.match(line)
        if row and int(row.group(1)):
            with_effects.add(int(row.group(1)))

    found: list[DiscoveredItem] = []
    for line in item_text.splitlines():
        match = _ITEM_ROW.match(line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        rest = re.sub(r"\{[^}]*\}", "SOCKETS", rest)
        rest = re.sub(r"&__item_stats_data\[(\d+)\]", r"STATS\1", rest)
        fields = [part.strip() for part in rest.split(",")]
        if len(fields) < 27:
            continue
        try:
            item_id = int(fields[0])
            ilevel = int(fields[4])
            quality = int(fields[8])
            inv_type = int(fields[9])
            stats_count = int(fields[17])
        except ValueError:
            continue
        if inv_type != inventory_type:
            continue

        offset = int(fields[16][5:]) if fields[16].startswith("STATS") else None
        primary: str | None = None
        secondary: str | None = None
        if offset is not None:
            for index in range(stats_count):
                stat_type = stats[offset + index]
                if primary is None and stat_type in _PRIMARY_STATS:
                    primary = _PRIMARY_STATS[stat_type]
                if secondary is None and stat_type in _SECONDARY_STATS:
                    secondary = _SECONDARY_STATS[stat_type]

        found.append(
            DiscoveredItem(
                item_id=item_id,
                name=name,
                slug=slugify_item(name),
                base_ilevel=ilevel,
                base_quality=quality,
                primary_stat=primary,
                secondary_stat=secondary,
                has_effect=item_id in with_effects,
            )
        )

    found.sort(key=lambda item: (-item.base_ilevel, item.base_quality, item.name))
    return found
