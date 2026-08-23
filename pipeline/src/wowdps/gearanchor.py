"""Normalized, comparable equipment for computed builds.

A computed build -- one this project constructs rather than one simc ships -- has
no gear of its own. Whatever kit it is handed (a harvested player's, or a shipped
profile's) is a second variable beside the talents, and a second variable is the
thing this project has already been burned by once.

**The failure is measured, not hypothetical.** The first run that simulated simc's
*disabled* profiles put all eight of their builds below all twenty-eight shipped
ones with no overlap at all -- 112k-134k against 177k-268k. A separation that clean
is never a balance finding; it is the signature of a systematic difference, and this
one is two differences: those profiles wear roughly a whole tier less gear (289
against the 334-344 the shipped ones state), and none of this season's tier set.
``dataset.gear_caveat`` exists to say so on the site. This module exists so a build
this project computes never has to carry that caveat in the first place.

There are two standards of "equal gear" here and they are not the same standard.

**Within one spec's search, gear must be byte-identical.** Not "roughly equal":
measured on Arcane Mage, MID2, one target, 1000 deterministic iterations, one ring,
the enchant alone is worth **+1.09%** and the gem **+0.44%** against **+0.09%** for
the whole ten-item-level step from 334 to 344. A talent difference worth 2-3% would
sit underneath a kit difference of that size. So candidates within a search differ in
``talents=`` and in nothing else, which is what an anchor makes possible: one kit,
emitted once, worn by every variant.

**Across specs, for ranking, the target is a band.** The tier's own shipped profiles
span one -- MID2's state 334 and 344 -- which is exactly why
``dataset.shipped_item_levels`` derives a band from them rather than fixing a value.
An anchor has to land inside that band, and it has to be able to say where inside it
landed and why.

What is normalized, and what is deliberately not
-----------------------------------------------
**Item level, by writing ``ilevel=`` onto every gear line.** That is measured-sound
and it is measured twice. A profileset using ``trinket1=,id=250215,ilevel=334``
returned DPS identical to the last digit to one that also passed the profile's own
``bonus_id=12854/13440``, so an explicit item level overrides the scaling those bonus
ids would otherwise set; and the same held for a ring, with a gem and without one. So
the item level can be moved without touching anything else on the line.

**Gems, enchants, crafted stats and bonus ids are preserved verbatim.** The first
three because they carry real stats -- the enchant is worth twelve times the whole
ten-item-level step -- and the fourth because it costs nothing to keep and the
measurement above says keeping it changes no number. A normalization that dropped
bonus ids would be making a claim about every slot from a measurement taken on two.

**The tier set is overridden explicitly, in both directions.** Same mechanism and
same reason as ``buffsweep.set_variants``: a kit that already wears the set would
otherwise carry it into the anchor silently, and a kit that wears last season's would
carry *that*. Neither is a property of the build being computed. The current tier's
token is written at the state derived below -- typically 1/1 -- and every other
numbered tier of the same expansion is written to 0/0, so the anchor *states* the
set state rather than inheriting it.

**Nothing else is touched.** Race, consumables, level and the action list stay as the
source profile has them. Those are not gear, and a module called "gear anchor" that
quietly reset a race would be the worst kind of surprise.

Where the target comes from
---------------------------
Derived from the tier's own shipped profiles, never written down here, so a new tier
needs no edit. Two derivations, one for each half:

* **Item level: the floor of the band** ``dataset.shipped_item_levels`` reports.
  Two candidates were rejected and the reasons are measurements. The *mode* of the
  item levels the tier's shipped gear lines state is a coin flip -- counted on MID2
  at simc 69a46e1, the 301 gear lines that state one split **133 at 344, 131 at 334**
  and 37 at 331, a two-line margin that would flip on one profile being retuned. And
  the *ceiling* flatters: every computed build would then wear the top of the ladder
  while the shipped builds it is ranked against wear a mixture averaging 336.75-339.25
  (simc's own ``# gear_ilvl`` lines, measured the same day). The floor is the
  direction that cannot manufacture a win -- a computed build that beats a shipped one
  from the bottom of the band is winning from behind -- and it is the same choice
  ``equipment.SlotPool.baseline_ilevel`` already makes, for the same reason.
* **Tier set: the state most of the tier's shipped profiles are actually in.** The
  option token comes from simc's set table -- MID2's thirteen sets, one per class,
  all share ``midnight_season_2`` -- but *which* state to enforce is a separate
  question and the answer is not "whatever the kit had".

  **Measured on MID2 at simc 69a46e1: the tier's shipped profiles disagree with each
  other.** Counting set pieces by ``dbc_item_data_t::id_set`` against the set table's
  ``set_id`` -- which is exactly what ``set_bonus_t::initialize`` does -- 24 of the 28
  shipped damage profiles wear four or five pieces and **four wear none**: both Arcane
  Mage builds and both Frost Mage builds. That gap is already in the published
  ranking and nothing flags it. Its size, measured profileset against profileset at
  1000 deterministic iterations, one target: forcing the four-piece onto Arcane is
  **+13.13%**, and forcing it onto Shadow Priest -- which already wears it -- is
  **bit-identical**, while removing it costs Shadow **-11.22%**. So the piece count
  read out of simc's item table and simc's own behaviour agree on both sides, which
  is what makes the offline count usable.

  A per-kit inheritance rule would therefore hand two Mage builds a kit 13% behind
  every other spec's, and an unconditional four-piece would be a hard-coded guess
  that a tier shipping no set would silently invert. The modal state across the
  tier's shipped profiles is neither.

Both are published in the anchor's description rather than assumed, because the
anchor moves numbers and a reader has to be able to see it rather than trust it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .buffsweep import TierSet, sets_for_tier
from .profiles import SpecProfile
from .talenttree import CLASS_IDS

#: simc's own equipment option names, in the order ``util::slot_type_string`` lists
#: them (``engine/util/util.cpp``). This is simc's option vocabulary rather than
#: anything about a tier, so it is written down; a slot simc adds would need a line
#: here, and simc adding a slot is not a seasonal event.
GEAR_SLOTS: tuple[str, ...] = (
    "head",
    "neck",
    "shoulders",
    "shirt",
    "chest",
    "waist",
    "legs",
    "feet",
    "wrists",
    "hands",
    "finger1",
    "finger2",
    "trinket1",
    "trinket2",
    "back",
    "main_hand",
    "off_hand",
    "tabard",
)

#: Where ``ilevel=`` is inserted on a line that does not already carry one: directly
#: after these, which is where simc's own profile generator writes it. Option order
#: inside a gear line does not change what simc builds -- ``item_t::parse_options``
#: collects every option into a named field and ``item_t::init`` decodes them in its
#: own fixed order -- so this is about a rendered line still reading like a simc
#: profile line, not about correctness.
_ILEVEL_AFTER: tuple[str, ...] = ("bonus_id", "id")

_GEAR_LINE = re.compile(rf"^({'|'.join(GEAR_SLOTS)})\s*=(?P<rest>.*)$")


@dataclass(frozen=True)
class GearLine:
    """One equipped item, as simc's profile syntax carries it.

    ``options`` keeps the key/value pairs in the order the source wrote them, and
    ``name`` keeps the bare item name simc puts first (empty for the
    ``trinket1=,id=...`` form the sweeps emit). Round-tripping an untouched line has
    to be byte-identical or a "normalized" kit is silently a different kit.
    """

    slot: str
    name: str
    options: tuple[tuple[str, str], ...]

    @property
    def ilevel(self) -> int | None:
        for key, value in self.options:
            if key == "ilevel":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    @property
    def item_id(self) -> int | None:
        for key, value in self.options:
            if key == "id":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    def has(self, key: str) -> bool:
        return any(existing == key for existing, _ in self.options)

    def render(self) -> str:
        parts = [f"{self.slot}={self.name}"]
        parts.extend(f"{key}={value}" for key, value in self.options)
        return ",".join(parts)

    def with_ilevel(self, level: int) -> GearLine:
        """The same item at a stated item level, with everything else untouched.

        Replacing in place when the line already states one, and inserting after the
        bonus ids otherwise, so a line that already sits at the target renders back
        byte-identically -- which is the cheapest available check that this is a
        normalization rather than a rewrite.
        """
        if self.has("ilevel"):
            options = tuple(
                (key, str(level) if key == "ilevel" else value) for key, value in self.options
            )
            return replace(self, options=options)

        insert_at = len(self.options)
        for anchor in _ILEVEL_AFTER:
            found = [index for index, (key, _) in enumerate(self.options) if key == anchor]
            if found:
                insert_at = found[-1] + 1
                break
        options = (*self.options[:insert_at], ("ilevel", str(level)), *self.options[insert_at:])
        return replace(self, options=options)


def parse_gear_lines(text: str) -> list[GearLine]:
    """Every equipment line in a block of simc profile text, in source order.

    Commented lines are skipped, which matters more than it looks: a profile's
    ``# Gear Summary`` block repeats ``set_bonus=`` and the generator files carry
    whole profiles behind ``#``. Reading those would build a kit nobody wears.
    """
    found: list[GearLine] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _GEAR_LINE.match(line)
        if not match:
            continue
        found.append(_parse_rest(match.group(1), match.group("rest")))
    return found


def _parse_rest(slot: str, rest: str) -> GearLine:
    fields = rest.split(",")
    name = ""
    if fields and "=" not in fields[0]:
        name = fields[0].strip()
        fields = fields[1:]
    options: list[tuple[str, str]] = []
    for field in fields:
        if "=" not in field:
            continue
        key, value = field.split("=", 1)
        options.append((key.strip(), value.strip()))
    return GearLine(slot=slot, name=name, options=tuple(options))


def read_kit(path: Path) -> list[GearLine]:
    """The gear a ``.simc`` file equips.

    The same entry point serves a shipped profile and a harvested character, because
    a harvested kit is written as simc gear lines and there is no second syntax to
    support. Anything that can produce those lines can be anchored.
    """
    return parse_gear_lines(path.read_text(encoding="utf-8", errors="replace"))


#: Piece counts that turn a set on. simc has exactly two thresholds per set and
#: ``item_set_bonus.inc`` states them per row, but every set in the table is 2/4, so
#: the mapping is written rather than derived. A set with different thresholds would
#: need this to become a lookup -- it would not be silently wrong, it would enforce
#: the wrong pair and the description would say which pair it enforced.
SET_NONE, SET_TWO, SET_FOUR = 0, 2, 4


def set_state(pieces: int) -> int:
    """Which bonus state a piece count puts an actor in."""
    if pieces >= SET_FOUR:
        return SET_FOUR
    if pieces >= SET_TWO:
        return SET_TWO
    return SET_NONE


_ITEM_ROW = re.compile(r'^\s*\{ "((?:[^"\\]|\\.)*)", (.*) \},\s*$')
#: ``id_set`` is the 24th field of ``dbc_item_data_t`` (``engine/dbc/item_data.hpp``),
#: counting the name out separately as the row regex does. Read positionally for the
#: same reason ``equipment.discover_items`` reads its fields positionally: the table
#: is a plain C array and there is no simc command that lists items.
#:
#: **This is the second place that parses that array**, and the duplication is worth
#: knowing about: ``equipment.discover_items`` reads different columns of the same
#: rows for the gear pools. A simc struct change breaks both, and loudly -- both
#: check the field count -- but both have to be fixed.
_ID_SET_FIELD = 23
_FIELD_COUNT = 27


def parse_item_sets(simc_dir: Path, ptr: bool = False) -> dict[int, int]:
    """``item id -> id_set`` for every item that belongs to a set.

    This is the join simc itself makes: ``set_bonus_t::initialize`` skips any item
    whose ``parsed.data.id_set`` is zero and otherwise matches it against
    ``item_set_bonus_t::set_id``. Doing the same offline is what lets a kit be asked
    "do you already wear this set" without running a simulation.
    """
    name = "item_data_ptr.inc" if ptr else "item_data.inc"
    path = simc_dir / "engine" / "dbc" / "generated" / name
    found: dict[int, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _ITEM_ROW.match(line)
        if not match:
            continue
        rest = re.sub(r"\{[^}]*\}", "SOCKETS", match.group(2))
        rest = re.sub(r"&__item_stats_data\[(\d+)\]", r"STATS\1", rest)
        fields = [part.strip() for part in rest.split(",")]
        if len(fields) < _FIELD_COUNT:
            continue
        try:
            item_id = int(fields[0])
            id_set = int(fields[_ID_SET_FIELD])
        except ValueError:
            continue
        if id_set:
            found[item_id] = id_set
    return found


def count_set_pieces(
    kit: list[GearLine], set_ids: frozenset[int], item_sets: dict[int, int]
) -> int:
    """How many pieces of one set a kit is wearing.

    An empty ``set_ids`` counts nothing, which is the right answer for a class the
    tier ships no set for rather than something to guard against.
    """
    return sum(1 for line in kit if line.item_id and item_sets.get(line.item_id) in set_ids)


def set_ids_for(sets: list[TierSet], tier: str, wow_class: str | None) -> frozenset[int]:
    """The item-set ids of one tier's set for one class."""
    class_id = CLASS_IDS.get(wow_class or "")
    return frozenset(
        entry.set_id
        for entry in sets_for_tier(sets, tier)
        if entry.set_id and (class_id is None or entry.class_id == class_id)
    )


