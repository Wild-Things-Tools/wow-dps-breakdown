"""The talent tree, decoded from simc's own data -- no external API.

The site shows a build's DPS, its gear and its hero tree. What it could not show is
the thing every reader actually asks first: *what does this build take?* The loadout
hash is in the dataset already, and it is unreadable.

The obvious route was the one wtt-backend uses -- Blizzard's Game Data API for the
tree layout, and Blizzard's character API for the selected nodes. It is the wrong one
here, twice over. That API hands back *a character's* selections, and this project has
no characters, only simc profiles; and it would make the talent view depend on
credentials and a live service, where every other number on the site is derived from
simc and byte-reproducible.

It turns out none of that is needed. ``engine/dbc/generated/trait_data.inc`` carries
every field the render needs -- node id, entry id, ``row``, ``col``, ``name``,
``max_ranks``, ``node_type``, the hero sub-tree id, and the spell id that a Wowhead
link needs -- and ``player.cpp`` carries the loadout format simc itself parses. So the
whole feature is a join of two things already in the checkout.

What is *not* in simc's data, and is not invented here:

- **Icons.** simc ships no icon name for a spell any more than for an item. The nodes
  render as Wowhead spell links, and Wowhead's own script draws the icon -- the same
  arrangement the Loot view already uses, and the reason the tree is HTML rather than
  SVG (``power.js`` skips anything whose ``nodeName`` is not ``A`` or ``AREA``).
- **Connector lines.** Blizzard's API has an ``unlocks`` edge list; simc has no edge
  table at all. The grid is drawn from ``row``/``col`` without the lines rather than
  guessing which node feeds which, because a wrong edge is a claim about the tree.
- **Hero tree names.** ``trait_data.inc`` stores the SELECTION rows with the literal
  name ``"0"``. The id is what it carries, so the id is what this module reports, and
  ``herotrees.py`` remains the thing that names a tree.

## How the decode was verified without a single API call

The format is read out of ``parse_traits_hash`` in ``engine/player/player.cpp``:
a 6-bit base64 alphabet read least-significant-bit first, an 8-bit version, a 16-bit
specialization id, a 128-bit tree hash that simc itself skips, and then, **for every
node of the class sorted by node id**, one bit for selected, then -- if selected --
purchased, partially-ranked (with 6 bits of rank), and choice (with 2 bits of index).

Four independent checks, all run offline against MID2's 26 real hashes:

1. **Version and specialization.** Every hash reports version 2 and the correct
   canonical spec id -- 251 Frost Death Knight, 62 Arcane Mage, 267 Destruction
   Warlock, and so on for all fifteen specs. A wrong alphabet or bit order cannot
   produce fifteen correct ids.
2. **The stream terminates.** Every build consumes its hash to within six bits of the
   end, which is the padding to a 6-bit boundary. A desynchronised reader overruns or
   stops early; these do neither.
3. **simc's own spec rule.** ``parse_traits_hash`` refuses a non-hero node whose
   ``id_spec`` does not contain the player's spec. Applied to the decoded selections:
   **zero violations across all 26 builds**. A misread node would almost certainly
   land on another spec's talent.
4. **The hero tree, against an independent derivation.** The SELECTION node yields
   exactly one sub-tree id per build, and across 26 builds those ids map one-to-one
   onto the hero tree names ``herotrees.py`` resolved by a completely different method
   (running the profile and reading which abilities fired). Eighteen trees, no
   collisions, no disagreements.

Two things that cost time and are easy to get wrong again:

- **``tree_index`` 5 and above are not player traits.** simc's ``talent_tree`` enum
  ends the player trees at ``MAX = 5``; ``EXPANSION = 6`` holds things like Midnight's
  runeforge traits, and ``generate_tree_nodes`` stops before them. Including them adds
  nodes to the stream and desynchronises the whole decode -- which is subtle, because
  it still produces plausible-looking talent names.
- **A hero *node* can belong to two hero trees.** Filtering the decoded hero nodes to
  the sub-tree the SELECTION node names is what makes the count come out at the ten
  points a player actually spends. simc models this the same way, with
  ``player_sub_trees``.

## The finding this immediately produced

Applied to MID2, twenty-four of twenty-six builds spend an identical **34 points in
the specialisation tree** and 35 or 36 in the class tree. Both **Frost Death Knight**
profiles spend **10**. Their spec trees are normal and their hero trees resolve
correctly, so this is not a decode failure -- simc's shipped MID2 Frost Death Knight
profiles genuinely leave most of the class tree unspent, which would understate the
spec. It is surfaced as a caveat rather than corrected here: this module reads
profiles, it does not write them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Blizzard's export alphabet, from ``MakeBase64ConversionTable()`` in
#: ``Blizzard_SharedXMLBase/ExportUtil.lua`` and copied verbatim into simc.
BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_INDEX = {char: value for value, char in enumerate(BASE64)}

#: All from ``Blizzard_ClassTalentImportExport.lua`` via simc's ``player.cpp``.
LOADOUT_VERSION = 2
VERSION_BITS = 8
SPEC_BITS = 16
TREE_BITS = 128
RANK_BITS = 6
CHOICE_BITS = 2
CHAR_BITS = 6

#: ``talent_tree`` in ``engine/sc_enums.hpp``. Anything at or above MAX is not a
#: player trait and must not enter the node stream -- see the module docstring.
TREE_INVALID, TREE_CLASS, TREE_SPEC, TREE_HERO, TREE_SELECTION, TREE_MAX = 0, 1, 2, 3, 4, 5

#: ``trait_node_type_e``: 0 normal, 1 tiered, 2 choice, 3 sub-tree selection.
NODE_NORMAL, NODE_TIERED, NODE_CHOICE, NODE_SELECTION = 0, 1, 2, 3

#: simc's class ids, which is what ``trait_data_t::id_class`` holds. Keyed on the
#: class names the dataset already publishes.
CLASS_IDS = {
    "Warrior": 1,
    "Paladin": 2,
    "Hunter": 3,
    "Rogue": 4,
    "Priest": 5,
    "Death Knight": 6,
    "Shaman": 7,
    "Mage": 8,
    "Warlock": 9,
    "Monk": 10,
    "Druid": 11,
    "Demon Hunter": 12,
    "Evoker": 13,
}

_ROW = re.compile(r"^\s*\{\s*(.*?)\s*\}\s*,\s*$")
#: The hero tree name table, whose rows are ``{ <sub tree id>, "<name>", <class id> }``.
#:
#: Matched on the **shared suffix**, because the array is named
#: ``__trait_sub_tree_data`` in the live file and ``__ptr_trait_sub_tree_data`` in the
#: PTR one -- the same split ``parse_spec_list`` already handles for
#: ``class_spec_id``. Anchoring on the live name found nothing in PTR mode, which is
#: the mode the current tier runs in. See ``parse_sub_tree_names``.
_SUB_TREE_TABLE = re.compile(r"trait_sub_tree_data\s*\{\s*\{(.*?)\}\s*\}\s*;", re.S)
_SUB_TREE_ROW = re.compile(r'\{\s*(\d+)\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*(\d+)\s*\}')
_NAME = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ARRAY = re.compile(r"\{[^}]*\}")


class TalentDecodeError(ValueError):
    """The hash could not be read as a loadout for this class."""


class TalentEncodeError(ValueError):
    """The loadout could not be written as a hash.

    Separate from ``TalentDecodeError`` because the two mean opposite things about
    where the fault is: a decode error is a statement about a string somebody else
    wrote, an encode error is a statement about a ``Loadout`` this code assembled.
    """


@dataclass(frozen=True)
class Trait:
    """One entry of one talent node, straight out of ``trait_data.inc``."""

    tree_index: int
    class_id: int
    entry_id: int
    node_id: int
    max_ranks: int
    req_points: int
    spell_id: int
    row: int
    col: int
    selection_index: int
    name: str
    spec_ids: tuple[int, ...]
    sub_tree: int
    node_type: int

    @property
    def is_player_trait(self) -> bool:
        return self.tree_index < TREE_MAX


def parse_trait_data(simc_dir: Path, ptr: bool = False) -> list[Trait]:
    """Read simc's generated trait table.

    Parsed rather than queried for the same reason ``discover_items`` parses
    ``item_data.inc``: there is no simc command that lists traits. Fields are read
    positionally against ``struct trait_data_t`` in ``engine/dbc/trait_data.hpp``.
    """
    path = _generated(simc_dir, "trait_data", ptr)
    found: list[Trait] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        row = _ROW.match(line)
        if not row:
            continue
        body = row.group(1)
        named = _NAME.search(body)
        if not named:
            continue

        head = [part.strip() for part in body[: named.start()].split(",") if part.strip()]
        tail_raw = body[named.end() :]
        arrays = re.findall(r"\{([^}]*)\}", tail_raw)
        tail = [
            part.strip()
            for part in _ARRAY.sub("A", tail_raw).split(",")
            if part.strip() and part.strip() != "A"
        ]
        if len(head) < 13 or len(tail) < 2:
            continue
        try:
            found.append(
                Trait(
                    tree_index=int(head[0]),
                    class_id=int(head[1]),
                    entry_id=int(head[2]),
                    node_id=int(head[3]),
                    max_ranks=int(head[4]),
                    req_points=int(head[5]),
                    spell_id=int(head[7]),
                    row=int(head[10]),
                    col=int(head[11]),
                    selection_index=int(head[12]),
                    name=named.group(1),
                    # Zeros are the array's padding, not a spec. Dropping them here
                    # is what lets "no spec restriction" be the empty tuple.
                    spec_ids=tuple(
                        value
                        for value in (
                            int(x) for x in (arrays[0].split(",") if arrays else []) if x.strip()
                        )
                        if value
                    ),
                    sub_tree=int(tail[-2]),
                    node_type=int(tail[-1]),
                )
            )
        except (ValueError, IndexError):
            continue
    return found


@dataclass(frozen=True)
class SubTree:
    """One hero talent tree, as simc's own data names it."""

    sub_tree: int
    name: str
    class_id: int


