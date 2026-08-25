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
token is written at the state derived below, and **every other set token the class
can wear** is written to zero, so the anchor *states* the set state rather than
inheriting it. Not just the other seasons of this expansion: measured on 69a46e1, ten
of MID2's forty damage profiles wear two or more pieces of some other set, one of
them a full four-piece from the expansion before. See ``other_set_tokens``.

**Every slot is written, including the ones the kit has nothing in.** A profileset's
options apply *on top of* the base profile, so a slot the kit is silent about keeps
the base actor's item at the base actor's item level. Measured on Arcane Mage, 1000
deterministic iterations, one target: a bare ``off_hand=`` on an otherwise inert
profileset moved 176,582.7 to 158,651.4, and emptying two slots the profile does not
use returned 176,582.7 unchanged. So the empty lines are worth 10% where they bite
and nothing where they do not.

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

  **The tier's shipped profiles disagree with each other**, and *how many* disagree
  is a fact with a date on it. Counting set pieces by ``dbc_item_data_t::id_set``
  against the set table's ``set_id`` -- which is exactly what
  ``set_bonus_t::initialize`` does -- over MID2's 28 shipped damage profiles:

      simc 69a46e1, 2026-08-21   24 wear four or five, 4 wear none
                                 (both Arcane and both Frost Mage builds)
      simc 22b442e, 2026-08-22   26 wear four or five, 2 wear none
                                 (both Arcane builds)

  Both are true at their own revision: simc gave Frost Mage the set between the two.
  **22b442e is the revision that matters**, because the published dataset was
  generated on 2026-08-22, so only the two Arcane builds lack it today.

  Its size, measured profileset against profileset at 1000 deterministic iterations,
  one target: forcing the four-piece onto Arcane is **+13.13%**, and forcing it onto
  Shadow Priest -- which already wears it -- is **bit-identical**, while removing it
  costs Shadow **-11.22%**. So the piece count read out of simc's item table and
  simc's own behaviour agree on both sides, which is what makes the offline count
  usable. Note that Shadow Priest states no ``set_bonus=`` line at all; it wears the
  set by equipping the pieces, which is precisely why the count is taken from the
  items rather than from the profile's options.

  A per-kit inheritance rule would therefore hand the Arcane builds a kit 13% behind
  every other spec's, and an unconditional four-piece would be a hard-coded guess
  that a tier shipping no set would silently invert. The modal state across the
  tier's shipped profiles is neither.

  Worth knowing where that Arcane gap comes from, and **this file used to name the
  wrong mechanism** -- corrected here from PR #35, which measured it. The old text
  said Arcane's profile "has its ``set_bonus=`` lines commented out", implying the
  build opts out of a set it owns. It does carry those two lines commented; so does
  **every one of the 35 MID2 profiles, and not one carries an active one** (measured
  on 22b442e). A convention every profile follows cannot explain a difference between
  two of them.

  The real mechanism is the **equipped items**. Fire wears
  ``primal_leywardens_manaflux`` (id 271562, ``id_set`` 2060) where Arcane wears
  ``ornaments_of_the_eternal_coil`` (id 268241, no ``id_set``) -- same slot, same item
  level, same bonus ids -- and the same substitution runs through head, chest, hands
  and legs. Arcane is not switching a set off; it is wearing four different items,
  none of which is a tier piece. That is a property of simc's profile, not of this
  repository, and it is the kind of gap ``dataset.gear_caveat`` exists to flag.

Both are published in the anchor's description rather than assumed, because the
anchor moves numbers and a reader has to be able to see it rather than trust it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from .buffsweep import TierSet, sets_for_tier
from .profiles import SpecProfile
from .talenttree import CLASS_IDS

#: One entry per equipment slot, in the order ``util::slot_type_string`` lists them
#: (``engine/util/util.cpp``). This is simc's vocabulary rather than anything about a
#: tier, so it is written down; a slot simc adds would need a line here, and simc
#: adding a slot is not a seasonal event.
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