def derive_set_pieces(
    profiles: list[SpecProfile],
    tier: str,
    sets: list[TierSet],
    item_sets: dict[int, int],
) -> tuple[int, dict[int, int]]:
    """The set state most of the tier's shipped profiles are in, and the tally.

    Each shipped profile is reduced to a *state* (none / two / four) before the vote,
    not to a piece count. MID2's counts are 12 profiles at four pieces and 12 at five,
    which is a coin flip between two numbers that mean the same thing; as states it is
    24 to 4 and there is nothing to flip.

    A tie goes to the **lower** state. A tier genuinely split down the middle has no
    comparable answer, and the lower one is the direction that cannot manufacture a
    win for a computed build -- the same reason the item level takes the band's floor.
    The tally is carried out so a reader sees the split rather than the verdict alone.
    """
    tally: dict[int, int] = {}
    for profile in profiles:
        if profile.unvalidated:
            # Same exclusion as the item-level band: a profile simc did not ship
            # must not vote on what shipped gear looks like.
            continue
        ids = set_ids_for(sets, tier, profile.wow_class)
        if not ids:
            continue
        state = set_state(count_set_pieces(read_kit(profile.path), ids, item_sets))
        tally[state] = tally.get(state, 0) + 1
    if not tally:
        return SET_NONE, tally
    best = max(tally.values())
    return min(state for state, count in tally.items() if count == best), tally