def _generated(simc_dir: Path, stem: str, ptr: bool) -> Path:
    name = f"{stem}_ptr.inc" if ptr else f"{stem}.inc"
    return simc_dir / "engine" / "dbc" / "generated" / name


def parse_sub_tree_names(simc_dir: Path, ptr: bool = False) -> dict[int, SubTree]:
    """Every hero tree's canonical name, keyed by sub-tree id.

    **This table did not exist when the rest of this module was written**, and the
    absence was recorded in three places as a fact: ``trait_data.inc`` stores the
    SELECTION rows with the literal name ``"0"``, so a tree could only be named by a
    build that played it, and a tree nobody played had no name anywhere. simc now
    ships ``__trait_sub_tree_data`` -- ``{id, "name", class id}`` for all 41 trees --
    in the same generated file, so the name is derived like everything else here.

    Measured on simc ``22b442e`` (2026-08-21): 41 rows in each of the live and PTR
    files, one per tree the trait table places, including the sixteen no build in any
    tier plays.

    **The PTR file names the array ``__ptr_trait_sub_tree_data``**, and the first
    version of this anchored on the live name, so it found nothing in PTR mode -- the
    mode the current tier runs in (``manifest.simc.ptr`` is true on simc's Midnight
    branch). It then fell back to the live table and logged that PTR shipped no such
    table, which was simply false. That did no damage on 22b442e, because the two
    tables' rows are byte-identical there -- checked; only the array name and the
    build number in the comment above it differ, 12.1.0.69382 against .69404 -- and it
    would have done real damage the moment they diverged: a PTR-only tree id comes
    back unnamed, which is the "Default" regression this module exists to prevent, or
    a renamed tree publishes the stale live name. Matching the shared suffix is what
    ``parse_spec_list`` already does for ``class_spec_id``, and it reads either file
    without a branch and without a fallback.

    A file without the table returns an empty map rather than raising, so an older
    checkout degrades to the previous behaviour instead of failing the run.
    """
    text = _generated(simc_dir, "trait_data", ptr).read_text(encoding="utf-8", errors="replace")
    table = _SUB_TREE_TABLE.search(text)
    if not table:
        return {}
    found: dict[int, SubTree] = {}
    for raw_id, name, raw_class in _SUB_TREE_ROW.findall(table.group(1)):
        found[int(raw_id)] = SubTree(sub_tree=int(raw_id), name=name, class_id=int(raw_class))
    return found


