"""Changing a talent build, and deciding offline whether the result is legal.

``talenttree.py`` reads and writes the format. This module is the layer above it: the
handful of edits a talent search actually makes, and the check that says whether an
edited build is one a player could have.

Why a validator exists at all
-----------------------------
**simc checks almost nothing.** Read against ``parse_traits_hash`` in
``engine/player/player.cpp`` (simc 69a46e1, checked 2026-08-23), the whole of its
validation is: the base64 alphabet, the string being long enough for a header, the
serialization version, the specialization id, a node's spec rule, the rank fitting
under ``max_ranks``, a partial rank not *being* ``max_ranks``, a multi-entry non-tiered
node not carrying a partial rank, the choice bit appearing only on choice and selection
nodes, and the choice index being in bounds.

That list has no unlock edges in it, no point gates and no point budget. simc will
load, and cheerfully simulate for ten minutes, a build that spends sixty points in a
tree, takes a capstone with nothing leading to it, and skips the gates entirely. The
number that comes back is a real simulation of a character that cannot exist.

So there are three separate questions, and this module keeps them apart:

* **Can it be written at all?** -- ``Finding.unencodable``. Some loadouts have no
  hash: a rank that does not fit six bits, a choice index past the node's last entry,
  a node of another class. ``encode_loadout`` raises on those, so simc never sees them
  and no simc wording exists to predict -- the message is this project's own.
* **Will simc take it?** -- ``Finding.simc_refuses``. A search should never spend a
  simulation on a hash simc is going to reject, and the rejection is decidable here,
  offline, in simc's own words.
* **Is it a legal build?** -- everything else. This is where game legality lives,
  because nothing downstream will ever ask.

The first two are not exclusive: an out-of-bounds choice index is both a string this
encoder refuses to write and one simc refuses to read.

What can and cannot be checked
------------------------------
``req_points`` -- the gate a node sits behind -- **is** in simc's data: column 6 of
``trait_data.inc``, parsed into ``Trait.req_points`` since the tree view was built and
never read until now. Measured over the 35 shipped MID2 profiles: 1,614 selections sit
behind a non-zero gate and exactly **2** violate it, both of them ``Rune Mastery``
(gate 23) in the two Frost Death Knight profiles that spend **10** class points. Those
are the same two builds ``talenttree._THIN_CLASS_TREE`` already flags by a completely
different route, so the gate check reproduces a known finding rather than inventing
one, which is the strongest control available for it offline.

**Unlock edges are not in simc's data** -- checked on simc ``69a46e1``, 2026-08-23:
``engine/dbc/generated/`` ships three trait arrays (``__trait_data_data``,
``__trait_definition_effect_data``, ``__trait_sub_tree_data``) and no edge table, and
``trait_data_t`` carries no edge field. That is why ``tree_layout`` draws the grid
without connector lines. The date belongs to the claim: simc's *extraction* toolchain
knows the DB2 table (``dbc_extract3/formats/12.1.0.68209.json`` declares ``TraitEdge``
with ``id_left_trait_node``/``id_right_trait_node``) and simply does not generate it, so
this is a shipping decision that can change without notice -- which is precisely how
``trait_sub_tree_data`` appeared, after this project had recorded its absence as
permanent in three places.

The edges exist in Blizzard's
talent-tree API as a per-node ``unlocks`` list, and the sibling ``wtt-backend``
repository stores them (``TalentTree.tree_data``, trimmed by
``apps/bnetapi/services/game_data/talent_tree.py``). They are **not wired in here**,
and the reason is measured rather than assumed. The only copy reachable as a *file*
rather than a live API call is
``wtt-backend/docs/api_structure/game_data_api/talent_api/talent_tree_nodes.json``:
one tree (id 786, Shaman) of thirteen classes, 209 nodes of which 30 carry an
``unlocks`` list, captured 2026-01-14 -- seven months before this tier. Its node ids
*do* join to simc's (183 of its 209 are in simc's player-node set, so the join key is
sound and this is a data-coverage problem, not a schema one), and everything else is in
a live database behind Battle.net credentials.

Adding a network call to this module would put the talent view behind credentials,
which is the exact dependency ``talenttree``'s docstring exists to refuse. So the
interface is defined and shipped **unpopulated**, and ``validate_loadout`` reports
edges as *unchecked* rather than passing them silently. A validator that cannot check
edges must say so: "no findings" and "nothing to find" are different answers, and only
one of them is honest about an unreachable capstone.

To populate it, hand ``UnlockEdges`` a mapping of node id to the nodes it unlocks, from
any source that reads Blizzard's talent-tree endpoint, and record where it came from in
``source``. Nothing else changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from .talenttree import (
    CHOICE_BITS,
    LOADOUT_VERSION,
    NODE_CHOICE,
    NODE_SELECTION,
    NODE_TIERED,
    RANK_BITS,
    TREE_CLASS,
    TREE_HERO,
    TREE_SELECTION,
    TREE_SPEC,
    VERSION_BITS,
    Loadout,
    Selection,
    Trait,
    max_ranks_of,
)

#: The trees a build spends points in, in the order a reader thinks about them.
POINT_TREES = (TREE_CLASS, TREE_SPEC, TREE_HERO)

_TREE_NAMES = {TREE_CLASS: "class", TREE_SPEC: "specialisation", TREE_HERO: "hero"}


class TalentEditError(ValueError):
    """A mutation was asked for that the tree cannot express."""


# --------------------------------------------------------------------------------
# Mutation primitives
# --------------------------------------------------------------------------------


def _entries(nodes: dict[int, list[Trait]], node_id: int) -> list[Trait]:
    entries = nodes.get(node_id)
    if not entries:
        raise TalentEditError(f"node {node_id} is not a node of this class")
    return entries


def _selection(
    nodes: dict[int, list[Trait]],
    node_id: int,
    *,
    rank: int,
    purchased: bool = True,
    choice_index: int | None = None,
) -> Selection:
    """Build a selection from the tree, so its name, entry and rank ceiling agree.

    Every mutation goes through this rather than through ``dataclasses.replace`` on an
    existing selection, because a rank change also moves the *partially ranked* bit and
    a choice change also moves the entry id, the name and the spell id. Replacing one
    field and keeping the rest is how a mutated build ends up describing one talent and
    encoding another.

    Which is also why a **granted** node is refused an entry or a rank here. A node the
    game grants is written as *selected, not purchased*, and the format stops the record
    there: no rank, no choice index, and simc reads the node's first entry at one rank.
    So a granted node flipped to its other entry describes one talent and encodes the
    other -- and the string it encodes to is the *unmutated build's own hash*, byte for
    byte, which no downstream check can see. Purchasing the node is the expressible
    edit, and ``select_node`` is the way to make it.
    """
    entries = _entries(nodes, node_id)
    if not purchased and choice_index is not None:
        raise TalentEditError(
            f"node {node_id} is granted rather than purchased, and the format writes no "
            f"choice index for a granted node; purchase it to choose an entry"
        )
    if not purchased and rank != 1:
        raise TalentEditError(
            f"node {node_id} is granted rather than purchased, so it holds exactly one "
            f"rank; {rank} cannot be written"
        )
    trait = entries[0]
    if choice_index is not None:
        if not 0 <= choice_index < len(entries):
            raise TalentEditError(
                f"choice index {choice_index} out of bounds for node {node_id} "
                f"({len(entries)} entries)"
            )
        trait = entries[choice_index]
    max_rank = max_ranks_of(entries)
    if rank < 1 or rank > max_rank:
        raise TalentEditError(f"rank {rank} out of range for node {node_id} (max {max_rank})")
    return Selection(
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
        # Deliberately left to derive from the rank. A decoded selection carries the
        # bit the *source string* had; a mutated one must carry the bit its own rank
        # implies, or a node dropped to a partial rank still encodes as full.
        partial=None,
        choice_index=choice_index,
    )


def _with(loadout: Loadout, selections: Iterable[Selection]) -> Loadout:
    """The mutated loadout, with the donor's *description of its own string* dropped.

    ``framing`` is kept deliberately -- the encoder replays it, which is what makes an
    unchanged build come back byte-identical. ``spare_bits`` is the opposite case: it
    describes where the source string's node stream ended, the mutant's stream is a
    different length, and nothing recomputes it. Carried verbatim it survived as a
    number about a string nobody wrote, negative included.
    """
    ordered = tuple(sorted(selections, key=lambda s: s.node_id))
    return replace(loadout, selections=ordered, spare_bits=None)


def selected(loadout: Loadout, node_id: int) -> Selection | None:
    return next((s for s in loadout.selections if s.node_id == node_id), None)


def select_node(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    node_id: int,
    *,
    rank: int | None = None,
    choice_index: int | None = None,
) -> Loadout:
    """Take a node, or change how many ranks it holds.

    ``rank`` defaults to the node's maximum, which is what "take this talent" means for
    every single-rank node and most others. ``choice_index`` defaults to 0 for a choice
    or selection node and to ``None`` for anything else -- writing the choice bit on a
    plain node is one of the few things simc actually refuses.
    """
    entries = _entries(nodes, node_id)
    is_choice = entries[0].node_type in (NODE_CHOICE, NODE_SELECTION)
    if choice_index is None and is_choice:
        current = selected(loadout, node_id)
        choice_index = current.choice_index if current and current.choice_index is not None else 0
    if choice_index is not None and not is_choice:
        raise TalentEditError(
            f"node {node_id} is not a choice node but was given index {choice_index}"
        )
    new = _selection(
        nodes,
        node_id,
        rank=max_ranks_of(entries) if rank is None else rank,
        choice_index=choice_index,
    )
    return _with(loadout, [s for s in loadout.selections if s.node_id != node_id] + [new])


def deselect_node(loadout: Loadout, node_id: int) -> Loadout:
    """Drop a node entirely. Dropping one that was not taken is not an error --
    a search asks for a state, not for a delta."""
    return _with(loadout, [s for s in loadout.selections if s.node_id != node_id])


def set_choice(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    node_id: int,
    choice_index: int,
) -> Loadout:
    """Flip a choice node to its other entry, keeping its rank.

    Refuses a node that is not selected: "change which half of a talent I took" has no
    meaning for a talent nobody took, and quietly selecting it would spend a point the
    caller did not ask to spend.

    Refuses a **granted** node for the same reason one step further on -- see
    ``_selection``. The format cannot say which entry of a granted node is taken, so
    the flip would encode to the hash it started from; the caller who wants the other
    entry has to purchase the node, which is a different edit and spends a point.
    """
    current = selected(loadout, node_id)
    if current is None:
        raise TalentEditError(f"node {node_id} is not selected, so it has no choice to flip")
    entries = _entries(nodes, node_id)
    if entries[0].node_type not in (NODE_CHOICE, NODE_SELECTION):
        raise TalentEditError(f"node {node_id} is not a choice node")
    new = _selection(
        nodes,
        node_id,
        rank=current.rank,
        purchased=current.purchased,
        choice_index=choice_index,
    )
    return _with(loadout, [s for s in loadout.selections if s.node_id != node_id] + [new])


def move_rank(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    *,
    source_node: int,
    target_node: int,
    ranks: int = 1,
) -> Loadout:
    """Move ranks from one node to another **in the same tree**, leaving the total alone.

    Same tree because that is what keeps the edit a single question. Moving a point
    from the class tree to the spec tree changes two budgets at once, so a search that
    did it could not attribute the result to either -- and it is not a move a player can
    make: the trees have separate point pools.

    A source emptied by the move is dropped; a target not yet taken is taken. Both are
    the same edit seen from the two ends, and making the caller special-case them is
    how an off-by-one point appears in a sweep.

    Two ways of leaving the total alone that this did not, both found by reading the
    code rather than by a failing test, and both producing a build ``validate_loadout``
    called legal:

    * **the diagonal.** ``source_node == target_node`` read the target's rank before the
      source was deselected, so the node came back holding its own rank plus the ranks
      "moved", and the tree gained them. A sweep enumerating (source, target) pairs hits
      the diagonal on every node it visits.
    * **two hero sub-trees.** The same-tree guard compared ``tree_index``, and every
      hero node of every hero tree carries ``TREE_HERO``. Moving a rank to a node of the
      tree the build does not play spends the point outside
      ``Loadout.in_tree(TREE_HERO)``, so the hero total silently *dropped* by one.

    Both are refused by name below, and the invariant is then asserted centrally by
    ``_totals_preserved`` -- see there for why the assertion is worth keeping alongside
    the named guards.
    """
    if ranks < 1:
        raise TalentEditError("a rank move must move at least one rank")
    if source_node == target_node:
        raise TalentEditError(
            f"node {source_node} cannot give ranks to itself; a rank move needs two nodes"
        )
    source = selected(loadout, source_node)
    if source is None:
        raise TalentEditError(f"node {source_node} is not selected, so it has no rank to give")
    source_tree = _entries(nodes, source_node)[0].tree_index
    target_tree = _entries(nodes, target_node)[0].tree_index
    if source_tree != target_tree:
        raise TalentEditError(
            f"node {source_node} is in tree {source_tree} and node {target_node} is in tree "
            f"{target_tree}; a rank move must stay inside one tree"
        )
    if source.rank < ranks:
        raise TalentEditError(
            f"node {source_node} holds {source.rank} rank(s), cannot give {ranks} away"
        )

    target = selected(loadout, target_node)
    if source_tree == TREE_HERO:
        landing = _landing_entry(nodes, target_node, target)
        if landing.sub_tree != source.sub_tree:
            raise TalentEditError(
                f"node {source_node} is in hero sub tree {source.sub_tree} and node "
                f"{target_node} in sub tree {landing.sub_tree}; a build plays one hero tree, "
                f"so a rank moved across the two leaves the one it plays"
            )

    target_rank = (target.rank if target else 0) + ranks
    moved = deselect_node(loadout, source_node)
    if source.rank > ranks:
        moved = select_node(moved, nodes, source_node, rank=source.rank - ranks)
    moved = select_node(
        moved,
        nodes,
        target_node,
        rank=target_rank,
        choice_index=target.choice_index if target else None,
    )
    return _totals_preserved(
        loadout,
        moved,
        what=f"moving {ranks} rank(s) from node {source_node} to node {target_node}",
    )


def _landing_entry(nodes: dict[int, list[Trait]], node_id: int, current: Selection | None) -> Trait:
    """The entry ``select_node`` will land this node on, which is what a hero move has
    to judge: a hero *node* can belong to two sub-trees, so the node id alone does not
    say which tree a rank ends up in."""
    entries = _entries(nodes, node_id)
    index = current.choice_index if current and current.choice_index is not None else 0
    return entries[index] if index < len(entries) else entries[0]


def _totals_preserved(before: Loadout, after: Loadout, *, what: str) -> Loadout:
    """Refuse an edit that claims to move points and does not.

    The invariant asserted centrally rather than trusted per function, because both
    ways of breaking it shipped at once and neither was visible from the mutation that
    caused it -- an over-budget build and an under-budget one, both of which
    ``validate_loadout`` called legal (``derive_point_budget`` is a ceiling from the
    observed maximum, so 35 class points becoming 36 does not trip it either).

    A named guard says *why* an edit is refused; this says *that* the arithmetic came
    out. Keep both: the guards are a list of the cases somebody thought of.
    """
    for tree in POINT_TREES:
        spent_before, spent_after = before.points(tree), after.points(tree)
        if spent_before != spent_after:
            raise TalentEditError(
                f"{what} took the {_TREE_NAMES[tree]} tree from {spent_before} to "
                f"{spent_after} points; a rank move leaves every tree's total alone"
            )
    return after


def selection_node_for_spec(nodes: dict[int, list[Trait]], spec_id: int) -> int:
    """The hero-tree selection node **this spec** must use.

    The measured fact this whole primitive turns on. A class has one selection node per
    specialisation, not one per class: Rogue carries 99842 (Subtlety, 261), 99843
    (Outlaw, 260) and 99844 (Assassination, 259), each with two entries naming the two
    hero trees that spec can play. Checked across simc's whole trait table on 69a46e1:
    **all 80 selection-node entries carry exactly one spec id**, so "the node for this
    spec" is always well defined.

    Selecting a *different* spec's selection node -- which is what transplanting a donor
    build's hero records does, because the donor is a different spec -- makes simc say
    ``Hero tree selection node 99844 entry 123375 is not for the player's spec,
    ignoring.`` and abort. That message reads like a warning and is not: ``do_error``
    in ``parse_traits_hash`` **throws**, so the ``ignoring`` branch never runs and the
    profile fails to load.
    """
    found = [
        node_id
        for node_id, entries in nodes.items()
        if entries[0].tree_index == TREE_SELECTION and spec_id in entries[0].spec_ids
    ]
    if len(found) != 1:
        raise TalentEditError(
            f"expected exactly one hero-tree selection node for spec {spec_id}, found {found}"
        )
    return found[0]


def swap_hero_tree(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    sub_tree: int,
    *,
    donor: Loadout | None = None,
) -> Loadout:
    """Move a build onto a different hero tree.

    Three things happen, and the first is the one that is easy to get wrong -- see
    ``selection_node_for_spec``: the selection node is **this spec's own**, pointed at
    the entry naming the target tree, never the donor's.

    The old tree's hero nodes are dropped. Filling the new tree is a search, not an
    edit, so this does not guess: pass ``donor`` -- another build of the same class that
    plays the target tree -- to transplant its hero selections, or leave it out and get
    a build with the tree selected and no hero points spent, which ``validate_loadout``
    will report as under its hero budget rather than let pass.
    """
    node_id = selection_node_for_spec(nodes, loadout.spec_id)
    entries = _entries(nodes, node_id)
    index = next((i for i, entry in enumerate(entries) if entry.sub_tree == sub_tree), None)
    if index is None:
        offered = sorted({entry.sub_tree for entry in entries})
        raise TalentEditError(
            f"spec {loadout.spec_id} cannot play sub tree {sub_tree}; "
            f"its selection node {node_id} offers {offered}"
        )

    kept = [
        s
        for s in loadout.selections
        if s.tree_index not in (TREE_HERO, TREE_SELECTION)
        or (s.tree_index == TREE_HERO and s.sub_tree == sub_tree)
    ]
    moved = _with(loadout, kept)
    moved = select_node(moved, nodes, node_id, rank=1, choice_index=index)

    if donor is not None:
        for source in donor.selections:
            if source.tree_index != TREE_HERO or source.sub_tree != sub_tree:
                continue
            moved = select_node(
                moved,
                nodes,
                source.node_id,
                rank=source.rank,
                choice_index=source.choice_index,
            )
    return moved


# --------------------------------------------------------------------------------
# Offline legality
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One reason a build is not what it claims to be.

    Two flags, three claims, and conflating them shipped a real defect: three findings
    carried ``simc_refuses`` with a message simc **never emits**, so a caller grepping a
    real run's stderr for the predicted line would find nothing and could not tell its
    own misprediction from a simc version change.

    * ``simc_refuses`` -- simc will not load this hash, and ``message`` is simc's own
      wording with its own punctuation, copied from the format literals in
      ``parse_traits_hash``, so it can be matched against a run's stderr.
    * ``unencodable`` -- ``encode_loadout`` will not write this loadout at all, so
      there is no hash and simc is never asked. The wording is **ours**, deliberately,
      because there is no simc line to quote.
    * neither -- a build simc will happily simulate and a player could not have.

    The two flags are independent rather than exclusive: a choice index past the node's
    last entry is a string this encoder refuses to write *and* one simc refuses to read,
    and both facts are worth carrying.
    """

    code: str
    message: str
    node_id: int | None = None
    entry_id: int | None = None
    simc_refuses: bool = False
    unencodable: bool = False