@dataclass(frozen=True)
class AnchorTarget:
    """What every computed build of one tier wears, and where the numbers came from.

    Constructed by ``derive_target`` from the tier's own shipped profiles and simc's
    own set table. Nothing in it is written down in this module, so a new tier needs
    no edit here -- which is the same rule ``spec_coverage``'s reference list and
    ``shipped_item_levels``' band already follow.
    """

    tier: str
    #: The item level every gear line is written to.
    ilevel: int
    #: The band the tier's shipped profiles span, low to high. ``ilevel`` is its floor.
    band: tuple[int, int]
    #: simc's option token for this tier's set, or ``None`` for a tier shipping none.
    set_option: str | None
    #: The set's name for the class being anchored, when one was asked for. Display
    #: only -- the token is what enables the bonus.
    set_name: str = ""
    #: How many pieces the anchor states: 4, 2 or 0. Derived from what the tier's own
    #: shipped profiles wear -- never inherited from the kit being anchored, and never
    #: assumed. MID2's four Mage builds wear none where the other twenty-four wear
    #: four or five, so inheriting would put those two specs 13% behind the field.
    set_pieces: int = SET_FOUR
    #: How the tier voted: state -> how many shipped profiles are in it. Published, so
    #: a split tier reads as split rather than as a verdict.
    set_tally: tuple[tuple[int, int], ...] = ()
    #: Tokens written to zero: every other numbered tier of the same expansion. A kit
    #: still wearing last season's four-piece would otherwise carry it in silently.
    zeroed_options: tuple[str, ...] = ()

    @property
    def ilevel_evidence(self) -> str:
        return f"the floor of the {self.band_label} band the tier's shipped profiles state"

    @property
    def set_evidence(self) -> str:
        """Where the piece count came from, in one line, with the tally behind it."""
        if not self.set_option:
            return f"{self.tier} ships no set bonus"
        if not self.set_tally:
            return "stated without a tally"
        parts = ", ".join(
            f"{count} shipped profile(s) wear {state or 'none'}"
            for state, count in sorted(self.set_tally, reverse=True)
        )
        return f"the state most of the tier's shipped profiles are in ({parts})"

    @property
    def band_label(self) -> str:
        low, high = self.band
        return f"{low}" if low == high else f"{low}-{high}"

    def set_options(self) -> tuple[str, ...]:
        """``set_bonus=`` lines: this tier's set on, every other tier's set off.

        Written in both directions for the reason ``buffsweep.crossover_variants``
        writes its zeroes: a profile already wearing either set would otherwise carry
        it into a state meant to be without it, and the anchor would be describing a
        kit it is not producing.
        """
        options: list[str] = []
        if self.set_option:
            two = 1 if self.set_pieces >= SET_TWO else 0
            four = 1 if self.set_pieces >= SET_FOUR else 0
            options.append(f"set_bonus={self.set_option}_2pc={two}")
            options.append(f"set_bonus={self.set_option}_4pc={four}")
        for token in self.zeroed_options:
            options.append(f"set_bonus={token}_2pc=0")
            options.append(f"set_bonus={token}_4pc=0")
        return tuple(options)

    def to_json(self) -> dict:
        return {
            "tier": self.tier,
            "itemLevel": self.ilevel,
            "itemLevelBand": [self.band[0], self.band[1]],
            "itemLevelEvidence": self.ilevel_evidence,
            "tierSet": {
                "option": self.set_option,
                "name": self.set_name,
                "pieces": self.set_pieces if self.set_option else 0,
                "evidence": self.set_evidence,
            },
            "zeroedSets": list(self.zeroed_options),
        }