class _BitReader:
    """Blizzard's bit stream: 6 bits per character, least significant bit first.

    Reading past the end yields zeros rather than raising, which is what simc does
    (``byte = 0``) and is load-bearing: a loadout that selects nothing in the tail of
    the tree simply stops early, and the remaining nodes are correctly unselected.
    """

    def __init__(self, text: str) -> None:
        bad = next((c for c in text if c not in _INDEX), None)
        if bad is not None:
            raise TalentDecodeError(f"invalid character {bad!r} in loadout string")
        self.text = text
        self.head = 0

    def read(self, bits: int) -> int:
        value = 0
        for offset in range(bits):
            position, bit = divmod(self.head, CHAR_BITS)
            char = _INDEX[self.text[position]] if position < len(self.text) else 0
            value += ((char >> bit) & 1) << offset
            self.head += 1
        return value

    @property
    def spare_bits(self) -> int:
        return len(self.text) * CHAR_BITS - self.head


@dataclass(frozen=True)
class Selection:
    """One node the loadout takes, and how many ranks it puts in it."""

    node_id: int
    entry_id: int
    name: str
    spell_id: int
    rank: int
    tree_index: int
    sub_tree: int
    row: int
    col: int
    node_type: int
    max_ranks: int

    #: Whether the *purchased* bit was set. A node the game **grants** carries
    #: ``selected`` without ``purchased`` and sits at one rank, which is a different
    #: wire record from a node bought up to rank one. 277 of the 6,422 selected
    #: records in the 85 shipped MID1+MID2 profiles are granted ones, so this is not a
    #: corner case, and deriving the bit from ``rank == 1`` would be wrong for every
    #: granted node whose ``max_ranks`` is greater than one.
    purchased: bool = True

    #: Whether the *partially ranked* bit was set, or ``None`` to derive it as
    #: ``rank < max_ranks`` -- which is what simc's own exporter does
    #: (``rank == max_rank`` -> 0, else 1 plus the rank, ``player.cpp`` at 69a46e1).
    #: ``None`` is the right default for anything built by hand or by a mutation:
    #: the bit then always agrees with the rank. It is recorded explicitly by
    #: ``decode_loadout`` so that a string whose bit *disagrees* with its rank still
    #: re-encodes byte-identically -- one MID1 profile is in exactly that state, and
    #: simc refuses it ("Partial rank for node N but all N ranks are allocated.").
    partial: bool | None = None

    #: The choice index if the *choice* bit was written, otherwise ``None``. Not
    #: derivable from ``node_type``: 78 records in MID1's rotted profiles are choice
    #: nodes written **without** the bit and 89 are plain nodes written **with** it,
    #: so the encoder has to be told rather than to infer.
    choice_index: int | None = None