@dataclass(frozen=True)
class UnlockEdges:
    """Which node unlocks which -- absent from simc's data. See the module docstring.

    Empty by default, and an empty table is reported as *unchecked* rather than
    silently passed. ``source`` says where a populated one came from, because an edge
    is a claim about the tree and a claim needs a provenance.
    """

    source: str
    unlocks: Mapping[int, tuple[int, ...]]

    def __bool__(self) -> bool:
        return bool(self.unlocks)


NO_UNLOCK_EDGES = UnlockEdges(
    source=(
        "not available: simc ships no edge table, and the only file-reachable copy of "
        "Blizzard's is one class of thirteen and seven months stale"
    ),
    unlocks={},
)


@dataclass(frozen=True)
class PointBudget:
    """How many points a tree holds, derived rather than written down.

    ``per_tree`` is the **largest spend observed** in a set of loadouts somebody trusts,
    which for this project is the tier's own shipped profiles. Derived rather than
    hard-coded for the same reason ``spec_coverage``'s reference list is: a number typed
    into a table goes stale in the patch where it matters most, and nobody re-checks it.

    Measured over the 35 shipped MID2 profiles on simc 69a46e1 (2026-08-23), decoding
    against the PTR trait table the current tier runs on:

    ==========  ===================================================
    class       35 on 27 builds, 36 on 4, 37 on 2, **10 on 2**
    spec        34 on all 35
    hero        14 on 33 builds, 11 on 1, 12 on 1
    ==========  ===================================================

    So the ceiling is an **observed** one: 37 / 34 / 14. It bounds a search from above
    -- a build spending 40 class points is wrong however the game counts -- and it is
    deliberately not read as a floor. The two 10-point class trees are simc's shipped
    Frost Death Knight profiles, already flagged elsewhere as a thin class tree; taking
    the minimum instead of the maximum would enshrine that defect as the rule.
    """

    per_tree: Mapping[int, int]
    source: str