class AnchorError(ValueError):
    """A tier whose own profiles cannot say what a comparable kit looks like."""


def derive_target(
    profiles: list[SpecProfile],
    tier: str,
    sets: list[TierSet] | None = None,
    item_sets: dict[int, int] | None = None,
    wow_class: str | None = None,
) -> AnchorTarget:
    """What a computed build of this tier should wear, read off the tier itself.

    ``profiles`` is every profile of the tier; disabled ones are filtered out inside
    ``dataset.shipped_item_levels`` so one cannot drag the anchor toward its own gap
    and quietly excuse it.

    A tier whose shipped profiles state no item level at all is a **refusal**, not a
    default. Silence on some profiles is ordinary: measured on 2026-08-23, six of
    MID2's twenty-eight shipped damage profiles state none on any gear line -- both
    Assassination Rogue builds, both Elemental and both Enhancement Shaman builds --
    and simc resolves their gear from the bonus ids anyway. Silence on *every*
    profile is different: the tier then cannot say what comparable means, and
    inventing a number would put every computed build at an item level nobody chose.

    ``sets`` without ``item_sets`` is a refusal for the same reason. The set token
    alone says *which* set exists, not which state a comparable build is in, and the
    two answers are 13% apart on a real MID2 build. Guessing there would be the one
    hard-coded number this module exists to avoid, so the caller is told to pass
    ``parse_item_sets(simc_dir)`` rather than being given a default.
    """
    from .dataset import shipped_item_levels

    band = shipped_item_levels(profiles)
    if band is None:
        raise AnchorError(
            f"no shipped profile of {tier} states an item level on any gear line, so "
            f"there is no band to anchor inside. A computed build cannot be made "
            f"comparable to profiles that do not say what they wear."
        )

    option: str | None = None
    name = ""
    zeroed: list[str] = []
    pieces = SET_NONE
    tally: dict[int, int] = {}
    if sets is not None:
        if item_sets is None:
            raise AnchorError(
                "a set token was supplied without simc's item table, so which set "
                "state is comparable cannot be derived. Pass "
                "item_sets=parse_item_sets(simc_dir), or pass sets=None to anchor "
                "gear alone."
            )
        current = sets_for_tier(sets, tier)
        options = sorted({entry.option for entry in current})
        option = options[0] if options else None
        if len(options) > 1:
            # Not seen on MID1 or MID2 -- both carry exactly one token across all
            # thirteen classes -- but a tier that grew a second one would otherwise
            # have one of them picked silently.
            raise AnchorError(
                f"tier {tier} carries more than one set bonus token ({', '.join(options)}); "
                f"which one a computed build wears is a decision, not a default"
            )
        if option and wow_class:
            class_id = CLASS_IDS.get(wow_class)
            named = [entry.name for entry in current if entry.class_id == class_id]
            name = named[0] if named else ""
        if option:
            pieces, tally = derive_set_pieces(profiles, tier, sets, item_sets)
        zeroed = sorted(_other_tier_options(sets, tier, option))

    return AnchorTarget(
        tier=tier,
        ilevel=band[0],
        band=band,
        set_option=option,
        set_name=name,
        set_pieces=pieces,
        set_tally=tuple(sorted(tally.items())),
        zeroed_options=tuple(zeroed),
    )


