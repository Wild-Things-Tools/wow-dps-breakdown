"""Harvest the builds real players actually killed a boss with, from Warcraft Logs.

## Why this exists

For the damage specs simc ships no profile for, the missing artefact is a
*character*: gear plus talents that somebody has signed off. ``unvalidated.py``
records the shape of that gap -- the action lists exist for every spec, the
profiles are written and switched off, and this project deliberately does not
author its own, because a number invented here would be our opinion about how a
spec is geared and played published on a site whose whole claim is that its
numbers are derived from simc and byte-reproducible.

Top players already run good characters, and Warcraft Logs exposes them. That
turns an intractable search -- "which of the billions of talent arrangements is
good" -- into a bounded one: rank what real players actually ran, then look in
its neighbourhood.

## What Warcraft Logs offers, and where that was read

Three fields carry the whole feature. All three were read out of the published v2
schema as mirrored in three independent third-party clients on 2026-08-23
(``ToppleTheNun/mchammer``, ``svenliebig/wcl-blame``, ``math280h/go-wcl``), which
agree word for word on the descriptions quoted here:

* ``ReportFight.talentImportCode(actorID: Int!): String`` -- *"The import/export
  code for a Retail Dragonflight talent build."* That is exactly the string a simc
  profile's ``talents=`` line consumes and ``talenttree.decode_loadout`` reads.
  It is documented to be null for a classic or pre-Dragonflight fight, and for a
  non-player actor.
* ``Report.playerDetails(fightIDs: [Int], ...): JSON`` -- *"A table of information
  for the players of a report, including their specs, talents, gear, etc. This
  data is not considered frozen, and it can change without notice."* Untyped, so
  it is read defensively and what could not be read is reported.
* ``Encounter.characterRankings`` -- already used by ``fightprobe`` to get from a
  boss to a list of ``(report code, fight id)``. Reused unchanged.

**None of that is verified against the live service.** There are no Warcraft Logs
credentials in the environment this was written in, so no query in this module has
ever been sent. That is the same position ``fightprobe`` and ``lootsources`` were
in before their first CI run, and it is handled the same way: the extraction is
pinned against hand-written payloads, the first live run is a schema check as much
as a harvest, and ``--probe`` exists to make that first run cheap and legible
rather than a full sweep that fails on a field name.

## The cost shape, which is why the queries look like this

Ranking queries are close to free and report-level queries are the expensive kind
-- that is measured in this repository already (the first ``--order public`` run
moved the counter from nothing to 2880 of 3600 points). So the harvest is built to
need as few report-level requests as possible, and the arithmetic is:

    queries = 2 (bracketing rate-limit readings)
            + rankings pages gathered
            + 2 per sampled kill

**Two per kill, not two per player.** ``playerDetails`` returns every player in the
pull, so one request harvests a whole raid's gear and specs at once; and every
actor's talent code is fetched in a *single* request by aliasing
``talentImportCode`` once per actor inside one ``fights`` selection. A sampled kill
therefore costs the same whether one spec is wanted from it or twenty, which is
what makes harvesting for the nine missing specs affordable at all.

**What that costs in points is unknown and this module does not guess it.** The
query *count* is arithmetic and is reported; points per query are not published by
Warcraft Logs and the repository's own rule is to read ``rateLimitData`` and find
out. ``cmd_harvest_builds`` brackets the pass with two standalone readings, and a
counter that did not move is reported as UNMEASURED rather than as zero -- the
exact mistake that shipped once here already.

## What a harvested build is evidence of, and what it is not

**That a real player killed this boss with it.** Not that it is optimal, not that
it is what that player would run next week, and not that it beats what simc would
find. Four things travel with every row for that reason:

* the **encounter** and the **difficulty**, never mixed -- a Heroic build and a
  Mythic build are answers to different questions, so a run takes one difficulty
  and refuses a fight that states another;
* the **date range** of the sampled kills, because a build is a snapshot of a
  tuning pass;
* the **report code and fight id**, so any row can be opened and checked;
* how many of the sampled kills ran it, which is the only thing here that
  distinguishes a consensus build from one person's experiment.

And one bound no setting reaches: ``characterRankings`` contains **ranked parses
only**. A kill logged privately, or one Warcraft Logs declined to rank, is invisible
at any depth. That is their rule, not a limit of this code, and it is published in
the document rather than left to be discovered.

## Character names are deliberately not collected

The artefact is a build, not a person. A report code, a fight id and an actor id
identify the observation completely and let anybody re-open it; the character and
server names add nothing to that and would make this a small database about named
individuals. They are dropped at extraction rather than filtered at publication, so
they are never written to disk in the first place.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import equipment, profiles, specindex, talenttree
from .talenttree import Loadout, TalentDecodeError, Trait

log = logging.getLogger(__name__)

#: Rejection reasons. Every one of them is *published*: a hash that will not decode
#: is the single most interesting thing a harvest can turn up, because it means
#: either the format moved or the extraction is wrong, and a silent drop would
#: present a thin harvest as a complete one.
REASON_OK = "ok"
REASON_NO_CODE = "no_talent_code"
REASON_DECODE = "decode_error"
REASON_SPEC_MISMATCH = "spec_mismatch"
REASON_SPEC_RULE = "spec_rule_violation"
REASON_UNKNOWN_CLASS = "unknown_class"
REASON_UNKNOWN_SPEC = "unknown_spec"

# There is deliberately no rejection reason for a wrong difficulty. Mixing two is a
# refusal for the whole run (`DifficultyMixed`), not a per-observation verdict, and a
# constant sitting in this list would say the opposite -- that such a row is published
# as one rejection among others.


class DifficultyMixed(ValueError):
    """Refusal: a fight states a difficulty this harvest did not ask for.

    Not a filter and not a warning. A Heroic kill and a Mythic kill are different
    questions, and a document that quietly contained both would answer neither --
    the builds would be pooled, the counts would be meaningless, and nothing in the
    file would say so.
    """


@dataclass(frozen=True)
class GearPiece:
    """One equipped item, as Warcraft Logs reported it.

    ``slot`` is **derived** from simc's own item table rather than from the position
    the item held in the payload array -- see ``equipment.inventory_types`` for why.
    It is ``None`` for an item simc has never heard of, which is reported rather
    than guessed at.
    """

    index: int
    item_id: int
    item_level: int | None = None
    slot: str | None = None
    gem_ids: tuple[int, ...] = ()
    enchant_id: int | None = None
    bonus_ids: tuple[int, ...] = ()
    set_id: int | None = None

    def simc_line(self) -> str | None:
        """The item as a simc profile gear line, or ``None`` without a slot.

        Carries item level, gems and the enchant, and **not** bonus ids. That split
        is measured rather than stylistic: this project established that an explicit
        ``ilevel=`` overrides what bonus ids would otherwise set and that they are
        inert for rings as well as trinkets (identical DPS to the last digit), while
        the gem is worth +0.44% and the enchant +1.09% against +0.09% for a ten
        item level step. Dropping the gem and the enchant would be the order of
        magnitude error; dropping the bonus ids costs nothing measurable.

        ``bonus_ids`` is still carried on the dataclass, for the same reason
        ``GearItem`` carries it: it is free to keep and it is evidence.
        """
        if not self.slot:
            return None
        parts = [f"{self.slot}=,id={self.item_id}"]
        if self.item_level:
            parts.append(f"ilevel={self.item_level}")
        if self.gem_ids:
            parts.append("gem_id=" + "/".join(str(gem) for gem in self.gem_ids))
        if self.enchant_id:
            parts.append(f"enchant_id={self.enchant_id}")
        return ",".join(parts)

    def to_json(self) -> dict:
        payload: dict = {"index": self.index, "itemId": self.item_id, "slot": self.slot}
        if self.item_level is not None:
            payload["itemLevel"] = self.item_level
        if self.gem_ids:
            payload["gemIds"] = list(self.gem_ids)
        if self.enchant_id:
            payload["enchantId"] = self.enchant_id
        if self.bonus_ids:
            payload["bonusIds"] = list(self.bonus_ids)
        if self.set_id:
            payload["setId"] = self.set_id
        return payload


@dataclass(frozen=True)
class Observation:
    """One damage player in one sampled kill. No character or server name -- see the
    module docstring."""

    report: str
    fight_id: int
    actor_id: int
    encounter_id: int
    encounter_name: str
    difficulty: int
    killed_at_ms: float
    wow_class: str
    spec: str
    item_level: float | None = None
    talent_hash: str | None = None
    gear: tuple[GearPiece, ...] = ()
    #: Gear entries ``gear_from_row`` could not read. Carried on the observation
    #: rather than returned beside it, because the returned version was dropped at
    #: the only production call site: ``pieces, _skipped = gear_from_row(...)``.
    #: If Warcraft Logs renames ``combatantInfo.gear[].id`` every entry is skipped,
    #: every build publishes ``simcGear: []`` and ``itemsWithoutASlot: []``, and the
    #: file reads as "these builds wear nothing worth writing down" rather than "the
    #: gear payload moved". Only the count tells those two apart.
    gear_entries_skipped: int = 0
    #: The row carried no gear array at all -- as opposed to one that was empty or
    #: one whose entries could not be read. That is what an omitted
    #: ``includeCombatantInfo: true`` produces, and the first live probe produced it
    #: for fourteen players out of fourteen while every other number in the run
    #: looked healthy. ``gear_entries_skipped`` cannot see it: there were no entries
    #: to skip.
    combatant_info_missing: bool = False

    @property
    def spec_key(self) -> str:
        """``death_knight_frost``, the id shape the rest of the dataset joins on.

        This is simc's spelling, not Warcraft Logs'. See ``fold_name``: the raw
        payload would yield ``deathknight_frost`` and ``hunter_beastmastery``, which
        join to nothing.
        """
        return f"{_slug(self.wow_class)}_{_slug(self.spec)}"

    def source_json(self) -> dict:
        """Everything needed to re-open this observation, and to say what it is of.

        ``encounterName`` and ``difficulty`` were held on this dataclass and not
        emitted, which left a consumer of a *candidate* having to join back to
        ``fights.json`` to find out which boss and which difficulty it came from --
        a file a tier may not have at all. The document's own
        ``source.encounters[]`` carries ``{id, name}`` and is the better fallback of
        the two, but neither is a substitute for the row saying so itself.

        ``difficulty`` is the run's, always: a run takes one and ``DifficultyMixed``
        refuses to pool two, so this is a denormalisation and never a second answer.
        """
        return {
            "report": self.report,
            "fightID": self.fight_id,
            "actorID": self.actor_id,
            "encounterID": self.encounter_id,
            "encounterName": self.encounter_name,
            "difficulty": self.difficulty,
            "killedAt": _iso(self.killed_at_ms),
        }


@dataclass(frozen=True)
class Verdict:
    """What became of one observation's talent hash."""

    reason: str
    detail: str = ""
    loadout: Loadout | None = None

    @property
    def ok(self) -> bool:
        return self.reason == REASON_OK


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text.strip().lower()).strip("_")