#: **simc's option parser accepts more slot names than ``slot_type_string`` emits**,
#: and reading only the emitted ones is silent data loss rather than a parse error.
#: Every alias here is a second ``add_option`` against the *same* ``items[SLOT_*]``
#: entry in ``player_t::create_options`` (``engine/player/player.cpp:13276-13286``),
#: so **neither spelling is the correct one** and the repo-wide rule is: a reader
#: accepts every alias simc accepts, an emitter writes what simc ships.
#:
#: The two spellings split the population. Measured on simc 69a46e1: every *shipped*
#: MID2 profile writes the plural, and the singular appears only in the profiles simc
#: **disabled** -- Feral Druid and both Aldrachi Reaver Demon Hunters -- which is
#: exactly the set this module exists to rescue. So ``GEAR_SLOTS`` above keeps what
#: simc emits and nothing here renames anything; this only widens what is read.
#: Dropping these lines left Havoc's shoulder at item level 723 and its wrist at 720
#: against an anchor of 334, while the description reported the other fourteen slots
#: as normalized and said nothing about the two it never saw.
SLOT_ALIASES: dict[str, str] = {
    "shoulder": "shoulders",
    "leg": "legs",
    "foot": "feet",
    "wrist": "wrists",
    "hand": "hands",
    "ring1": "finger1",
    "ring2": "finger2",
}

#: Where ``ilevel=`` is inserted on a line that does not already carry one: directly
#: after these, which is where simc's own profile generator writes it. Option order
#: inside a gear line does not change what simc builds -- ``item_t::parse_options``
#: collects every option into a named field and ``item_t::init`` decodes them in its
#: own fixed order -- so this is about a rendered line still reading like a simc
#: profile line, not about correctness.
_ILEVEL_AFTER: tuple[str, ...] = ("bonus_id", "id")

#: Both spellings of a gem and of an enchant, because a profile uses either.
#: ``item_t::parse_options`` registers ``gem_id`` beside ``gems`` and ``enchant_id``
#: beside ``enchant``, the id form taking numbers and the other taking simc's own
#: names. Counted over the gear lines on 69a46e1: **six MID2 lines use the name
#: form** -- five on Blood Deathbringer, one on Havoc Aldrachi Reaver -- against 369
#: using ``enchant_id``, and MID1 uses it 38 times across eight profiles.
#:
#: The enchant round-trips either way, because the whole line is preserved; what was
#: wrong was the *evidence*. ``to_json()['preserved']['enchants']`` came back empty
#: for a slot that is enchanted, and that field exists precisely because "preserved"
#: is a claim rather than an observation.
_GEM_OPTIONS: tuple[str, ...] = ("gem_id", "gems")
_ENCHANT_OPTIONS: tuple[str, ...] = ("enchant_id", "enchant")

#: Longest first, so ``hands=`` cannot be read as the ``hand`` alias with a stray
#: ``s``. Python's alternation backtracks and would get there anyway; the sort makes
#: it true by construction rather than by a property of the regex engine.
_SLOT_NAMES = sorted((*GEAR_SLOTS, *SLOT_ALIASES), key=len, reverse=True)
_GEAR_LINE = re.compile(rf"^({'|'.join(_SLOT_NAMES)})\s*=(?P<rest>.*)$")