@dataclass(frozen=True)
class Framing:
    """How the source string was framed around the node stream.

    The loadout format has no length field: the node stream simply ends, and whatever
    is left over is padding to a 6-bit character boundary. That leaves two ways for a
    real string to differ from the shortest one that carries the same loadout, and
    **both occur in simc's own profiles**, measured over all 85 MID1+MID2 hashes on
    simc 69a46e1:

    * **Longer.** Three MID2 profiles -- all three Hunters -- carry six to nine spare
      bits where padding needs at most five, i.e. one whole extra character of zeros.
      The exporter wrote node records simc's trait table does not have.
    * **Shorter.** One MID1 profile (Shadow Priest, Archon) ends one bit *before* the
      node stream does, relying on the reader returning zeros past the end -- which
      both simc and ``_BitReader`` do.

    Neither changes what the string means, so neither is an error. But reproducing a
    source byte for byte needs them, which is why the decoder records the framing and
    the encoder replays it. ``length`` is the source's character count and ``tail``
    the bits that followed the node stream (empty when the stream overran the string).
    """

    length: int
    tail: tuple[int, ...] = ()


@dataclass(frozen=True)
class Loadout:
    """A decoded talent string."""

    version: int
    spec_id: int
    selections: tuple[Selection, ...]
    spare_bits: int

    #: How the source string was framed, when this loadout came from one. ``None`` for
    #: a loadout assembled in code, which then encodes to its own shortest form.
    framing: Framing | None = None

    @property
    def sub_tree(self) -> int | None:
        """The hero tree, from the SELECTION node -- the authoritative answer.

        A hero *node* can be shared by two hero trees, so the hero-tree nodes alone do
        not name one. The selection node does, and it agreed with ``herotrees.py`` on
        all 26 MID2 builds.
        """
        chosen = {s.sub_tree for s in self.selections if s.tree_index == TREE_SELECTION}
        return next(iter(chosen)) if len(chosen) == 1 else None

    def in_tree(self, tree_index: int) -> tuple[Selection, ...]:
        """Selections in one tree, with hero nodes narrowed to the chosen sub-tree."""
        picked = tuple(s for s in self.selections if s.tree_index == tree_index)
        if tree_index != TREE_HERO:
            return picked
        chosen = self.sub_tree
        return tuple(s for s in picked if chosen is None or s.sub_tree == chosen)

    def points(self, tree_index: int) -> int:
        return sum(s.rank for s in self.in_tree(tree_index))