_TIER_NAME = re.compile(r"([A-Za-z]+)(\d+)")


def _other_tier_options(sets: list[TierSet], tier: str, current: str | None) -> set[str]:
    """Set tokens belonging to other numbered tiers of the same expansion.

    Bounded on purpose. Zeroing every token simc ships would be a wall of no-op
    options on every variant; zeroing none would let a kit carry a previous season's
    four-piece into a build that is supposed to be wearing this one's. The
    expansion's own other seasons are the set a real character might still be wearing,
    and the prefix is read off the tier name with the same expression
    ``profiles.available_tiers`` uses to decide what a tier name is.
    """
    match = _TIER_NAME.fullmatch(tier)
    if not match:
        return set()
    prefix = match.group(1)
    found: set[str] = set()
    for entry in sets:
        other = _TIER_NAME.fullmatch(entry.tier)
        if not other or other.group(1) != prefix or entry.tier == tier:
            continue
        if entry.option and entry.option != current:
            found.add(entry.option)
    return found


@dataclass(frozen=True)
class SlotChange:
    """One slot's item level before and after. ``before`` is None when none was stated."""

    slot: str
    before: int | None
    after: int

    def to_json(self) -> dict:
        return {"slot": self.slot, "from": self.before, "to": self.after}


@dataclass(frozen=True)
class GearAnchor:
    """A kit normalized to one tier's anchor, plus what that did to it."""

    target: AnchorTarget
    lines: tuple[GearLine, ...]
    changes: tuple[SlotChange, ...]
    #: Slots whose stated item level already equalled the target, so nothing moved.
    unchanged: tuple[str, ...]
    #: Slots carrying a gem, an enchant or crafted stats -- all preserved verbatim.
    #: Counted rather than assumed because "preserved" is a claim, and on rings it is
    #: worth an order of magnitude more than the item-level move beside it.
    gemmed: tuple[str, ...] = ()
    enchanted: tuple[str, ...] = ()
    crafted: tuple[str, ...] = ()

    def options(self) -> tuple[str, ...]:
        """Every simc option line the anchor consists of, gear first then the set.

        Usable directly as a ``Profileset``'s options, which is the point: two
        variants that both carry these differ in nothing but what the caller varies.
        """
        return (*(line.render() for line in self.lines), *self.target.set_options())

    def to_json(self) -> dict:
        """The description published beside a computed build.

        A reader should be able to see the anchor rather than take it on trust, which
        means the item level, where it came from, the set state, and which slots the
        normalization actually moved -- an anchor that moved nothing and one that
        moved every slot by 45 levels are very different claims about the source kit.
        """
        return {
            **self.target.to_json(),
            "slotsNormalized": [change.to_json() for change in self.changes],
            "slotsAlreadyAtTarget": list(self.unchanged),
            "preserved": {
                "gems": list(self.gemmed),
                "enchants": list(self.enchanted),
                "craftedStats": list(self.crafted),
            },
        }