@dataclass(frozen=True)
class GearLine:
    """One equipped item, as simc's profile syntax carries it.

    ``options`` keeps the key/value pairs in the order the source wrote them, and
    ``name`` keeps the bare item name simc puts first (empty for the
    ``trinket1=,id=...`` form the sweeps emit). Round-tripping an untouched line has
    to be byte-identical or a "normalized" kit is silently a different kit.

    ``slot`` is always the canonical name, so two spellings of one slot compare and
    deduplicate as one slot; ``alias`` carries the spelling the source used when it
    used one of ``SLOT_ALIASES``, and ``render`` writes that back. Both halves are
    load-bearing: without the first, a kit written with ``wrist=`` would be handed an
    empty ``wrists=`` beside it and end up wearing neither.
    """

    slot: str
    name: str
    options: tuple[tuple[str, str], ...]
    #: The source's spelling, when it differed from ``slot``. Empty otherwise.
    alias: str = ""

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

    def has_any(self, keys: tuple[str, ...]) -> bool:
        return any(existing in keys for existing, _ in self.options)

    def render(self) -> str:
        parts = [f"{self.alias or self.slot}={self.name}"]
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


def _parse_rest(written: str, rest: str) -> GearLine:
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
    slot = SLOT_ALIASES.get(written, written)
    return GearLine(
        slot=slot,
        name=name,
        options=tuple(options),
        alias="" if slot == written else written,
    )


def read_kit(path: Path) -> list[GearLine]:
    """The gear a ``.simc`` file equips.

    The same entry point serves a shipped profile and a harvested character, because
    a harvested kit is written as simc gear lines and there is no second syntax to
    support. Anything that can produce those lines can be anchored.
    """
    return parse_gear_lines(path.read_text(encoding="utf-8", errors="replace"))


#: The piece counts a raid tier's set turns on at, and the default when a caller has
#: no set in hand. Named because they are what the tally and the tests read, not
#: because they are true of every set: **thresholds are per set and are read out of
#: the table**. Enumerated on simc 69a46e1, of the 29 sets in ``item_set_bonus.inc``
#: 17 carry a 2-piece alone, ``MID_UWP`` carries 2 and 3, and ``DF_RT`` carries 2, 4
#: and 6. Both Midnight raid tiers are 2/4, which is why writing 2/4 down was
#: correct for MID2 and wrong as a justification.
SET_NONE, SET_TWO, SET_FOUR = 0, 2, 4
DEFAULT_THRESHOLDS: tuple[int, ...] = (SET_TWO, SET_FOUR)


def set_state(pieces: int, thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS) -> int:
    """Which bonus state a piece count puts an actor in: the highest threshold met."""
    met = [threshold for threshold in thresholds if pieces >= threshold]
    return max(met) if met else SET_NONE


@dataclass(frozen=True)
class SetToken:
    """One set's option name and the piece counts it ships a bonus for.

    Carried together so every ``(token, threshold)`` pair written is one simc has.
    **Cross-checked against simc rather than reasoned about**, on 69a46e1: the
    vocabulary ``generate_set_bonus_options()`` prints when an option is rejected
    holds 42 entries, and the 42 this emits for MID2's Arcane Mage are the same 42.

    Where simc's tolerance actually ends, measured the same day, because guessing it
    the pessimistic way is still guessing:

    * ``bite_of_zuljan_4pc=0`` on a set with no four-piece is **accepted** and the
      sim runs. ``parse_set_bonus_option`` validates the token and only bounds the
      index by ``B_MAX``.
    * ``bite_of_zuljan_9pc=0`` is rejected, as is any token no set carries.
    * A token belonging to a set the actor's **class** has no row for is rejected:
      ``shadowlands_season_3_2pc=0`` on MID2's Devastation Evoker exits **80** with
      no DPS, because ``SL3`` ships for twelve classes and not that one.

    So the threshold is about writing what exists; the *class* is what avoids the
    failure. And do not take the printed vocabulary as the class-safe list --
    measured, it is **byte-identical for Mage and Evoker**, so it advertises the very
    option simc then refuses. simc's error message is a list of what the table holds,
    not of what this actor may pass.
    """

    option: str
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS

    def states(self, pieces: int) -> tuple[str, ...]:
        """``<token>_<N>pc=<0|1>`` clauses putting this set at ``pieces``.

        Clauses rather than whole options, because simc wants them on **one**
        ``set_bonus=`` line -- see ``AnchorTarget.set_options``.
        """
        return tuple(
            f"{self.option}_{threshold}pc={1 if pieces >= threshold else 0}"
            for threshold in self.thresholds
        )