def derive_point_budget(loadouts: Sequence[Loadout], *, source: str) -> PointBudget:
    """The per-tree ceiling implied by a set of builds. See ``PointBudget``."""
    if not loadouts:
        raise TalentEditError("cannot derive a point budget from no loadouts")
    return PointBudget(
        per_tree={tree: max(loadout.points(tree) for loadout in loadouts) for tree in POINT_TREES},
        source=source,
    )


@dataclass(frozen=True)
class Validation:
    """What is wrong with a build, and what could not be looked at."""

    findings: tuple[Finding, ...]
    unchecked: tuple[str, ...]

    @property
    def legal(self) -> bool:
        """True when nothing was found. **Not** the same as "this build is legal" when
        ``unchecked`` is non-empty -- read them together, which is why they are one
        object."""
        return not self.findings

    @property
    def simc_refusals(self) -> tuple[Finding, ...]:
        """The subset a simulation would waste itself on."""
        return tuple(f for f in self.findings if f.simc_refuses)

    @property
    def unencodable(self) -> tuple[Finding, ...]:
        """The subset that has no hash at all -- ``encode_loadout`` raises on these."""
        return tuple(f for f in self.findings if f.unencodable)


def validate_loadout(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    *,
    budget: PointBudget | None = None,
    edges: UnlockEdges = NO_UNLOCK_EDGES,
) -> Validation:
    """Everything decidable about a build without running anything.

    The ``simc_refuses`` findings are transcriptions of ``parse_traits_hash``, in its
    own words and with its own ids, so a caller can both skip the simulation and
    predict the line simc would have printed. The ``unencodable`` ones say there is no
    hash to run at all, in this project's words because simc has no line for them. The
    rest is game legality, which simc does not check.

    Read ``findings`` and ``unchecked`` together: ``legal`` means nothing was *found*,
    and a build whose hero sub-tree is indeterminate or whose unlock edges nobody could
    look at has not been cleared, it has been partly examined.
    """
    findings: list[Finding] = []
    findings += _hash_findings(loadout, nodes)
    findings += _gate_violations(loadout, nodes)
    if budget is not None:
        findings += _budget_violations(loadout, budget)

    unchecked: list[str] = []
    indeterminate = _indeterminate_hero_tree(loadout)
    if indeterminate:
        unchecked.append(indeterminate)
    if not edges:
        unchecked.append(
            f"unlock edges ({edges.source}); a node with nothing leading to it cannot be "
            f"detected, so an illegal build can still pass this check"
        )
    else:
        findings += _edge_violations(loadout, nodes, edges)
    if budget is None:
        unchecked.append("point budgets (no budget supplied)")
    return Validation(findings=tuple(findings), unchecked=tuple(unchecked))


