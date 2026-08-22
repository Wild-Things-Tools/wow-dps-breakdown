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
import logging
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
#: ``__trait_sub_tree_data`` rows: ``{ <sub tree id>, "<name>", <class id> }``.
_SUB_TREE_TABLE = re.compile(r"__trait_sub_tree_data\s*\{\s*\{(.*?)\}\s*\}\s*;", re.S)
_SUB_TREE_ROW = re.compile(r'\{\s*(\d+)\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*(\d+)\s*\}')
_NAME = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ARRAY = re.compile(r"\{[^}]*\}")


class TalentDecodeError(ValueError):
    """The hash could not be read as a loadout for this class."""


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

    Measured on simc ``22b442e`` (2026-08-21): 41 rows, one per tree the trait table
    places, including the sixteen no build in any tier plays.

    **The PTR table does not carry it**, and this project reads the PTR trait table
    for the current tier (``manifest.simc.ptr`` is true on simc's Midnight branch).
    ``trait_data_ptr.inc`` has no ``__trait_sub_tree_data`` at all -- checked on the
    same revision -- so asking for PTR names and taking the empty answer would leave
    every tree unnamed for exactly the tier that matters. A *name* is not a tree
    layout: the ids are the same ids, so falling back to the live table names them
    correctly, and the fallback says so rather than happening quietly. That is
    narrower than the standing rule about never reading the wrong trait table, which
    is about the node stream, and nothing here reads nodes.

    A file without the table in either place returns an empty map rather than
    raising, so an older checkout degrades to the previous behaviour instead of
    failing the run.
    """
    text = _generated(simc_dir, "trait_data", ptr).read_text(encoding="utf-8", errors="replace")
    table = _SUB_TREE_TABLE.search(text)
    if not table and ptr:
        logging.getLogger(__name__).info(
            "trait_data_ptr.inc ships no hero tree name table; reading the live one for names only"
        )
        text = _generated(simc_dir, "trait_data", False).read_text(
            encoding="utf-8", errors="replace"
        )
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


@dataclass(frozen=True)
class Loadout:
    """A decoded talent string."""

    version: int
    spec_id: int
    selections: tuple[Selection, ...]
    spare_bits: int

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
        max_rank = (
            sum(entry.max_ranks for entry in entries)
            if trait.node_type == NODE_TIERED
            else trait.max_ranks
        )
        rank = max_rank
        if not reader.read(1):  # purchased; otherwise granted at rank 1
            rank = 1
        else:
            if reader.read(1):  # partially ranked
                rank = reader.read(RANK_BITS)
            if reader.read(1):  # choice node
                index = reader.read(CHOICE_BITS)
                if index >= len(entries):
                    raise TalentDecodeError(
                        f"choice index {index} out of bounds for node {node_id} "
                        f"({len(entries)} entries)"
                    )
                trait = entries[index]
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
            )
        )
    return Loadout(
        version=version,
        spec_id=spec_id,
        selections=tuple(selections),
        spare_bits=reader.spare_bits,
    )


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