def nodes_for_class(traits: list[Trait], class_id: int) -> dict[int, list[Trait]]:
    """Every player node of one class, grouped by node id.

    This is ``generate_tree_nodes`` in ``player.cpp``: all trees below ``MAX``, keyed
    by node id in a ``std::map`` -- so ascending node id is the stream order, and
    entries inside a node keep the order the data file lists them in, which is what a
    choice index selects against.
    """
    grouped: dict[int, list[Trait]] = {}
    for trait in traits:
        if trait.class_id != class_id or not trait.is_player_trait:
            continue
        grouped.setdefault(trait.node_id, []).append(trait)
    return grouped


def max_ranks_of(entries: list[Trait]) -> int:
    """How many ranks a node holds, which is not always its first entry's ``max_ranks``.

    A **tiered** node spreads its ranks over several entries and simc sums them
    (``range::accumulate`` in both ``parse_traits_hash`` and ``generate_traits_hash``);
    every other kind of node takes the figure off its first entry. Getting this wrong
    moves the *partially ranked* bit, which moves six bits of rank into or out of the
    stream and desynchronises everything after it.
    """
    first = entries[0]
    if first.node_type == NODE_TIERED:
        return sum(entry.max_ranks for entry in entries)
    return first.max_ranks


def decode_loadout(loadout: str, nodes: dict[int, list[Trait]]) -> Loadout:
    """Read a talent loadout string against one class's nodes.

    A transcription of ``parse_traits_hash``. simc's validation errors are *not*
    reproduced -- the point here is to read what the string says, and a profile whose
    string disagrees with the tree is a finding for a human, exactly as it is for a
    fight profile.
    """
    reader = _BitReader(loadout)
    version = reader.read(VERSION_BITS)
    if version != LOADOUT_VERSION:
        raise TalentDecodeError(
            f"loadout serialization version {version}, expected {LOADOUT_VERSION}"
        )
    spec_id = reader.read(SPEC_BITS)
    reader.read(TREE_BITS)  # tree hash; simc skips it too

    selections: list[Selection] = []
    for node_id in sorted(nodes):
        entries = nodes[node_id]
        if not reader.read(1):  # selected
            continue
        trait = entries[0]
        max_rank = max_ranks_of(entries)
        rank = max_rank
        purchased = bool(reader.read(1))  # otherwise granted at rank 1
        partial: bool | None = None
        choice_index: int | None = None
        if not purchased:
            rank = 1
        else:
            partial = bool(reader.read(1))  # partially ranked
            if partial:
                rank = reader.read(RANK_BITS)
            if reader.read(1):  # choice node
                choice_index = reader.read(CHOICE_BITS)
                if choice_index >= len(entries):
                    raise TalentDecodeError(
                        f"choice index {choice_index} out of bounds for node {node_id} "
                        f"({len(entries)} entries)"
                    )
                trait = entries[choice_index]
        selections.append(
            Selection(
                node_id=node_id,
                entry_id=trait.entry_id,
                name=trait.name,
                spell_id=trait.spell_id,
                rank=rank,
                tree_index=trait.tree_index,
                sub_tree=trait.sub_tree,
                row=trait.row,
                col=trait.col,
                node_type=trait.node_type,
                max_ranks=max_rank,
                purchased=purchased,
                partial=partial,
                choice_index=choice_index,
            )
        )
    # Capture the framing before draining the tail: `spare_bits` is negative when the
    # stream overran the string, and reading that many bits would be meaningless.
    spare = reader.spare_bits
    tail = tuple(reader.read(1) for _ in range(spare)) if spare > 0 else ()
    return Loadout(
        version=version,
        spec_id=spec_id,
        selections=tuple(selections),
        spare_bits=spare,
        framing=Framing(length=len(loadout), tail=tail),
    )