def _indeterminate_hero_tree(loadout: Loadout) -> str | None:
    """Whether the hero checks above were run on one hero tree or on several pooled.

    ``Loadout.in_tree(TREE_HERO)`` narrows hero nodes to the sub-tree the SELECTION node
    names, and falls back to *keeping everything* when there is no such node or when two
    of them disagree. That fallback is right for the tree view -- it draws what it has --
    and wrong for a gate or a budget: ``_gate_violations`` then compares a node's
    ``req_points`` against a spend pooled from two trees, which is over-permissive, and
    ``_budget_violations`` compares the same pooled figure against a ceiling one tree is
    supposed to hold.

    Neither check can say which tree it should have used, and neither is worth refusing
    over -- a build carrying no selection node is a legitimate intermediate state of a
    hero swap. What is not acceptable is saying nothing: "the sub-tree is indeterminate"
    must not reach a reader as "no findings, so this is legal", which is the same
    distinction ``unchecked`` exists for one row above.
    """
    hero = [s for s in loadout.selections if s.tree_index == TREE_HERO]
    if not hero or loadout.sub_tree is not None:
        return None
    named = sorted({s.sub_tree for s in loadout.selections if s.tree_index == TREE_SELECTION})
    trees = sorted({s.sub_tree for s in hero})
    how = (
        f"the build selects {len(named)} sub-tree selection node(s) naming {named}"
        if named
        else "the build selects no sub-tree selection node"
    )
    return (
        f"which hero tree this build plays ({how}, so no single tree is named); the "
        f"{sum(s.rank for s in hero)} hero point(s) in sub-tree(s) {trees} are therefore "
        f"pooled, and the hero gate and budget checks above ran against that pooled total "
        f"rather than against one tree's"
    )