class AnchorError(ValueError):
    """A tier or a table that cannot say what a comparable kit looks like."""


_ITEM_ROW = re.compile(r'^\s*\{ "((?:[^"\\]|\\.)*)", (.*) \},\s*$')

#: ``id_set`` is the 24th field of ``dbc_item_data_t`` (``engine/dbc/item_data.hpp``),
#: counting the name out separately as the row regex does. Read positionally for the
#: same reason ``equipment.discover_items`` reads its fields positionally: the table
#: is a plain C array and there is no simc command that lists items.
_ID_SET_FIELD = 23

#: Fields per row, counting the name out separately. Measured on simc 69a46e1: **all
#: 115,470 rows are exactly this wide**, none narrower and none wider, which is why
#: ``parse_item_sets`` tests equality rather than a lower bound.
ITEM_FIELD_COUNT = 27


def iter_item_rows(text: str) -> Iterator[tuple[str, list[str]]]:
    """``(name, fields)`` for every ``dbc_item_data_t`` row in ``item_data.inc``.

    The two substitutions are what make a positional split possible at all: a socket
    array and a stats pointer both contain commas, so each is collapsed to one token
    before the row is split.

    **``equipment.discover_items`` decodes the same rows the same way**, for different
    columns -- it wants item level, quality and inventory type for the gear pools,
    this wants ``id_set``. Sharing one decoder was written and then backed out, to
    keep this branch clear of a parallel change to that module; a simc struct change
    therefore still has to be fixed in both places, and this comment is the pointer
    between them. Rows of the wrong width are yielded rather than swallowed, so the
    caller decides whether that is something to skip or something to refuse on --
    the two modules answer differently and neither can tell for the other.
    """
    for line in text.splitlines():
        match = _ITEM_ROW.match(line)
        if not match:
            continue
        rest = re.sub(r"\{[^}]*\}", "SOCKETS", match.group(2))
        rest = re.sub(r"&__item_stats_data\[(\d+)\]", r"STATS\1", rest)
        yield match.group(1), [part.strip() for part in rest.split(",")]