class _BitWriter:
    """The exact mirror of ``_BitReader``: 6 bits per character, least significant
    bit first, padded with zeros to a character boundary.

    Kept as a separate class rather than folded into ``encode_loadout`` because the
    padding rule is the subtle part: the last character of a loadout string is almost
    never full, and a writer that padded on the *wrong* side would produce a string
    that reads back as a different build rather than as an error.
    """

    def __init__(self) -> None:
        self.bits: list[int] = []

    def write(self, value: int, bits: int) -> None:
        if value < 0 or value >> bits:
            raise TalentEncodeError(f"value {value} does not fit in {bits} bits")
        for offset in range(bits):
            self.bits.append((value >> offset) & 1)

    def text(self, bits: list[int] | None = None) -> str:
        raw = self.bits if bits is None else bits
        padded = raw + [0] * (-len(raw) % CHAR_BITS)
        out = []
        for start in range(0, len(padded), CHAR_BITS):
            char = 0
            for offset in range(CHAR_BITS):
                char |= padded[start + offset] << offset
            out.append(BASE64[char])
        return "".join(out)


def encode_loadout(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    *,
    preserve_framing: bool = True,
) -> str:
    """Write a loadout back out as a talent string: the inverse of ``decode_loadout``.

    This is what makes a talent *search* possible at all. Everything upstream of it
    reads builds simc already ships; a build nobody has written down has to be
    assembled here and handed to simc as a hash, because a hash is the only way to put
    talents into a profile.

    The format is written from ``generate_traits_hash`` in ``engine/player/player.cpp``
    -- simc's own exporter, which is the authority on the two rules that are not
    visible from the reader alone:

    * the 128-bit tree hash is written as **zeros**, with simc's own comment saying it
      is "0-filled to bypass validation, as GetTreeHash() is unavailable externally".
      Measured to be zero in all 85 shipped MID1+MID2 profiles, so nothing is lost by
      writing zeros;
    * the choice bit covers ``NODE_CHOICE`` **and** ``NODE_SELECTION`` -- the hero-tree
      selection node is written as a choice node. Reading only ``NODE_CHOICE`` produces
      a string whose hero tree silently reverts to the node's first entry.

    Everything else is taken from the ``Selection`` rather than re-derived, which is
    what makes the round trip exact: see ``purchased``, ``partial`` and
    ``choice_index`` there for the measurements behind each.

    ``preserve_framing`` replays the source string's own length and tail (see
    ``Framing``). It is on by default because the common case is "decode, change one
    thing, encode", where reproducing the source's framing is what makes an *unchanged*
    build come back byte-identical -- the property the whole round-trip test rests on.
    Turning it off yields the shortest string carrying the same loadout, which simc
    reads identically.

    Raises ``TalentEncodeError`` for a loadout that cannot be written at all: two
    selections of one node, a node the class does not have, a value too large for its
    field, a choice index past the node's last entry, or a **granted** node carrying a
    rank or a choice index the format has nowhere to put. Those are all silent
    corruptions if written anyway -- an over-large rank would simply lose its high bits
    and encode as a different build, and a granted node's discarded choice index
    encodes as *the build you started from*, which is worse: the hash is valid, simc
    runs it, and the number comes back attributed to a variant that was never built.
    """
    writer = _BitWriter()
    writer.write(loadout.version, VERSION_BITS)
    writer.write(loadout.spec_id, SPEC_BITS)
    writer.write(0, TREE_BITS)

    chosen: dict[int, Selection] = {}
    for selection in loadout.selections:
        if selection.node_id in chosen:
            raise TalentEncodeError(f"node {selection.node_id} is selected twice")
        if selection.node_id not in nodes:
            raise TalentEncodeError(f"node {selection.node_id} is not a node of this class")
        chosen[selection.node_id] = selection

    for node_id in sorted(nodes):
        selection = chosen.get(node_id)
        if selection is None:
            writer.write(0, 1)
            continue
        writer.write(1, 1)
        writer.write(1 if selection.purchased else 0, 1)
        if not selection.purchased:
            # A granted node's record ends at the purchased bit: the format writes it
            # no rank and no choice index, and simc's reader gives it the node's FIRST
            # entry at one rank. So anything else this selection claims is not merely
            # lost, it is lost *silently* -- the string comes back byte-identical to
            # the one the unchanged build produces, and a search then attributes the
            # base build's DPS to a variant it never simulated. Refusing here is the
            # only place that can be seen: `validate_loadout` reports nothing, because
            # the hash it would judge is a perfectly legal one.
            if selection.choice_index is not None:
                raise TalentEncodeError(
                    f"node {node_id} is selected but not purchased, and the format writes no "
                    f"choice index for a granted node; index {selection.choice_index} "
                    f"(entry {selection.entry_id}) cannot be written"
                )
            if selection.rank != 1:
                raise TalentEncodeError(
                    f"node {node_id} is selected but not purchased, and the format writes no "
                    f"rank for a granted node; {selection.rank} ranks cannot be written"
                )
            continue  # granted nodes stop here; the game gave them at one rank

        max_rank = max_ranks_of(nodes[node_id])
        partial = selection.partial
        if partial is None:
            partial = selection.rank < max_rank
        writer.write(1 if partial else 0, 1)
        if partial:
            writer.write(selection.rank, RANK_BITS)

        writer.write(1 if selection.choice_index is not None else 0, 1)
        if selection.choice_index is not None:
            # The same bound `decode_loadout` enforces on the way in. Two bits of
            # field width is not the only limit: an index inside the field but past
            # the node's last entry writes a string simc refuses ("Index 2 for choice
            # node 12 out of bounds.") and this decoder raises on, so the error
            # belongs here, where the loadout that caused it is still in hand.
            if selection.choice_index >= len(nodes[node_id]):
                raise TalentEncodeError(
                    f"choice index {selection.choice_index} out of bounds for node {node_id} "
                    f"({len(nodes[node_id])} entries)"
                )
            writer.write(selection.choice_index, CHOICE_BITS)

    return writer.text(_framed(writer.bits, loadout.framing if preserve_framing else None))


