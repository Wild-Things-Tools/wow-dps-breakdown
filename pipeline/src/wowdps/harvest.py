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

from . import equipment, specindex, talenttree
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

    @property
    def spec_key(self) -> str:
        """``mage_arcane``, the id shape the rest of the dataset joins on."""
        return f"{_slug(self.wow_class)}_{_slug(self.spec)}"

    def source_json(self) -> dict:
        return {
            "report": self.report,
            "fightID": self.fight_id,
            "actorID": self.actor_id,
            "encounterID": self.encounter_id,
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


def gear_from_row(row: dict) -> tuple[list[GearPiece], int]:
    """``(pieces, entries skipped)`` from a ``playerDetails`` row's combatant info.

    Skipped entries are counted rather than ignored: an empty slot is an ordinary
    zero-id entry and a row of them is a payload this code did not understand, and
    only the count tells those two apart from outside.
    """
    info = row.get("combatantInfo")
    gear = info.get("gear") if isinstance(info, dict) else row.get("gear")
    if not isinstance(gear, list):
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
    """Name each item's slot from simc's item table. Returns ``(pieces, unresolved ids)``.

    The slot is a property of the item and simc ships it, so nothing here asserts
    what position 12 of somebody else's array means. The one thing the item table
    genuinely cannot answer is *which* ring is ``finger1``, because that is not a
    property of the ring; paired slots are numbered by order of appearance, which
    is stable for a given payload and is the only orderable fact available.

    An item simc has never heard of keeps its data and gets no slot. It cannot be
    put in a profile, so claiming a slot for it would be a claim with nothing
    behind it -- the same refusal ``gearpool`` makes for an item it cannot simulate.
    """
    used: dict[str, int] = {}
    resolved: list[GearPiece] = []
    unresolved: list[int] = []
    for piece in pieces:
        inventory_type = inventory.get(piece.item_id)
        slot = equipment.INVENTORY_TYPE_SLOTS.get(inventory_type) if inventory_type else None
        if slot is None:
            unresolved.append(piece.item_id)
            resolved.append(piece)
            continue
        if slot in equipment.PAIRED_SLOTS:
            used[slot] = used.get(slot, 0) + 1
            slot = f"{slot}{used[slot]}"
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
    class_id = talenttree.CLASS_IDS.get(observation.wow_class)
    if class_id is None:
        return Verdict(REASON_UNKNOWN_CLASS, f"no simc class id for {observation.wow_class!r}")

    expected = tables.spec_ids.get((observation.wow_class, observation.spec))
    if expected is None:
        return Verdict(
            REASON_UNKNOWN_SPEC,
            f"simc names no spec {observation.spec!r} for {observation.wow_class}",
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
    rankings_pages: int = 8
    page: int = 1
    order: str = "top"
    point_ceiling: float = 0.8
    max_sources: int = 3
    #: Limit the harvest to these spec keys (``mage_arcane``). Empty means every
    #: damage player in the sampled kills, which costs exactly the same.
    only_specs: tuple[str, ...] = ()


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

    @property
    def total(self) -> int:
        return self.rankings + self.player_details + self.talent_codes + self.rate_limit

    def to_json(self) -> dict:
        return {
            "rankings": self.rankings,
            "playerDetails": self.player_details,
            "talentCodes": self.talent_codes,
            "rateLimit": self.rate_limit,
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
    details: object,
    codes: dict[int, str],
    *,
    report: str,
    fight_id: int,
    encounter_id: int,
    encounter_name: str,
    difficulty: int,
    killed_at_ms: float,
    inventory: dict[int, int],
    only_specs: tuple[str, ...] = (),
) -> tuple[list[Observation], list[str], tuple[int, ...]]:
    """``(observations, buckets seen, item ids with no slot)`` for one sampled kill.

    Pure: everything it needs has already been fetched. That is what lets the whole
    extraction be driven from hand-written payloads in the tests without a client.
    """
    rows, buckets = player_detail_rows(details)
    observations: list[Observation] = []
    unresolved: list[int] = []

    for row in rows:
        actor_id = row.get("id")
        wow_class = row.get("type")
        spec = spec_of_row(row)
        if not isinstance(actor_id, int) or not isinstance(wow_class, str) or not spec:
            continue

        pieces, _skipped = gear_from_row(row)
        gear, missing = resolve_slots(pieces, inventory)
        unresolved.extend(missing)

        level = row.get("maxItemLevel")
        if not isinstance(level, (int, float)):
            level = row.get("minItemLevel")

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
        )
        if only_specs and observation.spec_key not in only_specs:
            continue
        observations.append(observation)

    return observations, buckets, tuple(unresolved)


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


def harvest_encounter(
    client,
    encounter_id: int,
    settings: HarvestSettings,
    inventory: dict[int, int],
    plan: QueryPlan,
) -> tuple[list[Observation], dict, list[str]]:
    """Sample kills of one encounter and read every damage player out of them.

    ``(observations, encounter summary, buckets seen)``. A budget abort is allowed
    to escape: the caller keeps what earlier encounters produced, which is the same
    contract ``probe_encounter`` has and the reason a stopped pass here reports
    "what has been collected" rather than losing it.
    """
    from . import fightprobe

    pages = []
    for offset in range(settings.rankings_pages):
        fightprobe.check_budget(client, settings.point_ceiling)
        pages.append(
            client.encounter_rankings(
                encounter_id,
                difficulty=settings.difficulty,
                metric=settings.metric,
                page=settings.page + offset,
            )
        )
        plan.rankings += 1

    name = next(
        (page.get("name") for page in pages if page.get("name")), f"encounter {encounter_id}"
    )

    # The rankings query is already scoped to one difficulty, so this can only fire
    # when that scoping did not hold -- which is exactly why it is a refusal and not
    # a filter. Checked here rather than after the kills are chosen, because a row
    # dropped by `select_report_fights` would take its disagreement with it.
    from .warcraftlogs import _ranking_entries

    for page in pages:
        for entry in _ranking_entries(page):
            check_difficulty(
                stated_difficulty(entry),
                settings.difficulty,
                f"a ranking row of encounter {encounter_id}",
            )

    kills = warcraftlogs_select(pages, settings.reports, settings.order)

    observations: list[Observation] = []
    buckets: list[str] = []
    unresolved: list[int] = []
    read = 0
    for code, fight_id, started in kills:
        fightprobe.check_budget(client, settings.point_ceiling)
        details = client.player_details(code, fight_id)
        plan.player_details += 1

        rows, seen = player_detail_rows(details)
        buckets.extend(seen)
        actor_ids = sorted({row["id"] for row in rows if isinstance(row.get("id"), int)})
        codes: dict[int, str] = {}
        if actor_ids:
            fightprobe.check_budget(client, settings.point_ceiling)
            codes = client.talent_import_codes(code, fight_id, actor_ids)
            plan.talent_codes += 1

        found, _seen, missing = observations_from_fight(
            details,
            codes,
            report=code,
            fight_id=fight_id,
            encounter_id=encounter_id,
            encounter_name=name,
            difficulty=settings.difficulty,
            killed_at_ms=started,
            inventory=inventory,
            only_specs=settings.only_specs,
        )
        observations.extend(found)
        unresolved.extend(missing)
        read += 1

    summary = {
        "id": encounter_id,
        "name": name,
        "killsRequested": settings.reports,
        "killsRead": read,
        "playersRead": len(observations),
        "killedBetween": date_span(observations),
        # An encounter whose rankings held fewer kills than were asked for is a fact
        # about the encounter, not a failure of the pass -- the same distinction the
        # fight probe draws with `searchExhausted`.
        "fewerKillsThanRequested": read < settings.reports,
        # Carried on the encounter rather than returned alongside it, because it has
        # to reach the document and a fourth return value is one the caller can
        # forget to thread through. It was: the first version of this collected the
        # ids and dropped them on the way out, so `coverage.itemsWithoutASlot` was
        # permanently empty and a run whose gear half was unusable said nothing.
        "itemsWithoutASlot": sorted(set(unresolved)),
    }
    return observations, summary, sorted(set(buckets))


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


def probe_shapes(details: object, codes: dict[int, str], rankings: dict) -> list[str]:
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
    """
    lines = ["--- payload shapes, read from the live service ---"]

    from .warcraftlogs import _ranking_entries

    entries = _ranking_entries(rankings)
    lines.append(f"characterRankings: {len(entries)} row(s)")
    if entries:
        lines.append(f"  ranking row keys: {sorted(entries[0])}")
        interesting = [k for k in entries[0] if "gear" in k.lower() or "talent" in k.lower()]
        lines.append(
            "  gear/talent already on the ranking row: "
            + (", ".join(interesting) if interesting else "none -- playerDetails is needed")
        )

    rows, buckets = player_detail_rows(details)
    lines.append(f"playerDetails: buckets {buckets or 'none found'}, {len(rows)} dps row(s)")
    if rows:
        lines.append(f"  dps row keys: {sorted(rows[0])}")
        info = rows[0].get("combatantInfo")
        shape = sorted(info) if isinstance(info, dict) else type(info).__name__
        lines.append(f"  combatantInfo keys: {shape}")
        pieces, skipped = gear_from_row(rows[0])
        lines.append(f"  gear entries: {len(pieces)} readable, {skipped} skipped")
        gear = (info or {}).get("gear") if isinstance(info, dict) else None
        if isinstance(gear, list) and gear and isinstance(gear[0], dict):
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
        help="limit the harvest to one spec id, e.g. 'mage_arcane' (repeatable). "
        "Costs exactly the same as harvesting everything -- the queries are per "
        "kill, not per player -- so this only narrows the file",
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
    from . import fightprobe, fightprofile
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
        only_specs=tuple(args.spec or ()),
    )
    if args.probe:
        encounter_ids = encounter_ids[:1]
        log.info("probe mode: one kill of encounter %d, writing nothing", encounter_ids[0])

    cache_dir = Path(args.cache) if args.cache else None
    plan = QueryPlan()
    observations: list[Observation] = []
    encounters: list[dict] = []
    transcript: list[str] = []

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
                    client, encounter_id, settings, inventory, plan
                )
            except fightprobe.PointBudgetExhausted as exc:
                # What has been read is kept. The pass is resumable by re-running,
                # and losing eight encounters' worth of work to the ninth is a
                # mistake this repository has made once already.
                log.warning("%s", exc)
                break
            except WarcraftLogsError as exc:
                log.error("encounter %d failed: %s", encounter_id, exc)
                continue
            observations.extend(found)
            summary["playerDetailBuckets"] = buckets
            encounters.append(summary)
            log.info(
                "encounter %d (%s): %d kill(s), %d damage player(s)",
                encounter_id,
                summary["name"],
                summary["killsRead"],
                summary["playersRead"],
            )

        if args.probe:
            rankings = client.encounter_rankings(
                encounter_ids[0], difficulty=settings.difficulty, metric=settings.metric
            )
            first = next(iter(observations), None)
            details = client.player_details(first.report, first.fight_id) if first else None
            codes = (
                client.talent_import_codes(first.report, first.fight_id, [first.actor_id])
                if first
                else {}
            )
            transcript.extend(probe_shapes(details, codes, rankings))

        client.rate_limit()
        ledger = client.ledger.to_json()

    kills = len({(o.report, o.fight_id) for o in observations})
    transcript.extend(describe_cost(ledger, plan, kills))

    if args.probe:
        # The whole point of a probe is what it decoded, so it says so -- but it
        # writes nothing, because one kill is not a dataset.
        for observation in observations[:5]:
            verdict = validate(observation, tables)
            transcript.append(
                f"  {observation.spec_key}: {verdict.reason}"
                + (f" ({verdict.detail})" if verdict.detail else "")
            )
        print("\n".join(transcript))
        return 0

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
    log.info(
        "%s: %d build(s) across %d spec(s) from %d kill(s); %d observation(s) rejected",
        path,
        coverage["buildsTotal"],
        coverage["specsWithABuild"],
        document["source"]["killsSampled"],
        coverage["rejectedTotal"],
    )
    print("\n".join(transcript))
    return 0