def _hash_findings(loadout: Loadout, nodes: dict[int, list[Trait]]) -> list[Finding]:
    """Everything the *hash* decides: what simc refuses, and what has no hash at all.

    simc's refusals are ``parse_traits_hash`` at 69a46e1, in simc's own words. Every one
    of them throws -- ``do_error`` raises ``std::invalid_argument``, so even the message
    that says "ignoring" aborts the parse -- and the strings are copied from the format
    literals, punctuation included, so they can be matched against a real run's output.

    The rest are this project's own words for a loadout ``encode_loadout`` will not
    write. They are *not* dressed up as simc wordings: simc never sees these builds, so
    quoting a line it would have printed is a claim nobody can check. See ``Finding``.
    """
    found: list[Finding] = []

    # The cheapest of simc's eleven refusals to predict, and the only one this encoder
    # can introduce by itself: `encode_loadout` writes `loadout.version` verbatim, so a
    # loadout assembled with the wrong one produces a well-formed string simc refuses on
    # its first check. Left unchecked, `validate_loadout` called such a build legal.
    if loadout.version != LOADOUT_VERSION:
        found.append(
            Finding(
                code="version",
                message="Invalid serialization version.",
                simc_refuses=True,
                # Outside eight bits there is no string at all: the writer raises rather
                # than dropping the high bits and producing a version 2 hash.
                unencodable=bool(loadout.version >> VERSION_BITS),
            )
        )

    for selection in loadout.selections:
        node_id = selection.node_id
        entries = nodes.get(node_id)
        if not entries:
            # Not a simc refusal, and simc has no wording for it: `generate_tree_nodes`
            # hands the parser the class's own nodes and it reads one bit per node, so
            # a node of another class is unreachable there. It is the *encoder* that
            # refuses, in the same words, and there is then no hash to run.
            found.append(
                Finding(
                    code="unknown_node",
                    message=f"node {node_id} is not a node of this class",
                    node_id=node_id,
                    unencodable=True,
                )
            )
            continue

        # simc tests the node's FIRST entry, before it has read the choice index --
        # not the entry the loadout ends up on.
        first = entries[0]
        wrong_spec = first.spec_ids and loadout.spec_id not in first.spec_ids
        if first.tree_index != TREE_HERO and wrong_spec:
            if first.tree_index == TREE_SELECTION:
                found.append(
                    Finding(
                        code="hero_selection_spec",
                        message=(
                            f"Hero tree selection node {node_id} entry {first.entry_id} is not "
                            f"for the player's spec, ignoring."
                        ),
                        node_id=node_id,
                        entry_id=first.entry_id,
                        simc_refuses=True,
                    )
                )
            else:
                found.append(
                    Finding(
                        code="spec_rule",
                        message=(
                            f"Selected node {node_id} entry {first.entry_id} is not available "
                            f"to player's spec."
                        ),
                        node_id=node_id,
                        entry_id=first.entry_id,
                        simc_refuses=True,
                    )
                )

        if not selection.purchased:
            continue

        max_rank = max_ranks_of(entries)
        partial = selection.partial if selection.partial is not None else selection.rank < max_rank
        if selection.rank > max_rank and not partial:
            # simc never sees this one: with the partial bit clear the rank is not
            # written at all, so the hash says "all ranks" and the build quietly
            # becomes a legal one that is not the build asked for. Worth a finding
            # precisely because nothing downstream will ever complain.
            found.append(
                Finding(
                    code="rank_not_written",
                    message=(
                        f"node {node_id} holds {selection.rank} ranks where the node has "
                        f"{max_rank}; the hash will say {max_rank}"
                    ),
                    node_id=node_id,
                )
            )
        if partial:
            if first.node_type != NODE_TIERED and len(entries) > 1:
                found.append(
                    Finding(
                        code="non_choice_multiple_entries",
                        message=f"Non-choice node {node_id} has multiple entries.",
                        node_id=node_id,
                        simc_refuses=True,
                    )
                )
            if selection.rank > max_rank:
                found.append(
                    Finding(
                        code="rank_over_max",
                        message=(
                            f"{selection.rank} ranks selected for node {node_id}, "
                            f"{max_rank} ranks max."
                        ),
                        node_id=node_id,
                        simc_refuses=True,
                    )
                )
            elif selection.rank == max_rank:
                found.append(
                    Finding(
                        code="partial_at_max",
                        message=(
                            f"Partial rank for node {node_id} but all {selection.rank} ranks "
                            f"are allocated."
                        ),
                        node_id=node_id,
                        simc_refuses=True,
                    )
                )
            # Inside the partial branch, because that is the only branch that writes a
            # rank. Outside it this fired for a rank the hash never carries -- already
            # reported as `rank_not_written` twelve lines above -- and reported it as a
            # simc refusal, so a search using `simc_refusals` to skip simulations
            # discarded a build simc accepts.
            if selection.rank >> RANK_BITS:
                found.append(
                    Finding(
                        code="rank_unwritable",
                        message=(
                            f"rank {selection.rank} for node {node_id} does not fit in the "
                            f"{RANK_BITS} bits the format gives it"
                        ),
                        node_id=node_id,
                        unencodable=True,
                    )
                )

        if selection.choice_index is not None:
            if first.node_type not in (NODE_CHOICE, NODE_SELECTION):
                found.append(
                    Finding(
                        code="choice_on_plain",
                        message=f"Node {node_id} is not a choice node but has index selection.",
                        node_id=node_id,
                        simc_refuses=True,
                    )
                )
            elif selection.choice_index >= len(entries):
                # Both at once, which is what the two flags are for: simc refuses a
                # string carrying this index, and this encoder refuses to write one.
                found.append(
                    Finding(
                        code="choice_index_out_of_bounds",
                        message=(
                            f"Index {selection.choice_index} for choice node {node_id} "
                            f"out of bounds."
                        ),
                        node_id=node_id,
                        simc_refuses=True,
                        unencodable=True,
                    )
                )
            elif selection.choice_index >> CHOICE_BITS:
                found.append(
                    Finding(
                        code="choice_index_unwritable",
                        message=(
                            f"choice index {selection.choice_index} for node {node_id} does "
                            f"not fit in the {CHOICE_BITS} bits the format gives it"
                        ),
                        node_id=node_id,
                        unencodable=True,
                    )
                )
    return found