def _framed(bits: list[int], framing: Framing | None) -> list[int]:
    """Replay a source string's framing around a freshly written node stream.

    One rule covers both directions, because both are just "make the stream as long as
    the source was, when that can be done without changing what it says":

    * append the tail the source carried past its node stream, then
    * pad with zeros if still short of the source's length;
    * if instead the stream now needs *more* room than the source had, give it the room
      -- unless everything past the source's length is zero, in which case trim back to
      the source's length.

    The trim is what reproduces a source that stopped early, and it is guarded on the
    trimmed bits being zero so that a mutation which selects a node near the end of the
    tree can never be silently truncated into a different build.
    """
    if framing is None:
        return bits
    target = framing.length * CHAR_BITS
    framed = bits + list(framing.tail)
    if len(framed) < target:
        return framed + [0] * (target - len(framed))
    if len(framed) > target and not any(framed[target:]):
        return framed[:target]
    return framed


def spec_rule_violation(loadout: Loadout, nodes: dict[int, list[Trait]]) -> str | None:
    """simc's own refusal of a decoded loadout, in simc's own words, or None.

    ``parse_traits_hash`` rejects a **non-hero** node whose ``id_spec`` does not
    contain the player's spec, and says so as *"Selected node N entry M is not
    available to player's spec"*. That is one of the two wordings a stale talent hash
    produces; the other -- *"Node N is not a choice node but has index selection"* --
    is a decode failure and comes out of ``decode_loadout`` as a
    ``TalentDecodeError`` naming the same node.

    Together those two are enough to say **which** of simc's shipped profiles will
    not load, and why, **without running simc**. Checked against simc's own CI output
    for 2026-08-22: it names node 91020 for Havoc Aldrachi Reaver and node 110203
    entry 136735 for Arms Warrior, and this reproduces both ids exactly. The control
    is that all 35 shipped MID2 profiles pass, so it is not simply refusing
    everything. ``wowdps check-profiles`` remains the version that asks simc itself;
    this is the version a run without a binary can afford.
    """
    for selection in loadout.selections:
        if selection.tree_index in (TREE_HERO, TREE_SELECTION):
            continue
        entry = next(
            (
                candidate
                for candidate in nodes.get(selection.node_id, [])
                if candidate.entry_id == selection.entry_id
            ),
            None,
        )
        if entry is None or not entry.spec_ids:
            continue
        if loadout.spec_id not in entry.spec_ids:
            return (
                f"Selected node {selection.node_id} entry {selection.entry_id} "
                f"is not available to player's spec"
            )
    return None