def parse_item_sets(simc_dir: Path, ptr: bool = False) -> dict[int, int]:
    """``item id -> id_set`` for every item that belongs to a set.

    This is the join simc itself makes: ``set_bonus_t::initialize`` skips any item
    whose ``parsed.data.id_set`` is zero and otherwise matches it against
    ``item_set_bonus_t::set_id``. Doing the same offline is what lets a kit be asked
    "do you already wear this set" without running a simulation.

    **It refuses rather than returning what it managed**, and that is the whole of the
    difference from what it used to do. Skipping a malformed row and returning the
    accumulated dict means a simc struct change makes every row fail and the function
    answer ``{}`` -- which is not ``None``, so ``derive_target`` accepted it, counted
    zero pieces on every profile, and published *"the state most of the tier's shipped
    profiles are in (28 shipped profile(s) wear none)"* as derived evidence for a kit
    with the set switched off. By this module's own measurement that costs 13.13% on
    Arcane Mage, stated as a finding about the tier.

    Two guards, because the failure has two shapes:

    * **Nothing decoded.** No rows at all, or none of the expected width, or none
      belonging to a set. The last one is worth refusing on even though it looks like
      a legitimate answer: 12,260 of simc's 115,470 items carry an ``id_set`` on
      69a46e1, and a table where *none* does is a table that was not read.
    * **A row of the wrong width.** Measured on 69a46e1, all 115,470 rows are exactly
      ``ITEM_FIELD_COUNT`` fields. So the test is equality, not the lower bound it
      was: a field *inserted* before index 23 passes a lower bound and silently
      turns ``id_set`` into its neighbour, which is the same wrong answer wearing a
      plausible face.
    """
    name = "item_data_ptr.inc" if ptr else "item_data.inc"
    path = simc_dir / "engine" / "dbc" / "generated" / name
    found: dict[int, int] = {}
    rows = 0
    malformed = 0
    for _name, fields in iter_item_rows(path.read_text(encoding="utf-8", errors="replace")):
        rows += 1
        if len(fields) != ITEM_FIELD_COUNT:
            malformed += 1
            continue
        try:
            item_id = int(fields[0])
            id_set = int(fields[_ID_SET_FIELD])
        except ValueError:
            continue
        if id_set:
            found[item_id] = id_set
    if malformed:
        raise AnchorError(
            f"{malformed} of {rows} rows in {path.name} are not {ITEM_FIELD_COUNT} "
            f"fields wide, so field {_ID_SET_FIELD} is no longer id_set. Re-read "
            f"dbc_item_data_t in engine/dbc/item_data.hpp and fix _ID_SET_FIELD; "
            f"equipment.discover_items reads the same rows and needs the same fix."
        )
    if not found:
        raise AnchorError(
            f"no item in {path.name} belongs to a set ({rows} rows read), so no kit "
            f"can be asked whether it already wears one. That is not a tier without "
            f"sets -- it is a table that was not read."
        )
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
    """The item-set ids of one tier's set for one class.

    ``wow_class=None`` means every class's, which is what a caller asking about the
    tier rather than about an actor wants. **A name this table does not know is a
    refusal, not that.** It used to fall through the same ``None`` branch, so
    ``set_ids_for(sets, 'MID2', 'Deathknight')`` -- simc's own token spelling against
    a table keyed on ``'Death Knight'`` -- returned all thirteen MID2 set ids instead
    of the one, and ``count_set_pieces`` would then have counted a Mage's robes as
    Death Knight tier. A misspelling has to be told apart from an absence.
    """
    class_id: int | None = None
    if wow_class:
        class_id = CLASS_IDS.get(wow_class)
        if class_id is None:
            raise AnchorError(
                f"simc knows no class named {wow_class!r}; its own token spelling is "
                f"'deathknight' where this table is keyed on 'Death Knight'. Pass a "
                f"name from talenttree.CLASS_IDS, or None for every class's."
            )
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
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
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
        state = set_state(count_set_pieces(read_kit(profile.path), ids, item_sets), thresholds)
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
    #: The piece counts that token ships a bonus for, read off simc's table rather
    #: than assumed to be 2 and 4. See ``SetToken``.
    set_thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS
    #: The set's name for the class being anchored, when one was asked for. Display
    #: only -- the token is what enables the bonus.
    set_name: str = ""
    #: How many pieces the anchor states. Derived from what the tier's own shipped
    #: profiles wear -- never inherited from the kit being anchored, and never
    #: assumed. MID2's Arcane builds wear none where the rest of the tier wears four
    #: or five, so inheriting would put those specs 13% behind the field.
    #:
    #: **The default is no set**, and the default is the part worth stating: it used
    #: to be the four-piece, so an ``AnchorTarget`` built without deriving the state
    #: emitted ``_2pc=1``/``_4pc=1`` and published *"stated without a tally"* beside
    #: it -- hard-coding the exact thing the field docstring above forbids. Nothing
    #: undeclared can manufacture a win from here, which is the same direction the
    #: item level's band floor and the tally's tie rule already take.
    set_pieces: int = SET_NONE
    #: How the tier voted: state -> how many shipped profiles are in it. Published, so
    #: a split tier reads as split rather than as a verdict.
    set_tally: tuple[tuple[int, int], ...] = ()
    #: Written to zero: every other set token the class can wear, each at its own
    #: thresholds. A kit still wearing last season's four-piece -- or one of this
    #: expansion's crafted 2-pieces, which ten of MID2's forty damage profiles do --
    #: would otherwise carry it into the anchor silently.
    zeroed: tuple[SetToken, ...] = ()

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

    @property
    def zeroed_options(self) -> tuple[str, ...]:
        """The tokens written to zero, for display and for the published description."""
        return tuple(token.option for token in self.zeroed)

    def set_states(self) -> tuple[str, ...]:
        """Every ``<token>_<N>pc=<0|1>`` the anchor states, this tier's set first.

        Written in both directions for the reason ``buffsweep.crossover_variants``
        writes its zeroes: a profile already wearing either set would otherwise carry
        it into a state meant to be without it, and the anchor would be describing a
        kit it is not producing.
        """
        states: list[str] = []
        if self.set_option:
            states.extend(SetToken(self.set_option, self.set_thresholds).states(self.set_pieces))
        for token in self.zeroed:
            states.extend(token.states(SET_NONE))
        return tuple(states)

    def set_options(self) -> tuple[str, ...]:
        """One ``set_bonus=`` line carrying all of them, slash-delimited.

        One line rather than one per state, because that is what simc asks for and
        the ask is not cosmetic-looking until you read ``parse_set_bonus``: a repeated
        ``set_bonus=`` **appends** with a ``/`` and raises a MODERATE error every time
        it does. Forty-one zeroed states would be forty-one warnings per actor,
        drowning the one that matters.

        The two forms are otherwise the same run, measured rather than assumed --
        Shadow Priest, MID2, 1000 deterministic iterations, one target, the set
        switched off: two lines gave **187,312.2** and the slash-delimited line gave
        **187,312.2**, bit-identical, against 210,980.8 with the set left alone. So
        this is a change of spelling and not of state.
        """
        states = self.set_states()
        return (f"set_bonus={'/'.join(states)}",) if states else ()

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
    zeroed: tuple[SetToken, ...] = ()
    thresholds = DEFAULT_THRESHOLDS
    pieces = SET_NONE
    tally: dict[int, int] = {}
    class_id = CLASS_IDS.get(wow_class) if wow_class else None
    if wow_class and class_id is None:
        raise AnchorError(
            f"simc knows no class named {wow_class!r}; its own token spelling is "
            f"'deathknight' where this table is keyed on 'Death Knight'. Pass a name "
            f"from talenttree.CLASS_IDS, or None to leave the anchor class-agnostic."
        )
    if sets is not None:
        if not item_sets:
            # Empty and absent are the same refusal, deliberately. ``{}`` is what a
            # struct change used to produce, and it read as "nothing wears a set" --
            # a full set of plausible numbers with the set switched off on every
            # build. ``parse_item_sets`` refuses at the source now; this catches a
            # caller that built the dict some other way.
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
        if option:
            named = [entry for entry in current if entry.class_id == class_id]
            name = named[0].name if named else ""
            for entry in current:
                if entry.thresholds:
                    thresholds = entry.thresholds
                    break
            pieces, tally = derive_set_pieces(profiles, tier, sets, item_sets, thresholds)
        zeroed = other_set_tokens(sets, option, class_id)

    return AnchorTarget(
        tier=tier,
        ilevel=band[0],
        band=band,
        set_option=option,
        set_thresholds=thresholds,
        set_name=name,
        set_pieces=pieces,
        set_tally=tuple(sorted(tally.items())),
        zeroed=zeroed,
    )