def _gate_violations(loadout: Loadout, nodes: dict[int, list[Trait]]) -> list[Finding]:
    """``req_points``: a node behind a gate needs that many points spent in its tree.

    Column 6 of ``trait_data.inc``, parsed all along and never read. The values in
    MID2's table are 0, 1, 8, 20 and 23 -- the class tree gates at 8/20/23, the spec
    tree at 8/20, the hero tree at 1.

    The spend counted is the whole tree **including the gated node itself**, which is
    the weaker of the two readings. Both were measured over the shipped MID2 profiles
    and both flag exactly the same two builds, so nothing here rests on the choice;
    the weaker one is taken because it under-claims, and inventing a violation is the
    worse direction to fail.
    """
    found: list[Finding] = []
    for tree in POINT_TREES:
        selections = loadout.in_tree(tree)
        spent = sum(s.rank for s in selections)
        for selection in selections:
            entry = next(
                (e for e in nodes.get(selection.node_id, ()) if e.entry_id == selection.entry_id),
                None,
            )
            if entry is None or not entry.req_points or spent >= entry.req_points:
                continue
            found.append(
                Finding(
                    code="gate",
                    message=(
                        f"{selection.name!r} (node {selection.node_id}) needs "
                        f"{entry.req_points} points in the {_TREE_NAMES[tree]} tree; "
                        f"the build spends {spent}"
                    ),
                    node_id=selection.node_id,
                    entry_id=selection.entry_id,
                )
            )
    return found