def apply(target: AnchorTarget, kit: list[GearLine]) -> GearAnchor:
    """Write the anchor onto a kit: one item level everywhere, the set stated.

    Every line is rewritten even when its level already matches, so the emitted kit
    is a function of the target alone and two kits anchored to the same target differ
    only where the *items* differ. A line already at the target renders back
    byte-identically, so "rewritten" costs nothing.
    """
    lines: list[GearLine] = []
    changes: list[SlotChange] = []
    unchanged: list[str] = []
    gemmed: list[str] = []
    enchanted: list[str] = []
    crafted: list[str] = []

    for line in kit:
        before = line.ilevel
        lines.append(line.with_ilevel(target.ilevel))
        if before == target.ilevel:
            unchanged.append(line.slot)
        else:
            changes.append(SlotChange(slot=line.slot, before=before, after=target.ilevel))
        if line.has("gem_id"):
            gemmed.append(line.slot)
        if line.has("enchant_id"):
            enchanted.append(line.slot)
        if line.has("crafted_stats"):
            crafted.append(line.slot)

    return GearAnchor(
        target=target,
        lines=tuple(lines),
        changes=tuple(changes),
        unchanged=tuple(unchanged),
        gemmed=tuple(gemmed),
        enchanted=tuple(enchanted),
        crafted=tuple(crafted),
    )


def describe(anchor: GearAnchor) -> str:
    """One sentence for a log line or a caveat, saying what the anchor did."""
    target = anchor.target
    moved = len(anchor.changes)
    span = sorted({change.before for change in anchor.changes if change.before is not None})
    from_part = ""
    if span:
        written = f"{span[0]}" if len(span) == 1 else f"{span[0]}-{span[-1]}"
        from_part = f" from {written}"
    silent = sum(1 for change in anchor.changes if change.before is None)
    if silent:
        from_part += f" ({silent} slot(s) stated none)"
    if not target.set_option:
        set_part = ", with no tier set (this tier ships none)"
    elif target.set_pieces:
        set_part = f", wearing the {target.set_pieces}-piece {target.set_name or target.set_option}"
    else:
        set_part = (
            f", with no {target.set_name or target.set_option} "
            f"(most of the tier's shipped profiles wear none)"
        )
    return (
        f"Gear anchored to item level {target.ilevel}, the floor of the "
        f"{target.band_label} band {target.tier}'s shipped profiles state: "
        f"{moved} slot(s) normalized{from_part}, "
        f"{len(anchor.unchanged)} already there{set_part}. "
        f"Gems, enchants and crafted stats preserved."
    )