def other_set_tokens(
    sets: list[TierSet], current: str | None, class_id: int | None
) -> tuple[SetToken, ...]:
    """Every set token but the anchor's own that the class being anchored can wear.

    **This used to be bounded to other *numbered* tiers of the same expansion**, on
    the argument that zeroing everything would be a wall of no-op options and that a
    previous season's set is the only one a real character might still be wearing.
    Both halves were wrong, and measured wrong on MID2's own profiles at simc
    69a46e1:

    * The bound was expressed as ``fullmatch("([A-Za-z]+)(\\d+)")`` on the tier label,
      which the eight un-numbered Midnight tiers fail on the underscore. ``MID_BOZ``,
      ``MID_VB`` and six more are real 2-piece bonuses of this very expansion, and
      **ten of MID2's forty damage profiles wear two or more pieces of one** -- all
      four Frost and Unholy Death Knight builds carry ``bite_of_zuljan``, Balance
      Druid carries ``voidlight_bindings``. So the anchor left an unstated set bonus
      standing on a quarter of the tier.
    * "Other seasons of this expansion" is not the set a character might still be
      wearing either. MID2's Havoc profile wears a full ``thewarwithin_season_3``
      four-piece, from the expansion before.

    Zeroing everything is the only bound that cannot let an unstated bonus through,
    which is this module's whole thesis applied to itself. The cost is measured and
    small: 40 zeroed lines for a Mage on 69a46e1, 42 set_bonus lines in all, parsed
    once per actor.

    Scoped by class because the option is only valid for a class the set exists for
    -- ``set_bonus_t::parse_set_bonus_option`` skips rows of another class and then
    rejects the whole option. Measured on 69a46e1: ``SL3`` ships for twelve classes
    and not for Evoker, and ``set_bonus=shadowlands_season_3_2pc=0`` on MID2's
    Devastation profile exits **80** with no DPS at all. With no class in hand only
    the tokens *every* class in the table can wear are returned, which is the
    conservative direction: an option nobody can reach costs a line, one nobody can
    parse costs the run.
    """
    by_option: dict[str, tuple[set[int], set[int]]] = {}
    for entry in sets:
        if not entry.option or entry.option == current:
            continue
        classes, thresholds = by_option.setdefault(entry.option, (set(), set()))
        classes.add(entry.class_id)
        thresholds.update(entry.thresholds or DEFAULT_THRESHOLDS)
    every_class = {entry.class_id for entry in sets}
    found: list[SetToken] = []
    for option, (classes, thresholds) in by_option.items():
        wearable = class_id in classes if class_id is not None else classes >= every_class
        if wearable:
            found.append(SetToken(option=option, thresholds=tuple(sorted(thresholds))))
    return tuple(sorted(found, key=lambda token: token.option))


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
    #: Slots the kit does not carry, written as an explicit empty option. See
    #: ``options`` -- these are what stops the base actor's own item surviving inside
    #: something the description calls anchored.
    emptied: tuple[str, ...] = ()

    def options(self) -> tuple[str, ...]:
        """Every simc option line the anchor consists of, gear first then the set.

        Usable directly as a ``Profileset``'s options, which is the point: two
        variants that both carry these differ in nothing but what the caller varies.

        **Every slot is written, including the ones the kit has nothing in.** A
        profileset applies its options *on top of* the base profile, so a slot the
        kit is silent about keeps whatever the base actor wears -- at the base
        actor's item level, inside a kit this then describes as anchored.
        MID2's Windwalker Monk kit has fifteen gear lines and its Arcane Mage sixteen,
        so anchoring the Monk against the Mage as base left an off-hand nobody chose,
        at an item level the anchor did not set. An empty option clears the slot,
        which is the one thing that makes the emitted kit a function of the kit alone.
        """
        empties = tuple(f"{slot}=" for slot in self.emptied)
        return (*(line.render() for line in self.lines), *empties, *self.target.set_options())

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
            "slotsEmptied": list(self.emptied),
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
        if line.has_any(_GEM_OPTIONS):
            gemmed.append(line.slot)
        if line.has_any(_ENCHANT_OPTIONS):
            enchanted.append(line.slot)
        if line.has("crafted_stats"):
            crafted.append(line.slot)

    worn = {line.slot for line in kit}
    return GearAnchor(
        target=target,
        lines=tuple(lines),
        changes=tuple(changes),
        unchanged=tuple(unchanged),
        gemmed=tuple(gemmed),
        enchanted=tuple(enchanted),
        crafted=tuple(crafted),
        emptied=tuple(slot for slot in GEAR_SLOTS if slot not in worn),
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
        # ``set_evidence`` rather than a sentence of its own, because a target built
        # without a tally would otherwise announce a vote that never happened -- the
        # same claim-without-evidence the SET_NONE default exists to prevent.
        set_part = f", with no {target.set_name or target.set_option} ({target.set_evidence})"
    emptied = f", {len(anchor.emptied)} emptied" if anchor.emptied else ""
    return (
        f"Gear anchored to item level {target.ilevel}, the floor of the "
        f"{target.band_label} band {target.tier}'s shipped profiles state: "
        f"{moved} slot(s) normalized{from_part}, "
        f"{len(anchor.unchanged)} already there{emptied}{set_part}. "
        f"Gems, enchants and crafted stats preserved."
    )


def display_json(anchor: GearAnchor, profile: str | None = None) -> dict:
    """The anchor as the site's ``DpsGearAnchor`` wants it, which is a different shape.

    ``to_json`` above is the **record**: every slot the normalization touched, the band,
    the tally behind the set state. ``DpsGearAnchor`` in ``dps-data.models.ts`` is the
    **reading**: a label, one list of what was held constant and one of what was left
    alone, because that is what the panel prints. Only ``itemLevel`` is common to both,
    and ``preserved``/``tierSet`` appear in both with different kinds.

    The mismatch was reported by wtt-frontend#130 and deliberately left for whoever
    wired the producer to settle. **It is settled here, on the pipeline side**: the
    frontend model is untouched and this function projects onto it. The reasoning is
    that the frontend's shape is the one with a consumer -- a published field nothing
    reads is a field that drifts -- while the record has no reader outside this
    repository and loses nothing by staying here. Every fact ``to_json`` carries is
    still expressed, as a sentence rather than as a column.

    Nothing is dropped silently: an anchor that normalized nothing produces an empty
    ``normalised`` list, which the panel renders as absent rather than as "held
    nothing constant".
    """
    target = anchor.target
    normalised: list[str] = []
    if anchor.changes:
        span = sorted({c.before for c in anchor.changes if c.before is not None})
        if not span:
            written = ""
        elif len(span) == 1:
            written = f" from {span[0]}"
        else:
            written = f" from {span[0]}-{span[-1]}"
        silent = sum(1 for c in anchor.changes if c.before is None)
        stated = f" ({silent} stated none)" if silent else ""
        normalised.append(
            f"item level {target.ilevel} on {len(anchor.changes)} slot(s){written}{stated}"
        )
    if anchor.unchanged:
        normalised.append(f"{len(anchor.unchanged)} slot(s) already at {target.ilevel}")
    if anchor.emptied:
        normalised.append(
            f"{len(anchor.emptied)} slot(s) the kit does not fill, written empty so the "
            f"base actor's own item cannot survive inside it"
        )
    normalised.append(f"item level target: {target.ilevel_evidence}")
    if target.set_option:
        normalised.append(f"tier set state: {target.set_evidence}")
    if target.zeroed:
        normalised.append(f"{len(target.zeroed)} other set token(s) written to zero")

    preserved: list[str] = []
    for label, slots in (
        ("gem_id", anchor.gemmed),
        ("enchant_id", anchor.enchanted),
        ("crafted_stats", anchor.crafted),
    ):
        if slots:
            preserved.append(f"{label} ({len(slots)} slot(s))")

    if not target.set_option:
        tier_set = f"none -- {target.tier} ships no set bonus"
    elif target.set_pieces:
        tier_set = f"{target.set_pieces}-piece {target.set_name or target.set_option}"
    else:
        tier_set = f"no {target.set_name or target.set_option}"

    return {
        "label": f"Item level {target.ilevel} ({target.band_label} band), {tier_set}",
        "profile": profile,
        "itemLevel": target.ilevel,
        "normalised": normalised,
        "preserved": preserved,
        "tierSet": tier_set,
    }