def fold_name(text: str) -> str:
    """``Death Knight``, ``DeathKnight`` and ``death_knight`` all fold to one key.

    **Warcraft Logs spells a class and a spec without spaces and simc spells them
    with**, and this join is the whole reason two of the nine specs this command
    exists for could not be harvested at all. WCL sends ``DeathKnight``,
    ``DemonHunter`` and ``BeastMastery``; ``talenttree.CLASS_IDS`` is keyed on
    ``Death Knight`` and simc names the spec ``Beast Mastery``. Looked up directly,
    every Death Knight and Demon Hunter observation came back
    ``REASON_UNKNOWN_CLASS`` and every Beast Mastery one ``REASON_UNKNOWN_SPEC`` --
    Havoc and Devourer being two of the specs the harvest is for.

    Neither spelling is wrong and neither side is going to change, so the join runs
    through a fold rather than through either of them. Nothing is *stored* folded:
    an observation carries simc's spelling, because that is what
    ``Observation.spec_key`` has to produce for the file to join the rest of the
    dataset (``death_knight_frost``, not ``deathknight_frost``).
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def canonical_class(name: str) -> str | None:
    """Warcraft Logs' ``DeathKnight`` -> simc's ``Death Knight``. ``None`` if neither.

    ``profiles.CLASS_TOKENS`` is already keyed on exactly this fold -- it is how a
    simc profile's ``deathknight=`` line is read -- so the mapping exists and only
    had to be used.
    """
    entry = profiles.CLASS_TOKENS.get(fold_name(name))
    return entry[0] if entry else None


def _iso(ms: float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------
# Reading the payloads
# --------------------------------------------------------------------------------

#: The role buckets ``playerDetails`` is documented to split its table into. Only
#: ``dps`` is harvested -- this site publishes damage specs -- but all three are
#: *looked for*, so "the payload had no dps bucket" reads differently from "the
#: payload was not the shape we expected at all".
ROLE_BUCKETS = ("dps", "healers", "tanks")


def player_detail_rows(payload: object, bucket: str = "dps") -> tuple[list[dict], list[str]]:
    """``(rows, buckets seen)`` out of a ``playerDetails`` payload.

    The field is an untyped ``JSON`` scalar whose shape Warcraft Logs explicitly
    declines to freeze, so every layer is optional here: the value may arrive as a
    JSON string, wrapped in ``{"data": ...}``, or as the bare table. Returning the
    buckets that *were* present is what lets the caller say which of those it got
    instead of reporting an empty harvest with no reason attached.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return [], []
    if not isinstance(payload, dict):
        return [], []

    # `playerDetails` is documented to sit under a `data` key in the payload it
    # returns. Accepting both shapes costs one line and turns a wrapper change from
    # an empty harvest into a non-event.
    table = payload
    for key in ("data", "playerDetails"):
        inner = table.get(key)
        if isinstance(inner, dict):
            table = inner

    seen = [name for name in ROLE_BUCKETS if isinstance(table.get(name), list)]
    rows = table.get(bucket)
    return ([row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []), seen


def spec_of_row(row: dict) -> str | None:
    """The specialisation a ``playerDetails`` row states, from either field carrying it.

    ``specs`` is the documented one and is a list because a player can swap
    mid-fight; ``icon`` carries the same thing as ``Mage-Arcane`` and is the
    fallback. A row that swapped spec during the pull is refused rather than
    guessed at -- its talent code is one build and its damage is two.
    """
    specs = row.get("specs")
    if isinstance(specs, list):
        names = []
        for entry in specs:
            if isinstance(entry, dict) and isinstance(entry.get("spec"), str):
                names.append(entry["spec"])
            elif isinstance(entry, str):
                names.append(entry)
        unique = sorted(set(names))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return None
    icon = row.get("icon")
    if isinstance(icon, str) and "-" in icon:
        return icon.split("-", 1)[1]
    return None


def _ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    out: list[int] = []
    for entry in value:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            out.append(entry)
        elif isinstance(entry, dict):
            for key in ("id", "itemID", "itemId"):
                if isinstance(entry.get(key), int):
                    out.append(entry[key])
                    break
    return tuple(out)


def gear_array(row: dict) -> list | None:
    """The gear entries of one ``playerDetails`` row, or ``None`` when it carries none.

    ``None`` and ``[]`` are different answers and the whole of blocker 1 was the two
    being indistinguishable from outside: a row whose combatant info was never
    requested has no gear array at all, and a row that has one and is empty is a
    player wearing nothing. Both used to leave ``gear_from_row`` returning
    ``([], 0)``.
    """
    info = row.get("combatantInfo")
    gear = info.get("gear") if isinstance(info, dict) else row.get("gear")
    return gear if isinstance(gear, list) else None


def describe_combatant_info(row: dict) -> str:
    """What a row's ``combatantInfo`` *is*, in words rather than as a type name.

    The probe printed ``combatantInfo keys: list``, which is the type name standing
    where a reader expects keys -- so an argument that was never sent read exactly
    like a payload shape this code had failed to parse, and the first live run was
    diagnosed as the wrong problem. Measured on that run (CI 32660348582,
    2026-08-23): fourteen real players, every one of them carrying ``[]``.

    The empty list is named for what it means, because Warcraft Logs has a documented
    reason to send it and this code has a documented way to stop it. A **non-empty**
    list is deliberately described and not parsed: nobody has seen one, and a branch
    written for an unobserved shape is a guess with the authority of code.
    """
    info = row.get("combatantInfo")
    if isinstance(info, dict):
        return f"dict, keys {sorted(info)}"
    if isinstance(info, list):
        if not info:
            return (
                "empty list (no combatant info in the response -- playerDetails was "
                "asked without includeCombatantInfo: true)"
            )
        return (
            f"list of {len(info)} entry/entries -- a shape this code has never "
            f"observed and does not parse; first entry is "
            f"{sorted(info[0]) if isinstance(info[0], dict) else type(info[0]).__name__}"
        )
    if info is None:
        return "absent from the row"
    return type(info).__name__


def gear_from_row(row: dict) -> tuple[list[GearPiece], int]:
    """``(pieces, entries skipped)`` from a ``playerDetails`` row's combatant info.

    Skipped entries are counted rather than ignored: an empty slot is an ordinary
    zero-id entry and a row of them is a payload this code did not understand, and
    only the count tells those two apart from outside.

    A row with **no** gear array is a third state again, and ``combatant_info_missing``
    on the observation is what publishes it -- see ``gear_array``.
    """
    gear = gear_array(row)
    if gear is None:
        return [], 0

    pieces: list[GearPiece] = []
    skipped = 0
    for index, entry in enumerate(gear):
        if not isinstance(entry, dict):
            skipped += 1
            continue
        item_id = entry.get("id")
        if not isinstance(item_id, int) or item_id <= 0:
            skipped += 1
            continue
        level = entry.get("itemLevel")
        enchant = entry.get("permanentEnchant")
        set_id = entry.get("setID")
        pieces.append(
            GearPiece(
                index=index,
                item_id=item_id,
                item_level=level if isinstance(level, int) else None,
                gem_ids=_ints(entry.get("gems")),
                enchant_id=enchant if isinstance(enchant, int) and enchant else None,
                bonus_ids=_ints(entry.get("bonusIDs")),
                set_id=set_id if isinstance(set_id, int) and set_id else None,
            )
        )
    return pieces, skipped


def resolve_slots(
    pieces: list[GearPiece], inventory: dict[int, int]
) -> tuple[tuple[GearPiece, ...], tuple[int, ...]]:
    """Name each item's slot from simc's item table. Returns ``(pieces, unplaced ids)``.

    The slot is a property of the item and simc ships it, so nothing here asserts
    what position 12 of somebody else's array means. What the item table genuinely
    cannot answer is *which* of two interchangeable items goes in the first socket
    -- which ring is ``finger1``, which of a dual-wielder's two one-handers is the
    main hand -- because that is not a property of either item. Those are settled by
    order of appearance, which is the only orderable fact available.

    So this is a socket allocation rather than a lookup: each item names the sockets
    its inventory type could occupy and takes the first one still free.
    **Constrained items are placed first**, which is what stops a flexible one-hander
    that happens to be listed before a shield from taking the off hand the shield
    cannot do without. The sort is stable, so two items with the same freedom keep
    their order and a re-run places them identically.

    An item simc has never heard of, or one with no socket left, keeps its data and
    gets no slot: it cannot be put in a profile, so claiming one would be a claim
    with nothing behind it -- the same refusal ``gearpool`` makes for an item it
    cannot simulate.
    """
    candidates = [equipment.slot_candidates(inventory.get(piece.item_id)) for piece in pieces]
    order = sorted(range(len(pieces)), key=lambda index: len(candidates[index]) or 99)

    taken: set[str] = set()
    slots: dict[int, str] = {}
    for index in order:
        slot = next((name for name in candidates[index] if name not in taken), None)
        if slot is None:
            continue
        taken.add(slot)
        slots[index] = slot

    resolved: list[GearPiece] = []
    unresolved: list[int] = []
    for index, piece in enumerate(pieces):
        slot = slots.get(index)
        if slot is None:
            unresolved.append(piece.item_id)
            resolved.append(piece)
            continue
        resolved.append(
            GearPiece(
                index=piece.index,
                item_id=piece.item_id,
                item_level=piece.item_level,
                slot=slot,
                gem_ids=piece.gem_ids,
                enchant_id=piece.enchant_id,
                bonus_ids=piece.bonus_ids,
                set_id=piece.set_id,
            )
        )
    return tuple(resolved), tuple(unresolved)


# --------------------------------------------------------------------------------
# Validating what came back
# --------------------------------------------------------------------------------


# --------------------------------------------------------------------------------
# Which encounter id a boss's kills are actually under
# --------------------------------------------------------------------------------

#: A Warcraft Logs PTR encounter id is its live id **with a 5 written in front**.
#: CLAUDE.md establishes it from two independent zone pairs -- zone 48 carries 53176
#: where zone 46 carries 3176, and zone 54 carries 53470 where zone 53 carries 3470
#: -- and treats it as a *feature* rather than a nuisance: "a measurement taken
#: against 53470 cannot be mistaken for one taken against 3470", so the PTR-ness
#: rides in the id and needs no separate flag.
#:
#: That is why ``fight_profiles.json`` is not rewritten to fix this. MID2 was seeded
#: from zone 54 and its eight encounters are PTR ids on purpose; renumbering them
#: would relabel every PTR fight measurement filed under them as a live one. The
#: harvest's *addressing* is what has to move, and only for the harvest.
_PTR_ID_PREFIX = "5"


def live_twin_id(encounter_id: int) -> int | None:
    """The live encounter id a PTR id is the twin of, or ``None``.

    Purely the shape of the number -- it says nothing about whether either id
    exists, and nothing here acts on it without checking the name. A remainder with
    a leading zero is refused: no live id is written ``0123``, so ``50123`` is not a
    PTR id of 123 and reading it as one would silently address a different boss.
    """
    if encounter_id <= 0:
        return None
    digits = str(encounter_id)
    if not digits.startswith(_PTR_ID_PREFIX) or len(digits) < 2:
        return None
    rest = digits[1:]
    if rest.startswith("0"):
        return None
    return int(rest)


def names_agree(left: str | None, right: str | None) -> bool:
    """Do two encounter names name the same boss?

    Compared on a strip-and-casefold rather than byte-identically, because the two
    ids are two rows of the same table and a difference in trailing space or case is
    not a difference in boss. Nothing more forgiving than that: the point of the
    check is that a wrong twin has to be caught, and every loosening of it is a way
    for one to pass. A missing name never agrees with anything.
    """
    if not left or not right:
        return False
    return left.strip().casefold() == right.strip().casefold()


@dataclass(frozen=True)
class IdChoice:
    """Which encounter id a harvest actually read, and why.

    Published per encounter rather than logged, because the substitution changes
    *which fight* the builds came from. A file that quietly harvested another id
    than the one it is filed under would be a full set of real builds under the
    wrong boss's name -- which is the one failure mode worse than harvesting none.
    """

    requested: int
    used: int | None
    reason: str
    substituted: bool = False

    @property
    def refused(self) -> bool:
        return self.used is None

    def to_json(self) -> dict:
        return {
            "requested": self.requested,
            "used": self.used,
            "substituted": self.substituted,
            "reason": self.reason,
        }


def choose_encounter_id(
    requested: int,
    requested_name: str | None,
    has_ranked_parses: bool,
    lookup_name,
) -> IdChoice:
    """Decide which encounter id to harvest, verifying any substitution by name.

    Measured in CI on 2026-08-23: encounter **53420 returns 0 kills and 0
    characterRankings**, while **3470 returns 1 kill, 100 characterRankings, 14 dps
    rows and 14 talent codes**. MID2's fight profiles carry the 53xxx ids because
    the tier was seeded from the PTR zone, so a harvest addressed at them reads
    nothing at all and says nothing about why.

    The rule, and each step of it is a refusal rather than a fallback:

    * an id with **ranked parses** is harvested as filed. No lookup is sent, so the
      ordinary case costs nothing;
    * an id with none, whose number is not ``<5><live id>``, has no twin to try;
    * a twin the schema does not name does not exist, and an id that names a
      *different boss* is a different boss. Both refuse, with the names printed.

    ``lookup_name`` is a callable rather than a fetched value so that the query is
    sent only on the branch that needs it -- and so this whole decision is testable
    with a dict.
    """
    if has_ranked_parses:
        return IdChoice(
            requested,
            requested,
            f"harvested as filed: encounter {requested} has ranked parses at this difficulty",
        )

    twin = live_twin_id(requested)
    if twin is None:
        return IdChoice(
            requested,
            None,
            f"refused: encounter {requested} has no ranked parses at this difficulty, "
            f"and its id is not a PTR id (a PTR id is a live id with a 5 in front), "
            f"so there is no live twin to try",
        )

    twin_name = lookup_name(twin)
    if twin_name is None:
        return IdChoice(
            requested,
            None,
            f"refused: encounter {requested} has no ranked parses and the live twin "
            f"{twin} is not an encounter Warcraft Logs knows",
        )
    if not names_agree(requested_name, twin_name):
        return IdChoice(
            requested,
            None,
            f"refused: encounter {requested} has no ranked parses, and its live twin "
            f"{twin} is a different boss -- {requested_name!r} against {twin_name!r}. "
            f"Harvesting it would file real builds under the wrong fight",
        )
    return IdChoice(
        requested,
        twin,
        f"read as {twin}: encounter {requested} is a PTR id with no ranked parses, "
        f"and its live twin {twin} carries the same name {twin_name!r}",
        substituted=True,
    )


@dataclass
class TalentTables:
    """simc's trait data, arranged the way a harvest needs to ask it.

    Built once per run. ``nodes`` is keyed by class id because ``decode_loadout``
    reads one class's node list, and ``spec_ids`` is what turns "the row says Arcane
    Mage" into the number the hash claims to be.
    """

    nodes: dict[int, dict[int, list[Trait]]]
    spec_ids: dict[tuple[str, str], int]
    sub_trees: dict[int, str]
    #: ``(folded class, folded spec) -> simc's own spelling of the pair``. Built from
    #: ``spec_ids``, so it cannot drift from it. See ``fold_name``.
    _canonical: dict[tuple[str, str], tuple[str, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._canonical = {
            (fold_name(wow_class), fold_name(spec)): (wow_class, spec)
            for wow_class, spec in self.spec_ids
        }

    def canonical_names(self, wow_class: str, spec: str) -> tuple[str, str]:
        """simc's spelling of a pair Warcraft Logs spelled its own way.

        Falls back to whatever the log said for the half that could not be resolved,
        so an unrecognised pair reaches ``validate`` carrying the name the payload
        actually contained -- which is the only useful thing a rejection can print.
        A class that resolves and a spec that does not comes out
        ``("Death Knight", "Frostfire")``, which reports ``unknown_spec`` rather than
        the misleading ``unknown_class``.
        """
        found = self._canonical.get((fold_name(wow_class), fold_name(spec)))
        if found:
            return found
        return canonical_class(wow_class) or wow_class, spec

    @classmethod
    def load(cls, simc_dir: Path, ptr: bool = False) -> TalentTables:
        traits = talenttree.parse_trait_data(simc_dir, ptr=ptr)
        nodes = {
            class_id: talenttree.nodes_for_class(traits, class_id)
            for class_id in talenttree.CLASS_IDS.values()
        }
        return cls(
            nodes=nodes,
            spec_ids=spec_ids(simc_dir, ptr=ptr),
            sub_trees={
                sub_tree_id: entry.name
                for sub_tree_id, entry in talenttree.parse_sub_tree_names(simc_dir, ptr=ptr).items()
            },
        )


def spec_ids(simc_dir: Path, ptr: bool = False) -> dict[tuple[str, str], int]:
    """``(class name, spec name) -> canonical spec id``, from simc's own two tables.

    Warcraft Logs names a spec in English (``"Arcane"``) and the loadout hash states
    a numeric spec id, so something has to join them. Both halves are already parsed
    by ``specindex`` for the picker: ``sc_spec_list`` gives each class its specs in
    the game's order, ``sc_specialization_data`` gives each enum name its id. This is
    the join, and it is derived for the same reason the picker's is -- Midnight adds
    a Demon Hunter spec, and a hand-written table would go stale in the patch where
    it matters most.
    """
    enum = specindex.parse_spec_enum(simc_dir, ptr=ptr)
    out: dict[tuple[str, str], int] = {}
    for wow_class, enum_names in zip(
        specindex._CLASS_ORDER, specindex.parse_spec_list(simc_dir, ptr=ptr), strict=True
    ):
        for enum_name in enum_names:
            spec_id = enum.get(enum_name)
            if spec_id:
                out[(wow_class, specindex._pretty_spec(enum_name, wow_class))] = spec_id
    return out


def validate(observation: Observation, tables: TalentTables) -> Verdict:
    """Decode one observation's hash and hold it to simc's own rules.

    Four ways this says no, and each one is a different finding:

    * **no talent code.** The field is documented to be null for a non-player actor
      and for a pre-Dragonflight fight. Getting one here would mean the actor
      selection is wrong.
    * **it will not decode.** Either the loadout format moved or the bit reader is
      desynchronised. The single most valuable thing this command can report.
    * **the spec id disagrees** with the spec the player details row states. Two
      sources describing one character; a disagreement means one of the two reads is
      wrong, and pooling the build under either name would bury it.
    * **simc would refuse it** -- a non-hero node whose ``id_spec`` excludes the
      player's spec, in simc's own wording. Harvesting a build simc will not load
      wastes the whole downstream sweep, and this decides it offline.

    A build that passes all four is *loadable*, which is the only claim made for it.
    Nothing here says it is good.
    """
    # Resolved here as well as at extraction, so a directly built observation -- a
    # test, the shipped-hash corpus, anything downstream -- cannot lose the join by
    # forgetting to canonicalise. `canonical_names` is idempotent.
    wow_class, spec = tables.canonical_names(observation.wow_class, observation.spec)

    class_id = talenttree.CLASS_IDS.get(wow_class)
    if class_id is None:
        return Verdict(REASON_UNKNOWN_CLASS, f"no simc class id for {observation.wow_class!r}")

    expected = tables.spec_ids.get((wow_class, spec))
    if expected is None:
        return Verdict(
            REASON_UNKNOWN_SPEC,
            f"simc names no spec {observation.spec!r} for {wow_class}",
        )

    if not observation.talent_hash:
        return Verdict(REASON_NO_CODE, "talentImportCode returned null for this actor")

    try:
        loadout = talenttree.decode_loadout(observation.talent_hash, tables.nodes[class_id])
    except (TalentDecodeError, ValueError, KeyError, IndexError) as exc:
        return Verdict(REASON_DECODE, str(exc))

    if loadout.spec_id != expected:
        return Verdict(
            REASON_SPEC_MISMATCH,
            f"hash states spec {loadout.spec_id}, the log says {observation.spec} ({expected})",
            loadout,
        )

    violation = talenttree.spec_rule_violation(loadout, tables.nodes[class_id])
    if violation:
        return Verdict(REASON_SPEC_RULE, violation, loadout)

    return Verdict(REASON_OK, "", loadout)


# --------------------------------------------------------------------------------
# Deduplicating
# --------------------------------------------------------------------------------


def loadout_key(loadout: Loadout) -> str:
    """A stable id for *what a build takes*, independent of the string that carried it.

    Two players running the same talents can hand back different hash strings -- the
    128 bits of tree hash simc writes as zeros are not the only thing that can
    differ, and re-exports move. So the key is the decoded content: spec id plus
    every ``(node, entry, rank)`` in ascending order. Keying on the hash string
    would report thirty copies of one build as thirty builds, which is precisely the
    number this command exists to produce.
    """
    body = json.dumps(
        {
            "spec": loadout.spec_id,
            "picks": sorted((s.node_id, s.entry_id, s.rank) for s in loadout.selections),
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass
class HarvestedBuild:
    """One distinct decoded loadout, and every kill it was seen in."""

    key: str
    loadout: Loadout
    talent_hash: str
    observations: list[Observation] = field(default_factory=list)

    @property
    def seen_in(self) -> int:
        return len(self.observations)

    def hero_tree(self, tables: TalentTables) -> tuple[int | None, str | None]:
        sub_tree = self.loadout.sub_tree
        return sub_tree, tables.sub_trees.get(sub_tree) if sub_tree else None

    def to_json(self, tables: TalentTables, max_sources: int) -> dict:
        sub_tree, tree_name = self.hero_tree(tables)
        # Newest kill first, so a capped list is the most recent evidence rather
        # than whichever report happened to be read first.
        ordered = sorted(self.observations, key=lambda o: -o.killed_at_ms)
        return {
            "buildKey": self.key,
            "talentHash": self.talent_hash,
            "specId": self.loadout.spec_id,
            "heroTree": {"subTree": sub_tree, "name": tree_name},
            "points": {
                "class": self.loadout.points(talenttree.TREE_CLASS),
                "spec": self.loadout.points(talenttree.TREE_SPEC),
                "hero": self.loadout.points(talenttree.TREE_HERO),
            },
            "selected": [
                {
                    "nodeId": s.node_id,
                    "entryId": s.entry_id,
                    "name": s.name,
                    "spellId": s.spell_id,
                    "rank": s.rank,
                    "tree": s.tree_index,
                }
                for s in sorted(self.loadout.selections, key=lambda s: (s.tree_index, s.node_id))
            ],
            "seenInKills": self.seen_in,
            "sources": [
                {
                    **observation.source_json(),
                    "itemLevel": observation.item_level,
                    "gear": [piece.to_json() for piece in observation.gear],
                    "simcGear": [
                        line for line in (piece.simc_line() for piece in observation.gear) if line
                    ],
                }
                for observation in ordered[:max_sources]
            ],
            "sourcesTruncated": max(0, self.seen_in - max_sources),
        }


def group_builds(
    observations: list[Observation], tables: TalentTables
) -> tuple[dict[str, list[HarvestedBuild]], dict[str, list[tuple[Observation, Verdict]]]]:
    """``(builds per spec, rejections per spec)``.

    Both halves are returned because both are published. A run that harvested
    forty kills and could use six of them has found something, and a document
    carrying only the six would say the opposite.
    """
    builds: dict[str, dict[str, HarvestedBuild]] = {}
    rejected: dict[str, list[tuple[Observation, Verdict]]] = {}

    for observation in observations:
        verdict = validate(observation, tables)
        spec_key = observation.spec_key
        if not verdict.ok or verdict.loadout is None:
            rejected.setdefault(spec_key, []).append((observation, verdict))
            continue
        key = loadout_key(verdict.loadout)
        bucket = builds.setdefault(spec_key, {})
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = HarvestedBuild(
                key=key,
                loadout=verdict.loadout,
                talent_hash=observation.talent_hash or "",
                observations=[observation],
            )
        else:
            existing.observations.append(observation)

    ordered = {
        spec: sorted(bucket.values(), key=lambda b: (-b.seen_in, b.key))
        for spec, bucket in builds.items()
    }
    return ordered, rejected


# --------------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------------

SCHEMA_VERSION = 1

#: What ``characterRankings`` cannot reach, stated in the file rather than left to
#: be rediscovered. Measured in this repository already: the ranking list is sorted
#: by damage and holds ranked parses only.
RANKED_ONLY_NOTE = (
    "Sampled from Warcraft Logs character rankings, which contain ranked parses "
    "only. A kill logged privately, or one Warcraft Logs declined to rank, is not "
    "in that list at any depth and no setting here reaches it."
)

EVIDENCE_NOTE = (
    "A harvested build is evidence that a real player killed this encounter at this "
    "difficulty with it. It is not evidence that the build is optimal, nor that it "
    "would still be chosen after a tuning pass."
)


def date_span(observations: list[Observation]) -> dict | None:
    """First and last kill in the sample, and how many days apart.

    A build is a snapshot of a tuning pass, so the window it was taken from is part
    of the claim. The fight probe publishes the same three fields for the same
    reason -- widening the sample is the lever, and publishing the dates is what
    makes the lever's effect checkable.
    """
    stamps = sorted(o.killed_at_ms for o in observations if o.killed_at_ms)
    if not stamps:
        return None
    return {
        "first": _iso(stamps[0]),
        "last": _iso(stamps[-1]),
        "spanDays": round((stamps[-1] - stamps[0]) / 86_400_000, 2),
    }


def build_document(
    tier: str,
    difficulty: int,
    observations: list[Observation],
    tables: TalentTables,
    encounters: list[dict],
    ledger: dict | None = None,
    max_sources: int = 3,
    unresolved_items: tuple[int, ...] = (),
    query_plan: dict | None = None,
) -> dict:
    """The published shape. Every count in it comes from the rows above it."""
    builds, rejected = group_builds(observations, tables)

    spec_rows = []
    for spec_key in sorted(set(builds) | set(rejected)):
        spec_builds = builds.get(spec_key, [])
        spec_rejects = rejected.get(spec_key, [])
        sample = spec_builds[0].observations[0] if spec_builds else spec_rejects[0][0]
        spec_observations = [o for b in spec_builds for o in b.observations]
        spec_rows.append(
            {
                "specId": spec_key,
                "class": sample.wow_class,
                "spec": sample.spec,
                "killsHarvested": len(spec_observations) + len(spec_rejects),
                "killsUsable": len(spec_observations),
                # The headline of this whole command: how many *different* builds
                # the sampled players actually ran. One means a settled spec; ten
                # over ten kills means there is no consensus to harvest.
                "distinctBuilds": len(spec_builds),
                "killedBetween": date_span(spec_observations),
                "builds": [b.to_json(tables, max_sources) for b in spec_builds],
                "rejected": [
                    {
                        "reason": verdict.reason,
                        "detail": verdict.detail,
                        **observation.source_json(),
                    }
                    for observation, verdict in spec_rejects
                ],
            }
        )

    document: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "tier": tier,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": {
            "difficulty": difficulty,
            "encounters": encounters,
            "killsSampled": len({(o.report, o.fight_id) for o in observations}),
            "playersRead": len(observations),
            "killedBetween": date_span(observations),
            "rankedParsesOnly": True,
            "note": RANKED_ONLY_NOTE,
            "evidence": EVIDENCE_NOTE,
        },
        "specs": spec_rows,
        "coverage": {
            "specsWithABuild": sum(1 for row in spec_rows if row["distinctBuilds"]),
            "specsSeen": len(spec_rows),
            "buildsTotal": sum(row["distinctBuilds"] for row in spec_rows),
            "rejectedTotal": sum(len(row["rejected"]) for row in spec_rows),
            # Items simc's table does not carry get no slot and cannot be written
            # into a profile. Published rather than dropped: a run where this is
            # large is a run whose gear half is not usable, and nothing else in the
            # file would say so.
            "itemsWithoutASlot": sorted(set(unresolved_items)),
            # Gear entries that could not be read *at all* -- as opposed to read and
            # not placeable, above. An empty slot is an ordinary zero-id entry, so a
            # handful is normal; every entry of every player is Warcraft Logs having
            # renamed `combatantInfo.gear[].id`, and without this number that run
            # publishes builds with `simcGear: []` and reads as "these builds wear
            # nothing worth writing down".
            "gearEntriesSkipped": sum(o.gear_entries_skipped for o in observations),
            # And the third state: a row that carried no gear array at all, which is
            # what `playerDetails` returns when it is asked without
            # `includeCombatantInfo: true`. It read as an empty harvest with healthy
            # numbers everywhere else on the first live probe.
            "playersWithoutCombatantInfo": sum(1 for o in observations if o.combatant_info_missing),
        },
    }
    if query_plan:
        document["source"]["queries"] = query_plan
    if ledger:
        document["cost"] = ledger
    return document


#: Fields that describe *the run* rather than the data it produced.
_PROVENANCE = ("generatedAt", "cost")


def write_harvested_builds(out_dir: Path, document: dict) -> Path:
    """Write ``<out_dir>/harvested-builds.json``, keeping the stamp when nothing moved.

    Same rule as the manifest and ``fights.json``: a wall-clock timestamp that
    rewrites itself every run means every run commits and "a diff means something
    moved" stops being true. ``cost`` travels with the timestamp because it is a
    measurement *of the run*, not of the game -- an identical harvest read out of a
    warm cache costs nothing and would otherwise rewrite the file to say so.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "harvested-builds.json"

    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        published = None

    settled = document
    if isinstance(published, dict):
        comparable = {k: v for k, v in document.items() if k not in _PROVENANCE}
        if {k: v for k, v in published.items() if k not in _PROVENANCE} == comparable:
            settled = dict(document)
            for key in _PROVENANCE:
                if key in published:
                    settled[key] = published[key]
                else:
                    settled.pop(key, None)
            log.info("harvest unchanged; keeping generatedAt %s", settled.get("generatedAt"))

    path.write_text(json.dumps(settled, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------


@dataclass
class HarvestSettings:
    encounter_ids: tuple[int, ...]
    difficulty: int
    metric: str = "dps"
    #: Distinct kills to read per encounter. The main cost dial: each one is two
    #: report-level queries, and each one yields a whole raid's worth of players.
    reports: int = 10
    #: One, matching the CLI default and its stated reason -- unlike the fight probe
    #: this command does not need the earliest kills, so the first page of the
    #: damage ranking is the sample. The dataclass said 8, so any caller building
    #: settings directly (five of the tests, and any future scheduled job) got eight
    #: times the ranking queries with no flag set and nothing saying so.
    rankings_pages: int = 1
    page: int = 1
    order: str = "top"
    point_ceiling: float = 0.8
    max_sources: int = 3
    #: Limit the harvest to these spec keys (``death_knight_frost``). Empty means
    #: every damage player in the sampled kills.
    only_specs: tuple[str, ...] = ()
    #: The same selection as ``(class, spec)`` in simc's spelling, which is what
    #: targets the *ranking* query. Empty means the encounter's overall ranking.
    #: See ``gather_rankings``: without this ``--spec`` could only narrow what the
    #: top overall parses happened to contain.
    spec_targets: tuple[tuple[str, str], ...] = ()


def resolve_spec_selection(
    keys: tuple[str, ...], tables: TalentTables
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    """``--spec`` values -> ``(canonical keys, (class, spec) pairs, unresolved)``.

    One lookup, because ``--spec`` does two jobs: it narrows the published file, and
    it targets the ranking query so the sampled kills are ones the spec actually
    parsed in. Both need the same answer and taking it twice is how they would drift.

    Resolved through the same fold everything else here uses, so ``deathknight_frost``
    -- Warcraft Logs' spelling, which is what somebody reading a log will type --
    resolves to ``death_knight_frost`` rather than silently matching nothing.
    """
    by_fold = {
        fold_name(wow_class + spec): (f"{_slug(wow_class)}_{_slug(spec)}", (wow_class, spec))
        for wow_class, spec in tables.spec_ids
    }
    resolved: list[str] = []
    targets: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for key in keys:
        found = by_fold.get(fold_name(key))
        if found is None:
            unresolved.append(key)
            continue
        if found[0] not in resolved:
            resolved.append(found[0])
            targets.append(found[1])
    return tuple(resolved), tuple(targets), tuple(unresolved)


@dataclass
class QueryPlan:
    """What a pass will send, counted rather than estimated.

    The point cost of a Warcraft Logs query is not published and this module does
    not predict one. The *number of requests* is arithmetic, it is the thing a
    reader can check against the ledger afterwards, and it is what the two-per-kill
    design is for.
    """

    rankings: int = 0
    player_details: int = 0
    talent_codes: int = 0
    rate_limit: int = 2
    #: One per encounter that had to check a live twin's name before harvesting it,
    #: which is at most one per encounter and none at all for an id with parses.
    encounter_names: int = 0

    @property
    def total(self) -> int:
        return (
            self.rankings
            + self.player_details
            + self.talent_codes
            + self.rate_limit
            + self.encounter_names
        )

    def to_json(self) -> dict:
        return {
            "rankings": self.rankings,
            "playerDetails": self.player_details,
            "talentCodes": self.talent_codes,
            "rateLimit": self.rate_limit,
            "encounterNames": self.encounter_names,
            "total": self.total,
            "note": (
                "Request counts, not points. Two report-level queries per sampled "
                "kill regardless of how many players are wanted from it: "
                "playerDetails returns the whole raid, and every actor's talent "
                "code is aliased into one request. What that costs in points is "
                "read back from rateLimitData in `cost`."
            ),
        }


def observations_from_fight(
    rows: list[dict],
    codes: dict[int, str],
    *,
    report: str,
    fight_id: int,
    encounter_id: int,
    encounter_name: str,
    difficulty: int,
    killed_at_ms: float,
    inventory: dict[int, int],
    tables: TalentTables,
    only_specs: tuple[str, ...] = (),
) -> tuple[list[Observation], tuple[int, ...]]:
    """``(observations, item ids with no slot)`` for one sampled kill.

    Takes the rows ``player_detail_rows`` already produced rather than the payload
    again: the caller has to parse it anyway to know which actors to ask for talent
    codes, and parsing it twice meant a second ``json.loads`` of a twenty-player
    table on every kill whose ``playerDetails`` arrived as a JSON string -- a shape
    this code explicitly supports.

    Pure: everything it needs has already been fetched, which is what lets the whole
    extraction be driven from hand-written payloads in the tests without a client.
    ``tables`` is data too, and it is here because the id this produces has to be
    simc's spelling rather than Warcraft Logs'.
    """
    observations: list[Observation] = []
    unresolved: list[int] = []

    for row in rows:
        actor_id = row.get("id")
        wow_class = row.get("type")
        spec = spec_of_row(row)
        if not isinstance(actor_id, int) or not isinstance(wow_class, str) or not spec:
            continue

        pieces, skipped = gear_from_row(row)
        gear, missing = resolve_slots(pieces, inventory)
        unresolved.extend(missing)
        no_combatant_info = gear_array(row) is None

        level = row.get("maxItemLevel")
        if not isinstance(level, (int, float)):
            level = row.get("minItemLevel")

        wow_class, spec = tables.canonical_names(wow_class, spec)
        observation = Observation(
            report=report,
            fight_id=fight_id,
            actor_id=actor_id,
            encounter_id=encounter_id,
            encounter_name=encounter_name,
            difficulty=difficulty,
            killed_at_ms=killed_at_ms,
            wow_class=wow_class,
            spec=spec,
            item_level=float(level) if isinstance(level, (int, float)) else None,
            talent_hash=codes.get(actor_id),
            gear=gear,
            gear_entries_skipped=skipped,
            combatant_info_missing=no_combatant_info,
        )
        if only_specs and observation.spec_key not in only_specs:
            continue
        observations.append(observation)

    return observations, tuple(unresolved)


def stated_difficulty(entry: dict) -> object:
    """The difficulty a ranking row states, or ``None`` if it states none.

    ``characterRankings`` is an untyped JSON scalar, so whether a row carries this
    at all is not knowable from the schema and is not assumed here. Returning
    ``None`` for an absent field is what lets ``check_difficulty`` apply its
    unknown-is-not-wrong rule instead of this function guessing one.
    """
    for key in ("difficulty", "difficultyID", "difficultyId"):
        value = entry.get(key)
        if value is not None:
            return value
    return None


def check_difficulty(stated: object, wanted: int, where: str) -> None:
    """Refuse a fight that states a difficulty this harvest did not ask for.

    A filter would be the wrong instrument. The rankings query is already scoped to
    one difficulty, so a fight arriving with another one means the scoping did not
    hold, and quietly dropping it would hide that while producing a plausible file.
    A fight that states *no* difficulty is allowed through -- unknown is not the
    same as wrong, and the same three-way rule already governs
    ``firstkills.kills_from_report``.
    """
    if stated is None:
        return
    if not isinstance(stated, int) or stated != wanted:
        raise DifficultyMixed(
            f"{where} states difficulty {stated!r}, this harvest asked for {wanted}. "
            f"Builds from two difficulties answer different questions and must not "
            f"be pooled."
        )


@dataclass
class ProbeCapture:
    """The raw payloads of the first kill a sweep read, kept for ``--probe``.

    **Kept rather than fetched again**, and both halves of that matter.

    *The count.* The first version of the probe asked for the rankings, the player
    details and the talent codes a second time, after the sweep had already paid for
    them: driven through the tests' own stub it made **eight** client calls while
    the run printed "queries sent: 5". Without a warm cache three of those are
    billed requests, so the measured points were divided by five queries and one
    kill and every extrapolation came out about 1.6x too large -- enough to call an
    affordable pass unaffordable, which is the one decision the probe exists to
    inform. It also re-fetched page 1 even when ``--page`` named another.

    *What it printed.* ``probe_shapes`` was handed the **parsed** result, so a run
    that produced no observations -- because a bucket was renamed, because
    ``playerDetails`` changed shape, or simply because ``--spec`` filtered the kill
    out -- printed the least information in exactly the case the probe exists to
    diagnose. What is kept here is the response itself, so a schema move is
    describable whether or not anything could be read out of it.
    """

    rankings: dict | None = None
    details: object = None
    codes: dict[int, str] = field(default_factory=dict)
    seen_a_fight: bool = False

    def keep_rankings(self, page: dict) -> None:
        if self.rankings is None:
            self.rankings = page

    def forget_rankings(self) -> None:
        """Drop the kept page when the harvest moves to another encounter id.

        The page kept from a PTR id with no ranked parses describes the id that was
        *not* read. Printed as the probe's payload shape it would say
        "characterRankings: 0 row(s)" about a run that then read a hundred of them,
        which is the probe reporting a schema problem it does not have.
        """
        self.rankings = None

    def keep_fight(self, details: object, codes: dict[int, str]) -> None:
        if not self.seen_a_fight:
            self.seen_a_fight = True
            self.details = details
            self.codes = dict(codes)


def gather_rankings(client, encounter_id: int, settings, plan: QueryPlan, capture=None):
    """The ranking pages one encounter's kills are chosen from.

    Two behaviours worth stating, because both were wrong:

    * **``--spec`` reaches the sample.** Without a spec named, the kills come from
      the encounter's overall damage ranking -- the top parses of anybody -- and a
      spec filter applied afterwards can only narrow what those happened to contain.
      The specs this command exists for (Havoc, Arms, Fury, Feral, Devourer) are the
      ones least likely to be in a top guild's roster, so ``--spec`` over ten kills
      could yield nothing and an empty ``specs`` array with nothing saying why.
      ``spec_rankings`` takes a class and a spec and asks for *their* parses, which
      is what makes the sample answer the question. It costs one extra ranking query
      per spec per page and nothing at all per kill -- ranking queries are the cheap
      kind and the report-level cost is per kill either way.
    * **An exhausted list stops the loop.** Pages were gathered unconditionally, so
      an encounter whose rankings hold twelve parses still paid for eight pages.
    """
    from . import fightprobe
    from .warcraftlogs import _ranking_entries

    targets: tuple[tuple[str, str] | None, ...] = settings.spec_targets or (None,)
    pages: list[dict] = []
    for target in targets:
        for offset in range(max(settings.rankings_pages, 1)):
            fightprobe.check_budget(client, settings.point_ceiling)
            if target is None:
                page = client.encounter_rankings(
                    encounter_id,
                    difficulty=settings.difficulty,
                    metric=settings.metric,
                    page=settings.page + offset,
                )
            else:
                page = client.spec_rankings(
                    encounter_id,
                    target[0],
                    target[1],
                    difficulty=settings.difficulty,
                    metric=settings.metric,
                    page=settings.page + offset,
                )
            plan.rankings += 1
            pages.append(page)
            if capture is not None:
                capture.keep_rankings(page)
            if not _ranking_entries(page):
                break
    return pages


def harvest_encounter(
    client,
    encounter_id: int,
    settings: HarvestSettings,
    inventory: dict[int, int],
    plan: QueryPlan,
    tables: TalentTables,
    capture: ProbeCapture | None = None,
) -> tuple[list[Observation], dict, list[str]]:
    """Sample kills of one encounter and read every damage player out of them.

    ``(observations, encounter summary, buckets seen)``.

    **A budget abort returns what it collected**, with the reason on the summary as
    ``stoppedBy``. That is ``fightprobe.probe_encounter``'s contract, which this
    function's docstring already claimed to follow while doing the opposite: the
    exception escaped, so kills 1-8 of the ninth encounter were discarded along with
    the encounter's summary, after they had been paid for. The reason travels on the
    summary rather than as a fourth return value for the same reason
    ``itemsWithoutASlot`` does -- a fourth return value is the one a caller forgets.

    ``DifficultyMixed`` is deliberately *not* caught here. It is a refusal for the
    whole run rather than a verdict about one encounter, and it is raised before any
    kill of this encounter has been read.
    """
    from . import fightprobe
    from .warcraftlogs import _ranking_entries

    observations: list[Observation] = []
    buckets: list[str] = []
    unresolved: list[int] = []
    name = f"encounter {encounter_id}"
    read = 0
    stopped: str | None = None

    def lookup_name(other_id: int) -> str | None:
        fightprobe.check_budget(client, settings.point_ceiling)
        plan.encounter_names += 1
        return client.encounter_name(other_id)

    # The value that survives a budget abort during the very first ranking query,
    # where nothing has been resolved yet and saying "harvested as filed" would be a
    # claim about a query that never came back.
    choice = IdChoice(encounter_id, encounter_id, "the pass stopped before this id was resolved")
    try:
        pages = gather_rankings(client, encounter_id, settings, plan, capture)
        name = next((page.get("name") for page in pages if page.get("name")), name)

        # Which id the kills are actually under. MID2's fight profiles carry PTR
        # encounter ids on purpose, and a PTR id has no ranked parses -- measured,
        # 53420 returns nothing where 3470 returns a hundred rows. The substitution
        # is verified by name and refused otherwise; see `choose_encounter_id`.
        choice = choose_encounter_id(
            encounter_id,
            next((page.get("name") for page in pages if page.get("name")), None),
            any(_ranking_entries(page) for page in pages),
            lookup_name,
        )
        if choice.refused:
            return [], _encounter_summary(settings, choice, name, 0, [], [], None), []
        if choice.substituted:
            if capture is not None:
                capture.forget_rankings()
            pages = gather_rankings(client, choice.used, settings, plan, capture)
            name = next((page.get("name") for page in pages if page.get("name")), name)

        # The rankings query is already scoped to one difficulty, so this can only
        # fire when that scoping did not hold -- which is exactly why it is a refusal
        # and not a filter. Checked here rather than after the kills are chosen,
        # because a row dropped by `select_report_fights` would take its
        # disagreement with it.
        for page in pages:
            for entry in _ranking_entries(page):
                check_difficulty(
                    stated_difficulty(entry),
                    settings.difficulty,
                    f"a ranking row of encounter {choice.used}",
                )

        for code, fight_id, started in warcraftlogs_select(pages, settings.reports, settings.order):
            fightprobe.check_budget(client, settings.point_ceiling)
            details = client.player_details(code, fight_id)
            plan.player_details += 1

            # Parsed once. The actor ids for the talent query and the observations
            # come out of the same rows; parsing the payload twice meant a second
            # `json.loads` of a twenty-player table on every kill whose
            # `playerDetails` arrived as a JSON string.
            rows, seen = player_detail_rows(details)
            buckets.extend(seen)
            actor_ids = sorted({row["id"] for row in rows if isinstance(row.get("id"), int)})
            codes: dict[int, str] = {}
            if actor_ids:
                fightprobe.check_budget(client, settings.point_ceiling)
                codes = client.talent_import_codes(code, fight_id, actor_ids)
                plan.talent_codes += 1
            if capture is not None:
                capture.keep_fight(details, codes)

            found, missing = observations_from_fight(
                rows,
                codes,
                report=code,
                fight_id=fight_id,
                # The id the kills are *under*, which is the live twin when one was
                # substituted. `summary["idResolution"]` carries the mapping back to
                # the id the tier files this boss under, so neither is lost.
                encounter_id=choice.used or encounter_id,
                encounter_name=name,
                difficulty=settings.difficulty,
                killed_at_ms=started,
                inventory=inventory,
                tables=tables,
                only_specs=settings.only_specs,
            )
            observations.extend(found)
            unresolved.extend(missing)
            read += 1
    except fightprobe.PointBudgetExhausted as exc:
        stopped = str(exc)

    summary = _encounter_summary(settings, choice, name, read, observations, unresolved, stopped)
    return observations, summary, sorted(set(buckets))


def _encounter_summary(
    settings: HarvestSettings,
    choice: IdChoice,
    name: str,
    read: int,
    observations: list[Observation],
    unresolved: list[int],
    stopped: str | None,
) -> dict:
    """One encounter's row of the published document.

    Built here rather than inline because there are two exits from
    ``harvest_encounter`` -- the ordinary one and the id refusal -- and a refusal
    that returned no summary would drop the encounter out of the file entirely,
    which is the one reading it must not have: an encounter nobody could harvest and
    an encounter nobody tried are different answers.
    """
    return {
        "id": choice.requested,
        "name": name,
        # Which id was actually read and why. `id` stays the one the tier files this
        # boss under, so `fight_profiles.json` still joins; `idResolution.used` is
        # where the kills came from, and it is what `source_json`'s `encounterID`
        # carries on every row.
        "idResolution": choice.to_json(),
        "killsRequested": settings.reports,
        "killsRead": read,
        "playersRead": len(observations),
        "killedBetween": date_span(observations),
        # An encounter whose rankings held fewer kills than were asked for is a fact
        # about the encounter, not a failure of the pass -- the same distinction the
        # fight probe draws with `searchExhausted`. Which is why it is false when the
        # point ceiling is what stopped the reading: `stoppedBy` says that instead,
        # and claiming both would say the encounter is thin when it is not. An id
        # that was refused is thin for a third reason again, which `idResolution`
        # states, so it does not claim this one either.
        "fewerKillsThanRequested": (
            read < settings.reports and stopped is None and not choice.refused
        ),
        # Carried on the encounter rather than returned alongside it, because it has
        # to reach the document and a fourth return value is one the caller can
        # forget to thread through. It was: the first version of this collected the
        # ids and dropped them on the way out, so `coverage.itemsWithoutASlot` was
        # permanently empty and a run whose gear half was unusable said nothing.
        "itemsWithoutASlot": sorted(set(unresolved)),
        "gearEntriesSkipped": sum(o.gear_entries_skipped for o in observations),
        # Players whose row carried no gear array at all. Zero here and zero above
        # means the gear really was read; this number equalling `playersRead` is the
        # blocker-1 failure, and nothing else in the file can distinguish it from a
        # raid that wears nothing.
        "playersWithoutCombatantInfo": sum(1 for o in observations if o.combatant_info_missing),
        **({"stoppedBy": stopped} if stopped else {}),
    }


def warcraftlogs_select(pages: list[dict], limit: int, order: str):
    """Indirection so the tests can drive the sweep without importing the client.

    ``select_report_fights`` already answers "which kills, one per report, in which
    order" and is pinned by ``test_warcraftlogs``; re-deriving it here would be a
    second copy of a rule that has been got wrong before.
    """
    from .warcraftlogs import select_report_fights

    return select_report_fights(pages, limit, order=order)


# --------------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------------


#: The size a full pass is extrapolated to when a measured per-kill cost exists:
#: 30 kills on each of the eight encounters MID2's raid has. Stated as a
#: constant so the extrapolation cannot quietly change size between runs.
FULL_PASS_KILLS = 240


def describe_cost(ledger: dict, plan: QueryPlan, kills: int) -> list[str]:
    """State what the pass cost, or that the counter refused to say.

    The rule this follows is already written in blood in this repository: the fight
    probe's first real run read ``pointsSpentThisHour`` as identical before and
    after, printed "0 points for a nine-boss pass", and invited the conclusion that
    the API is free. **A zero delta is UNMEASURED, not zero.** Both raw readings are
    printed so the next run can tell which of the two it got.

    The per-kill figure and the extrapolation below it are labelled as an
    extrapolation wherever they appear, because that is what they are: one
    measurement divided by a count and multiplied by a different count.
    """
    lines = [f"queries sent: {plan.total} ({plan.to_json()['note'].split('.')[0].lower()})"]
    spent = ledger.get("pointsSpentThisRun")
    limit = ledger.get("limitPerHour")

    if spent is None:
        lines.append("cost: no rate-limit reading came back, so this pass is unmeasured")
        return lines
    if spent <= 0:
        lines.append(
            f"cost: UNMEASURED -- the hourly counter did not move (readings "
            f"{ledger.get('firstReading')} -> {ledger.get('lastReading')} of {limit}). "
            f"That is not the same as free; treat the cost of a full pass as unknown "
            f"until a run moves it."
        )
        # Which of the two kinds of UNMEASURED this is. The bracketing readings are
        # never cached, so they always report the hour honestly -- but if the pass
        # between them was served from the response cache then it really did cost
        # nothing, and no number of re-runs against that cache will ever say more.
        # Without this line the two are indistinguishable from the output, and the
        # second dispatch of a probe is exactly when it happens.
        cached = ledger.get("cacheHits") or 0
        if cached:
            lines.append(
                f"cost: {cached} of {cached + (ledger.get('queries') or 0)} queries came "
                f"from the response cache, so this run genuinely spent nothing. To take "
                f"the measurement, run against an empty --cache directory."
            )
        return lines

    lines.append(f"cost: {spent:.1f} points of {limit} for {kills} kill(s), {plan.total} queries")
    if kills:
        per_kill = spent / kills
        full_pass = per_kill * FULL_PASS_KILLS
        share = f", i.e. {full_pass / limit:.1%} of one hour's budget" if limit else ""
        lines.append(
            f"cost: {per_kill:.2f} points per sampled kill (measured). EXTRAPOLATION, "
            f"not a measurement: {FULL_PASS_KILLS} kills (30 on each of 8 encounters) "
            f"would be about {full_pass:.0f} points{share}"
        )
    return lines


def probe_shapes(details: object, codes: dict[int, str], rankings: dict | None) -> list[str]:
    """What the live payloads actually look like, printed rather than assumed.

    Every GraphQL document in this project was written against a mirror of the v2
    schema, and the repository's standing rule is that the first live run of a query
    is a schema check as much as a measurement. This is that check made explicit and
    cheap: one kill, and the *keys* of each payload, so a field that moved is named
    in the run output instead of surfacing as an empty harvest.

    It also answers a question that cannot be answered offline and decides how much
    the rest of this command has to fetch: whether ``characterRankings`` rows
    already carry gear or talents, in which case a whole query per kill is
    unnecessary.

    **The arguments are the responses the sweep already fetched, not what it managed
    to parse out of them.** That is the difference between a probe that describes a
    schema move and one that goes quiet exactly when one happened: handed the parsed
    result, a run that read no observations printed nothing at all, and a ``--spec``
    probe that simply filtered its one kill out read as a failed schema check.
    """
    lines = ["--- payload shapes, read from the live service ---"]

    from .warcraftlogs import _ranking_entries

    if rankings is None:
        lines.append("characterRankings: no page was fetched (the run stopped before one)")
    else:
        entries = _ranking_entries(rankings)
        lines.append(f"characterRankings: {len(entries)} row(s)")
        if entries:
            lines.append(f"  ranking row keys: {sorted(entries[0])}")
            interesting = [k for k in entries[0] if "gear" in k.lower() or "talent" in k.lower()]
            lines.append(
                "  gear/talent already on the ranking row: "
                + (", ".join(interesting) if interesting else "none -- playerDetails is needed")
            )

    if details is None:
        lines.append("playerDetails: no kill was read, so there is no payload to describe")
        return lines

    rows, buckets = player_detail_rows(details)
    lines.append(f"playerDetails: buckets {buckets or 'none found'}, {len(rows)} dps row(s)")
    if not rows:
        # The case the probe is *for*. A bucket that was renamed, or a payload that
        # is not a table at all, leaves nothing to describe field by field -- so the
        # top-level shape is printed instead of nothing.
        top = sorted(details) if isinstance(details, dict) else type(details).__name__
        lines.append(f"  no dps rows to read. Top-level payload shape: {top}")
    else:
        lines.append(f"  dps row keys: {sorted(rows[0])}")
        lines.append(f"  combatantInfo: {describe_combatant_info(rows[0])}")
        pieces, skipped = gear_from_row(rows[0])
        missing = sum(1 for row in rows if gear_array(row) is None)
        lines.append(
            f"  gear entries: {len(pieces)} readable, {skipped} skipped; "
            f"{missing} of {len(rows)} dps row(s) carry no gear array at all"
        )
        gear = gear_array(rows[0]) or []
        if gear and isinstance(gear[0], dict):
            lines.append(f"  gear entry keys: {sorted(gear[0])}")

    lines.append(f"talentImportCode: {len(codes)} of the fight's actors returned a code")
    if codes:
        sample = next(iter(codes.values()))
        lines.append(f"  sample length {len(sample)} characters")
    return lines


def add_arguments(parser) -> None:
    parser.add_argument("--tier", default="MID2", help="tier the boss list is read from")
    parser.add_argument(
        "--encounter",
        type=int,
        action="append",
        help="encounter id to harvest (repeatable); default is every boss the tier's "
        "fight profiles list",
    )
    parser.add_argument(
        "--difficulty",
        type=int,
        default=5,
        help="5 = Mythic, 4 = Heroic. One per run: builds from two difficulties "
        "answer different questions and the run refuses to pool them",
    )
    parser.add_argument("--metric", default="dps", help="ranking metric used to pick kills")
    parser.add_argument(
        "--reports",
        type=int,
        default=10,
        help="distinct kills to sample per encounter (default 10). The main cost "
        "dial: two report-level queries each, and each one yields every damage "
        "player in the pull rather than one",
    )
    parser.add_argument(
        "--rankings-pages",
        type=int,
        default=1,
        help="ranking pages to gather per encounter before choosing kills. Ranking "
        "queries are the cheap kind, but unlike the fight probe this command does "
        "not need the earliest kills, so one page is the default",
    )
    parser.add_argument("--page", type=int, default=1, help="first rankings page (default 1)")
    parser.add_argument(
        "--order",
        choices=("top", "first"),
        default="top",
        help="which kills to sample. 'top' is the rankings' own damage order, which "
        "is what this command wants -- the question is what good players run. "
        "'first' takes the earliest kills instead, i.e. builds from before the tier "
        "was solved",
    )
    parser.add_argument(
        "--spec",
        action="append",
        help="harvest this spec id, e.g. 'death_knight_frost' (repeatable). The "
        "kills are then chosen from that spec's own rankings rather than the "
        "encounter's overall damage ranking, which matters because the specs with "
        "no simc profile are the ones least likely to be in a top guild's roster. "
        "Costs one extra ranking query per spec -- the cheap kind -- and nothing "
        "per kill, since playerDetails returns the whole raid either way",
    )
    parser.add_argument(
        "--simc-source",
        default="simc",
        help="a simc checkout (engine/dbc/generated). No binary needed: the talent "
        "tables decode the hashes and the item table names each gear slot",
    )
    parser.add_argument(
        "--ptr",
        action="store_true",
        help="read simc's PTR trait tables, which is what the current tier runs on",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="read ONE kill of ONE encounter, print what the payloads actually look "
        "like and what the pass cost, and write nothing. The cheap way to find out "
        "whether a full sweep is affordable before running one",
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=3,
        help="kills to publish per distinct build (default 3); the count is always "
        "published in full",
    )
    parser.add_argument(
        "--point-ceiling",
        type=float,
        default=0.8,
        help="abort once this fraction of the hourly point budget is spent",
    )
    parser.add_argument("--out", default="web/public/data", help="dataset root")
    parser.add_argument("--cache", help="response cache directory; a warm cache costs no points")
    parser.add_argument("--profiles-file", help="alternative fight profile file")


def cmd_harvest_builds(args) -> int:
    """Harvest, validate, deduplicate, publish -- or, with ``--probe``, just measure.

    ``--probe`` exists because of the order this had to be built in. Whether a full
    pass is affordable is a *measurement*, Warcraft Logs publishes no cost formula,
    and building a sweep nobody can afford to run would be the expensive mistake. So
    the smallest possible pass -- one kill of one encounter -- is a first-class mode
    that writes nothing, prints the payload shapes and reports what the counter did.
    """
    from . import fightprofile
    from .warcraftlogs import Credentials, WarcraftLogsClient, WarcraftLogsError

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        log.error("%s", exc)
        return 2

    simc_dir = Path(args.simc_source)
    try:
        tables = TalentTables.load(simc_dir, ptr=args.ptr)
    except (OSError, ValueError) as exc:
        log.error("simc's talent tables could not be read from %s: %s", simc_dir, exc)
        return 2
    try:
        inventory = equipment.inventory_types(simc_dir)
    except OSError as exc:
        log.error("simc's item table could not be read from %s: %s", simc_dir, exc)
        return 2
    log.info(
        "simc tables: %d classes of trait data, %d items, %d hero trees",
        len(tables.nodes),
        len(inventory),
        len(tables.sub_trees),
    )

    encounter_ids = list(args.encounter or ())
    if not encounter_ids:
        profiles = fightprofile.load_profiles(
            args.tier, Path(args.profiles_file) if args.profiles_file else None
        )
        encounter_ids = sorted(profiles.profiles)
        if not encounter_ids:
            # Same refusal as the fight probe. A tier with no boss list has no
            # encounters to harvest from, and substituting another season's raid
            # would produce a full set of real builds filed under the wrong name.
            log.error(
                "no fight profiles for tier %s, so there is no boss list to harvest "
                "from. Name encounters with --encounter, or run `wowdps fight-zones` "
                "to seed the tier.",
                args.tier,
            )
            return 1

    only_specs, spec_targets, unresolved_specs = resolve_spec_selection(
        tuple(args.spec or ()), tables
    )
    if unresolved_specs:
        # Refused rather than ignored: an unmatchable --spec would target no
        # ranking, filter every observation out, and publish an empty `specs`
        # array with nothing in the file saying why -- which is the failure this
        # flag was making in the first place.
        log.error(
            "no spec named %s; simc's own table spells these as class+spec, e.g. "
            "death_knight_frost or hunter_beast_mastery",
            ", ".join(repr(key) for key in unresolved_specs),
        )
        return 2

    # Truncated *before* the settings are built, so `settings.encounter_ids` is what
    # the run actually reads. It held every boss of the tier while the loop read one,
    # so any future code reading the list off the settings object -- the obvious
    # place -- would sweep the whole tier in the mode whose contract is "one kill of
    # one encounter, write nothing".
    if args.probe:
        encounter_ids = encounter_ids[:1]
        log.info("probe mode: one kill of encounter %d, writing nothing", encounter_ids[0])

    settings = HarvestSettings(
        encounter_ids=tuple(encounter_ids),
        difficulty=args.difficulty,
        metric=args.metric,
        reports=1 if args.probe else args.reports,
        rankings_pages=1 if args.probe else args.rankings_pages,
        page=args.page,
        order=args.order,
        point_ceiling=args.point_ceiling,
        max_sources=args.max_sources,
        only_specs=only_specs,
        spec_targets=spec_targets,
    )

    cache_dir = Path(args.cache) if args.cache else None
    plan = QueryPlan()
    observations: list[Observation] = []
    encounters: list[dict] = []
    transcript: list[str] = []
    capture = ProbeCapture() if args.probe else None
    refusal: str | None = None
    stopped_early = False

    with WarcraftLogsClient(credentials, cache_dir=cache_dir) as client:
        before = client.rate_limit()
        log.info(
            "point budget: %s of %s spent this hour, resets in %ss",
            before.get("pointsSpentThisHour"),
            before.get("limitPerHour"),
            before.get("pointsResetIn"),
        )
        for encounter_id in encounter_ids:
            try:
                found, summary, buckets = harvest_encounter(
                    client, encounter_id, settings, inventory, plan, tables, capture
                )
            except DifficultyMixed as exc:
                # A refusal for the run, and it was caught nowhere: it propagated
                # through a loop handling only PointBudgetExhausted and
                # WarcraftLogsError and out of `cli.main`, so a pass that had already
                # harvested eight encounters printed a traceback, wrote no file and
                # burned the points for nothing. What is kept is every encounter that
                # ran clean -- all of it at the difficulty that was asked for, so
                # nothing is pooled -- and the exit code says a person has to read it.
                refusal = str(exc)
                log.error("refusing to pool difficulties: %s", exc)
                break
            except WarcraftLogsError as exc:
                log.error("encounter %d failed: %s", encounter_id, exc)
                continue
            observations.extend(found)
            summary["playerDetailBuckets"] = buckets
            encounters.append(summary)
            resolution = summary["idResolution"]
            # Printed for every encounter, not only for a substituted one: "which id
            # did this actually read" is a question the reader has to be able to
            # answer without knowing which ids happen to be PTR ones. It goes on the
            # transcript as well as the log because the transcript is what the
            # workflow tees into its artifact and its run summary.
            transcript.append(f"encounter {encounter_id}: {resolution['reason']}")
            if resolution["used"] is None:
                log.warning("encounter %d: %s", encounter_id, resolution["reason"])
            else:
                log.info("encounter %d: %s", encounter_id, resolution["reason"])
            log.info(
                "encounter %d (%s): %d kill(s), %d damage player(s)",
                resolution["used"] if resolution["used"] is not None else encounter_id,
                summary["name"],
                summary["killsRead"],
                summary["playersRead"],
            )
            if summary.get("stoppedBy"):
                # The encounter kept the kills it had already paid for; the pass
                # stops here and is resumable by re-running once points reset.
                log.warning("%s", summary["stoppedBy"])
                stopped_early = True
                break

        if args.probe and capture is not None:
            # No second fetch. The sweep above already paid for these three
            # payloads; asking again made the probe's own request count wrong by
            # 60% in the direction that inflates the extrapolation.
            transcript.extend(probe_shapes(capture.details, capture.codes, capture.rankings))

        client.rate_limit()
        ledger = client.ledger.to_json()

    kills = len({(o.report, o.fight_id) for o in observations})
    transcript.extend(describe_cost(ledger, plan, kills))

    if args.probe:
        # The whole point of a probe is what it decoded, so it says so -- but it
        # writes nothing, because one kill is not a dataset.
        #
        # **Every player, not the first five.** A probe reads one kill, so this is
        # at most a raid's worth of lines, and which specs a kill contained is the
        # question a reader brings to it: the specs this command exists for are the
        # ones least likely to be in a top guild's roster, and "the sample did not
        # contain one" is a finding that a truncated list cannot state.
        for observation in observations:
            verdict = validate(observation, tables)
            transcript.append(
                f"  {observation.spec_key}: {verdict.reason}"
                + (f" ({verdict.detail})" if verdict.detail else "")
            )
        if refusal:
            transcript.append(f"refused: {refusal}")
        print("\n".join(transcript))
        return 1 if refusal else 0

    document = build_document(
        args.tier,
        settings.difficulty,
        observations,
        tables,
        encounters,
        ledger=ledger,
        max_sources=settings.max_sources,
        unresolved_items=tuple(
            item for encounter in encounters for item in encounter.get("itemsWithoutASlot", ())
        ),
        query_plan=plan.to_json(),
    )
    path = write_harvested_builds(Path(args.out) / args.tier, document)
    coverage = document["coverage"]
    # Which specs a pass actually got a usable build for, printed rather than left
    # to be read out of the file. A harvested hash is current and legal by
    # construction, so this list is the answer to "which specs can be published from
    # real characters" -- including the ones simc's own talent parser refuses to
    # load, which have no number at all without it. A spec with kills and no build
    # is listed too: that is a rejection to read, not an absence.
    transcript.append(
        f"specs: {coverage['specsWithABuild']} with a usable build, "
        f"{coverage['specsSeen']} seen in the sampled kills"
    )
    for row in document["specs"]:
        transcript.append(
            f"  {row['specId']}: {row['distinctBuilds']} distinct build(s) from "
            f"{row['killsUsable']} of {row['killsHarvested']} kill(s)"
            + (f", {len(row['rejected'])} rejected" if row["rejected"] else "")
        )
    log.info(
        "%s: %d build(s) across %d spec(s) from %d kill(s); %d observation(s) rejected",
        path,
        coverage["buildsTotal"],
        coverage["specsWithABuild"],
        document["source"]["killsSampled"],
        coverage["rejectedTotal"],
    )
    if refusal:
        transcript.append(f"refused: {refusal}")
        transcript.append(f"what ran clean before the refusal was still written to {path}")
    print("\n".join(transcript))
    # 1 is the one-difficulty refusal: the file is honest but a person has to read
    # the run before anything is committed off it. 2 is the point ceiling, which
    # `harvest-builds.yml` turns into a warning -- what was harvested is worth
    # keeping and re-running once points reset continues the pass.
    if refusal:
        return 1
    return 2 if stopped_early else 0