def _budget_violations(loadout: Loadout, budget: PointBudget) -> list[Finding]:
    found: list[Finding] = []
    for tree, ceiling in budget.per_tree.items():
        spent = loadout.points(tree)
        if spent > ceiling:
            found.append(
                Finding(
                    code="budget",
                    message=(
                        f"the build spends {spent} points in the {_TREE_NAMES[tree]} tree, "
                        f"above the {ceiling} observed in {budget.source}"
                    ),
                )
            )
    return found


def _edge_violations(
    loadout: Loadout, nodes: dict[int, list[Trait]], edges: UnlockEdges
) -> list[Finding]:
    """A node is reachable when something taken unlocks it, or nothing unlocks it at all.

    Untested against real edge data, because none is reachable -- see the module
    docstring. It is pinned against hand-built trees only, and that is stated rather
    than hidden: the check is here so that supplying edges is a one-line change, not so
    that anyone can claim edges are being checked today.
    """
    taken = {s.node_id for s in loadout.selections}
    unlocked_by: dict[int, set[int]] = {}
    for source_node, targets in edges.unlocks.items():
        for target in targets:
            unlocked_by.setdefault(target, set()).add(source_node)
    found: list[Finding] = []
    for selection in sorted(loadout.selections, key=lambda s: s.node_id):
        if selection.tree_index == TREE_SELECTION:
            continue
        parents = unlocked_by.get(selection.node_id)
        if not parents or parents & taken:
            continue
        found.append(
            Finding(
                code="unreachable",
                message=(
                    f"{selection.name!r} (node {selection.node_id}) is unlocked only by "
                    f"{sorted(parents)}, none of which the build takes"
                ),
                node_id=selection.node_id,
                entry_id=selection.entry_id,
            )
        )
    return found