def tree_layout(nodes: dict[int, list[Trait]], spec_id: int, sub_tree: int | None) -> list[dict]:
    """Every node a reader should see for one spec, selected or not.

    A talent view that draws only the taken nodes is a list, not a tree: the shape of
    what was *passed over* is most of the information. So the layout is the whole grid
    and the selection is an overlay on it.

    Nodes are filtered the way the game filters them -- a spec sees the class tree, its
    own spec tree, and the one hero tree it plays -- because showing another spec's
    branch would be drawing a tree nobody can take.
    """
    layout: list[dict] = []
    for node_id in sorted(nodes):
        entries = nodes[node_id]
        first = entries[0]
        if first.tree_index in (TREE_INVALID, TREE_SELECTION):
            continue
        if first.tree_index == TREE_HERO:
            if sub_tree is None or all(entry.sub_tree != sub_tree for entry in entries):
                continue
            entries = [entry for entry in entries if entry.sub_tree == sub_tree]
            first = entries[0]
        else:
            eligible = [
                entry for entry in entries if not any(entry.spec_ids) or spec_id in entry.spec_ids
            ]
            if not eligible:
                continue
            entries, first = eligible, eligible[0]

        layout.append(
            {
                "id": node_id,
                "tree": first.tree_index,
                "row": first.row,
                "col": first.col,
                "type": first.node_type,
                "maxRanks": (
                    sum(entry.max_ranks for entry in entries)
                    if first.node_type == NODE_TIERED
                    else first.max_ranks
                ),
                "entries": [
                    {"id": entry.entry_id, "name": entry.name, "spellId": entry.spell_id}
                    for entry in entries
                ],
            }
        )
    return layout


# --------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------

#: Below this many points in the class tree, a build is flagged. Twenty-four of MID2's
#: twenty-six spend 35 or 36; the two that do not spend 10. Anything under twenty is
#: not a build somebody assembled, it is a profile that never finished being written.
_THIN_CLASS_TREE = 20


def build_document(tier: str, builds: list[dict], traits: list[Trait]) -> dict:
    """Layouts keyed by spec, selections keyed by build.

    Two builds of one spec share a tree and differ only in what they take, so the
    layout is published once per (spec, hero tree) and the builds reference it. That
    is also the join the view needs: it draws one grid and paints two overlays.
    """
    trees: dict[str, Any] = {}
    published: list[dict] = []
    notes: list[str] = []

    for build in builds:
        class_id = CLASS_IDS.get(build["class"])
        loadout_str = build.get("talentHash")
        if not class_id or not loadout_str:
            continue
        nodes = nodes_for_class(traits, class_id)
        try:
            loadout = decode_loadout(loadout_str, nodes)
        except TalentDecodeError as exc:
            notes.append(f"{build['displayName']}: {exc}")
            continue

        sub_tree = loadout.sub_tree
        key = f"{loadout.spec_id}-{sub_tree if sub_tree is not None else 'none'}"
        if key not in trees:
            trees[key] = {
                "specId": loadout.spec_id,
                "subTree": sub_tree,
                "nodes": tree_layout(nodes, loadout.spec_id, sub_tree),
            }

        points = {
            "class": loadout.points(TREE_CLASS),
            "spec": loadout.points(TREE_SPEC),
            "hero": loadout.points(TREE_HERO),
        }
        caveat = None
        if points["class"] < _THIN_CLASS_TREE:
            caveat = (
                f"simc's profile for this build spends only {points['class']} points in the "
                f"class tree, where every other build in the tier spends about 35. The "
                f"decode is sound -- the specialisation tree and the hero tree are both "
                f"normal -- so this is simc's shipped talent string, and it would "
                f"understate the build."
            )
            notes.append(f"{build['displayName']}: thin class tree ({points['class']} points)")

        published.append(
            {
                "specId": build["id"],
                "displayName": build["displayName"],
                "tree": key,
                "heroTalent": build.get("heroTalent"),
                "points": points,
                "caveat": caveat,
                "selected": [
                    {"id": s.node_id, "entry": s.entry_id, "rank": s.rank}
                    for s in (
                        loadout.in_tree(TREE_CLASS)
                        + loadout.in_tree(TREE_SPEC)
                        + loadout.in_tree(TREE_HERO)
                    )
                ],
            }
        )

    return {
        "schemaVersion": 1,
        "tier": tier,
        "note": (
            "Decoded from the loadout string in simc's own profiles, against simc's own "
            "trait table. No external service is involved. Node icons are drawn by "
            "Wowhead's tooltip script from the spell id; connector lines are not shown "
            "because simc ships no edge data and guessing one would be a claim about the "
            "tree."
        ),
        "trees": trees,
        "builds": published,
        "notes": notes,
    }


def write_talent_trees(document: dict, out_dir: Path) -> Path:
    """Write the tier's talent trees, keeping the file stable when nothing moved."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "talent-trees.json"
    text = json.dumps(document, separators=(",", ":")) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return path
    path.write_text(text, encoding="utf-8")
    return path
