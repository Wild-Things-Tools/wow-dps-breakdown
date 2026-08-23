"""Mutating a build, and the offline legality check.

Every tree here is hand-built, so nothing depends on a simc checkout. The one thing
that cannot be hand-built is confidence that simc agrees, and that is what the
end-to-end run in the pull request covers: mutants generated through these primitives,
handed to simc at ``iterations=1``.
"""

import random

import pytest

from wowdps.talentedit import (
    NO_UNLOCK_EDGES,
    Finding,
    PointBudget,
    TalentEditError,
    UnlockEdges,
    derive_point_budget,
    deselect_node,
    move_rank,
    select_node,
    selected,
    selection_node_for_spec,
    set_choice,
    swap_hero_tree,
    validate_loadout,
)
from wowdps.talenttree import (
    BASE64,
    NODE_CHOICE,
    NODE_SELECTION,
    TREE_CLASS,
    TREE_HERO,
    TREE_SELECTION,
    TREE_SPEC,
    Loadout,
    TalentEncodeError,
    Trait,
    decode_loadout,
    encode_loadout,
    max_ranks_of,
)

SPEC = 260  # Outlaw, in the shipped data; any id does here
OTHER_SPEC = 259


def trait(node_id, entry_id, tree=TREE_CLASS, **kw):
    return Trait(
        tree_index=tree,
        class_id=4,
        entry_id=entry_id,
        node_id=node_id,
        max_ranks=kw.get("max_ranks", 1),
        req_points=kw.get("req_points", 0),
        spell_id=kw.get("spell_id", 1000 + entry_id),
        row=kw.get("row", 1),
        col=kw.get("col", 1),
        selection_index=kw.get("selection_index", 100),
        name=kw.get("name", f"Talent {entry_id}"),
        spec_ids=kw.get("spec_ids", ()),
        sub_tree=kw.get("sub_tree", 0),
        node_type=kw.get("node_type", 0),
    )


def empty(spec_id=SPEC) -> Loadout:
    return Loadout(version=2, spec_id=spec_id, selections=(), spare_bits=0)


# A small but complete class: a class tree, a spec tree, two hero trees and the two
# per-spec selection nodes that name them.
def sample_nodes():
    return {
        10: [trait(10, 100, max_ranks=2, name="Class A")],
        11: [trait(11, 110, max_ranks=2, name="Class B")],
        12: [
            trait(12, 120, node_type=2, name="Left"),
            trait(12, 121, node_type=2, name="Right"),
        ],
        13: [trait(13, 130, name="Gated", req_points=4)],
        20: [trait(20, 200, tree=TREE_SPEC, max_ranks=2, name="Spec A")],
        21: [trait(21, 210, tree=TREE_SPEC, max_ranks=2, name="Spec B")],
        30: [trait(30, 300, tree=TREE_HERO, sub_tree=51, name="Trick A")],
        31: [trait(31, 310, tree=TREE_HERO, sub_tree=52, name="Fate A")],
        # Deliberately adversarial ordering: the OTHER spec's selection node sorts
        # first *and* offers sub tree 52 as well. Anything that picks "the first
        # selection node offering this tree" takes node 40, which is the mistake simc
        # refuses -- and which the real Rogue data would also punish, since Outlaw's
        # node 99843 sorts after Subtlety's 99842 and both offer Trickster.
        40: [
            trait(
                40,
                400,
                tree=TREE_SELECTION,
                sub_tree=52,
                node_type=3,
                spec_ids=(OTHER_SPEC,),
                name="0",
            ),
            trait(
                40,
                401,
                tree=TREE_SELECTION,
                sub_tree=53,
                node_type=3,
                spec_ids=(OTHER_SPEC,),
                name="0",
            ),
        ],
        41: [
            trait(
                41, 410, tree=TREE_SELECTION, sub_tree=52, node_type=3, spec_ids=(SPEC,), name="0"
            ),
            trait(
                41, 411, tree=TREE_SELECTION, sub_tree=51, node_type=3, spec_ids=(SPEC,), name="0"
            ),
        ],
    }


# --------------------------------------------------------------------------------
# Mutation primitives
# --------------------------------------------------------------------------------


def test_a_choice_flip_changes_the_entry_and_keeps_the_rank():
    nodes = sample_nodes()
    build = select_node(empty(), nodes, 12, choice_index=0)
    assert selected(build, 12).name == "Left"
    flipped = set_choice(build, nodes, 12, 1)
    assert selected(flipped, 12).name == "Right"
    assert selected(flipped, 12).entry_id == 121
    assert selected(flipped, 12).rank == selected(build, 12).rank


def test_flipping_a_node_that_is_not_selected_is_refused():
    """ "Change which half I took" has no answer for a talent nobody took, and quietly
    selecting it would spend a point the caller did not ask to spend."""
    with pytest.raises(TalentEditError):
        set_choice(empty(), sample_nodes(), 12, 1)


def test_flipping_a_plain_node_is_refused():
    with pytest.raises(TalentEditError):
        set_choice(select_node(empty(), sample_nodes(), 10), sample_nodes(), 10, 1)


def test_a_rank_move_keeps_the_trees_total():
    nodes = sample_nodes()
    build = select_node(select_node(empty(), nodes, 20, rank=2), nodes, 21, rank=1)
    before = build.points(TREE_SPEC)
    moved = move_rank(build, nodes, source_node=20, target_node=21)
    assert moved.points(TREE_SPEC) == before
    assert selected(moved, 20).rank == 1
    assert selected(moved, 21).rank == 2


def test_a_rank_move_across_two_trees_is_refused():
    """The trees have separate point pools, so this is not a move a player can make --
    and a search that made it could not attribute the result to either tree."""
    nodes = sample_nodes()
    build = select_node(select_node(empty(), nodes, 10, rank=2), nodes, 20, rank=1)
    with pytest.raises(TalentEditError):
        move_rank(build, nodes, source_node=10, target_node=20)


def test_a_rank_move_that_empties_the_source_drops_it():
    nodes = sample_nodes()
    build = select_node(select_node(empty(), nodes, 20, rank=1), nodes, 21, rank=1)
    moved = move_rank(build, nodes, source_node=20, target_node=21)
    assert selected(moved, 20) is None
    assert selected(moved, 21).rank == 2


def test_a_rank_move_into_an_untaken_node_takes_it():
    nodes = sample_nodes()
    build = select_node(empty(), nodes, 20, rank=2)
    moved = move_rank(build, nodes, source_node=20, target_node=21)
    assert selected(moved, 21).rank == 1


def test_a_rank_move_from_a_node_that_has_too_few_ranks_is_refused():
    nodes = sample_nodes()
    build = select_node(empty(), nodes, 20, rank=1)
    with pytest.raises(TalentEditError):
        move_rank(build, nodes, source_node=20, target_node=21, ranks=2)


def test_lowering_a_rank_moves_the_partial_bit_with_it():
    """The reason every mutation rebuilds its selection instead of replacing one field.
    A node decoded at full rank carries ``partial=False``; drop a rank and keep that
    bit, and the build still *encodes* as full rank -- a mutation that silently did
    not happen."""
    nodes = sample_nodes()
    full = decode_loadout(encode_loadout(select_node(empty(), nodes, 20, rank=2), nodes), nodes)
    assert selected(full, 20).partial is False
    lowered = select_node(full, nodes, 20, rank=1)
    assert selected(lowered, 20).partial is None
    written = encode_loadout(lowered, nodes)
    assert selected(decode_loadout(written, nodes), 20).rank == 1


def test_selecting_a_plain_node_writes_no_choice_index():
    build = select_node(empty(), sample_nodes(), 10)
    assert selected(build, 10).choice_index is None


def test_giving_a_plain_node_a_choice_index_is_refused():
    with pytest.raises(TalentEditError):
        select_node(empty(), sample_nodes(), 10, choice_index=1)


def test_deselecting_a_node_that_was_never_taken_is_not_an_error():
    """A search asks for a state, not for a delta."""
    assert deselect_node(empty(), 10).selections == ()


def test_selecting_a_rank_above_the_maximum_is_refused():
    with pytest.raises(TalentEditError):
        select_node(empty(), sample_nodes(), 10, rank=3)


# --------------------------------------------------------------------------------
# The hero swap, and the node it has to go through
# --------------------------------------------------------------------------------


def test_the_selection_node_is_the_mutating_specs_own():
    """The measured fact the hero swap turns on: a class has one selection node **per
    specialisation**. Rogue carries 99842 (Subtlety), 99843 (Outlaw) and 99844
    (Assassination), and all 80 selection-node entries in simc's table carry exactly
    one spec id."""
    nodes = sample_nodes()
    assert selection_node_for_spec(nodes, SPEC) == 41
    assert selection_node_for_spec(nodes, OTHER_SPEC) == 40


def test_a_hero_swap_routes_through_the_players_own_selection_node():
    """The whole point of the primitive. Both specs here can play sub tree 52, so a
    naive implementation that picked *any* selection node offering it would take node
    41 -- the other spec's -- which simc refuses outright."""
    nodes = sample_nodes()
    build = swap_hero_tree(empty(), nodes, 52)
    assert selected(build, 41) is not None
    assert selected(build, 40) is None
    assert build.sub_tree == 52


def test_a_hero_swap_selects_the_entry_that_names_the_tree():
    nodes = sample_nodes()
    assert swap_hero_tree(empty(), nodes, 51).sub_tree == 51
    assert swap_hero_tree(empty(), nodes, 52).sub_tree == 52


def test_a_hero_swap_to_a_tree_the_spec_cannot_play_is_refused():
    with pytest.raises(TalentEditError):
        swap_hero_tree(empty(), sample_nodes(), 53)


def test_a_hero_swap_drops_the_old_trees_nodes():
    nodes = sample_nodes()
    trickster = select_node(swap_hero_tree(empty(), nodes, 51), nodes, 30)
    assert selected(trickster, 30) is not None
    fatebound = swap_hero_tree(trickster, nodes, 52)
    assert selected(fatebound, 30) is None
    assert fatebound.points(TREE_HERO) == 0


def test_a_hero_swap_with_a_donor_transplants_the_new_trees_nodes():
    nodes = sample_nodes()
    donor = select_node(swap_hero_tree(empty(), nodes, 52), nodes, 31)
    trickster = select_node(swap_hero_tree(empty(), nodes, 51), nodes, 30)
    swapped = swap_hero_tree(trickster, nodes, 52, donor=donor)
    assert selected(swapped, 31) is not None
    assert selected(swapped, 30) is None
    assert swapped.sub_tree == 52


def test_taking_another_specs_selection_node_is_reported_in_simcs_own_words():
    """simc's third refusal wording, and the one that reads like a warning and is not:
    ``do_error`` throws, so the "ignoring" branch never runs and the profile fails to
    load. Reproduced from a real run as exit 81."""
    nodes = sample_nodes()
    wrong = select_node(swap_hero_tree(empty(), nodes, 52), nodes, 40, choice_index=0)
    report = validate_loadout(wrong, nodes)
    assert not report.legal
    assert [f.code for f in report.simc_refusals] == ["hero_selection_spec"]
    assert report.simc_refusals[0].message == (
        "Hero tree selection node 40 entry 400 is not for the player's spec, ignoring."
    )


# --------------------------------------------------------------------------------
# What simc refuses, in simc's own words
# --------------------------------------------------------------------------------


def refusals(build, nodes):
    return {f.code: f.message for f in validate_loadout(build, nodes).simc_refusals}


def test_a_node_the_spec_may_not_take_is_reported():
    nodes = {10: [trait(10, 100, tree=TREE_SPEC, spec_ids=(OTHER_SPEC,))]}
    found = refusals(select_node(empty(), nodes, 10), nodes)
    assert found["spec_rule"] == ("Selected node 10 entry 100 is not available to player's spec.")


def test_a_rank_above_the_maximum_is_reported_in_simcs_own_words():
    """simc only reads the rank when the partial bit is set, so this is the only shape
    in which it ever sees an over-max rank -- and it is the shape a stale hash has."""
    from dataclasses import replace

    nodes = {10: [trait(10, 100, max_ranks=3)]}
    build = select_node(empty(), nodes, 10, rank=2)
    over = replace(build, selections=(replace(build.selections[0], rank=5, partial=True),))
    assert refusals(over, nodes)["rank_over_max"] == "5 ranks selected for node 10, 3 ranks max."


def test_an_over_max_rank_the_hash_cannot_carry_is_reported_separately():
    """With the partial bit clear the rank is never written, so simc sees a legal build
    at full rank and the extra ranks vanish between the loadout and the string. Nothing
    downstream can notice, which is exactly why it is worth a finding."""
    from dataclasses import replace

    nodes = {10: [trait(10, 100, max_ranks=3)]}
    build = select_node(empty(), nodes, 10, rank=2)
    over = replace(build, selections=(replace(build.selections[0], rank=5, partial=False),))
    report = validate_loadout(over, nodes)
    assert [f.code for f in report.findings] == ["rank_not_written"]
    assert report.simc_refusals == ()
    assert "the hash will say 3" in report.findings[0].message


def test_a_partial_rank_that_is_the_maximum_is_reported():
    from dataclasses import replace

    nodes = {10: [trait(10, 100, max_ranks=2)]}
    build = select_node(empty(), nodes, 10, rank=2)
    lying = replace(build, selections=(replace(build.selections[0], partial=True),))
    assert refusals(lying, nodes)["partial_at_max"] == (
        "Partial rank for node 10 but all 2 ranks are allocated."
    )


def test_a_choice_index_on_a_plain_node_is_reported():
    from dataclasses import replace

    nodes = {10: [trait(10, 100)]}
    build = select_node(empty(), nodes, 10)
    bad = replace(build, selections=(replace(build.selections[0], choice_index=1),))
    assert refusals(bad, nodes)["choice_on_plain"] == (
        "Node 10 is not a choice node but has index selection."
    )


def test_a_choice_index_past_the_last_entry_is_reported():
    from dataclasses import replace

    nodes = sample_nodes()
    build = select_node(empty(), nodes, 12, choice_index=1)
    bad = replace(build, selections=(replace(build.selections[0], choice_index=3),))
    assert refusals(bad, nodes)["choice_index_out_of_bounds"] == (
        "Index 3 for choice node 12 out of bounds."
    )


def test_a_clean_build_carries_no_refusal():
    nodes = sample_nodes()
    build = select_node(select_node(empty(), nodes, 10, rank=2), nodes, 12, choice_index=0)
    assert validate_loadout(build, nodes).simc_refusals == ()


# --------------------------------------------------------------------------------
# Game legality, which simc does not check at all
# --------------------------------------------------------------------------------


def test_a_gate_is_reported_when_the_tree_is_underspent():
    """``req_points`` has been parsed since the tree view was built and never read.
    simc does not check it, so a build can take a capstone with four points in the
    tree and simulate perfectly happily."""
    nodes = sample_nodes()
    build = select_node(empty(), nodes, 13)
    found = [f for f in validate_loadout(build, nodes).findings if f.code == "gate"]
    assert len(found) == 1
    assert "needs 4 points in the class tree" in found[0].message
    assert "spends 1" in found[0].message
    assert found[0].simc_refuses is False


def test_a_gate_is_not_reported_once_the_tree_is_spent_enough():
    nodes = sample_nodes()
    build = select_node(empty(), nodes, 13)
    build = select_node(build, nodes, 10, rank=2)
    build = select_node(build, nodes, 11, rank=2)
    assert [f for f in validate_loadout(build, nodes).findings if f.code == "gate"] == []


def test_the_point_budget_is_derived_from_the_builds_it_is_given():
    """Derived rather than typed, for the same reason the coverage reference list is:
    a number in a table goes stale in the patch where it matters most."""
    nodes = sample_nodes()
    small = select_node(empty(), nodes, 10, rank=1)
    large = select_node(select_node(empty(), nodes, 10, rank=2), nodes, 11, rank=2)
    budget = derive_point_budget([small, large], source="two hand-built builds")
    assert budget.per_tree[TREE_CLASS] == 4


def test_a_build_over_the_budget_is_reported():
    nodes = sample_nodes()
    budget = PointBudget(per_tree={TREE_CLASS: 2, TREE_SPEC: 99, TREE_HERO: 99}, source="a test")
    build = select_node(select_node(empty(), nodes, 10, rank=2), nodes, 11, rank=2)
    found = [
        f for f in validate_loadout(build, nodes, budget=budget).findings if f.code == "budget"
    ]
    assert len(found) == 1
    assert "spends 4 points in the class tree, above the 2" in found[0].message


def test_deriving_a_budget_from_nothing_is_refused():
    with pytest.raises(TalentEditError):
        derive_point_budget([], source="nothing at all")


# --------------------------------------------------------------------------------
# Unlock edges: absent, and said to be absent
# --------------------------------------------------------------------------------


def test_edges_are_reported_as_unchecked_when_none_are_supplied():
    """The honesty requirement. simc ships no edge table and no populated copy is
    reachable offline, so "no findings" must not read as "this build is legal"."""
    nodes = sample_nodes()
    report = validate_loadout(select_node(empty(), nodes, 10), nodes)
    assert report.legal
    assert any("unlock edges" in note for note in report.unchecked)
    assert not NO_UNLOCK_EDGES


def test_supplied_edges_find_a_node_nothing_taken_unlocks():
    nodes = sample_nodes()
    edges = UnlockEdges(source="a test", unlocks={10: (11,)})
    build = select_node(empty(), nodes, 11)
    report = validate_loadout(build, nodes, edges=edges)
    assert [f.code for f in report.findings] == ["unreachable"]
    assert not any("unlock edges" in note for note in report.unchecked)


def test_a_node_whose_parent_is_taken_is_reachable():
    nodes = sample_nodes()
    edges = UnlockEdges(source="a test", unlocks={10: (11,)})
    build = select_node(select_node(empty(), nodes, 10), nodes, 11)
    assert [
        f for f in validate_loadout(build, nodes, edges=edges).findings if f.code == "unreachable"
    ] == []


def test_a_node_nothing_unlocks_is_a_root_rather_than_unreachable():
    nodes = sample_nodes()
    edges = UnlockEdges(source="a test", unlocks={10: (11,)})
    build = select_node(empty(), nodes, 10)
    assert [
        f for f in validate_loadout(build, nodes, edges=edges).findings if f.code == "unreachable"
    ] == []


def test_validation_separates_what_simc_refuses_from_what_it_accepts():
    """The distinction the whole module is built around: one of these costs a
    simulation, the other costs a wrong answer."""
    nodes = sample_nodes()
    gated = select_node(empty(), nodes, 13)
    report = validate_loadout(gated, nodes)
    assert report.findings and report.simc_refusals == ()
    assert isinstance(report.findings[0], Finding)


# --------------------------------------------------------------------------------
# The property the whole feature rests on: a mutation that changes the build
# changes the hash
# --------------------------------------------------------------------------------


def as_string(bits: list[int]) -> str:
    """A hand-built bit stream as a loadout string, six bits per character, LSB first."""
    padded = bits + [0] * (-len(bits) % 6)
    return "".join(
        BASE64[sum(padded[start + offset] << offset for offset in range(6))]
        for start in range(0, len(padded), 6)
    )


def header(spec_id: int = SPEC) -> list[int]:
    version = [(2 >> i) & 1 for i in range(8)]
    spec = [(spec_id >> i) & 1 for i in range(16)]
    return version + spec + [0] * 128


def mutable_nodes():
    """``sample_nodes`` plus a tiered node and a second hero node in each sub-tree, so
    a random walk can reach every shape the encoder branches on."""
    nodes = sample_nodes()
    nodes[14] = [
        trait(14, 140, node_type=1, max_ranks=1, name="Tier A"),
        trait(14, 141, node_type=1, max_ranks=2, name="Tier B"),
    ]
    nodes[32] = [trait(32, 320, tree=TREE_HERO, sub_tree=51, max_ranks=2, name="Trick B")]
    nodes[33] = [trait(33, 330, tree=TREE_HERO, sub_tree=52, max_ranks=2, name="Fate B")]
    return nodes


def granted_base(nodes) -> Loadout:
    """A build carrying a **granted** choice node, decoded from a hand-built string.

    Not reachable through the mutation API -- ``_selection`` only ever builds purchased
    selections -- and it is the one shape finding the encoder's worst failure needs, so
    it is written on the wire and read back the way a shipped profile arrives.
    """
    records = {
        12: [1, 0],  # selected, NOT purchased: the game grants this choice node
        20: [1, 1, 0, 0],  # purchased, full rank
        30: [1, 1, 0, 0],  # a hero node of sub tree 51
        41: [1, 1, 0, 1, 1, 0],  # this spec's selection node, entry index 1 -> tree 51
    }
    bits = header()
    for node_id in sorted(nodes):
        bits += records.get(node_id, [0])
    return decode_loadout(as_string(bits), nodes)


def takes(loadout: Loadout) -> dict[int, tuple[int, int, bool]]:
    """What a build *takes*: entry, rank and whether it was bought, per node.

    The projection the property compares on, and it has to carry the entry id rather
    than the node id alone -- flipping a choice node changes nothing else.
    """
    return {s.node_id: (s.entry_id, s.rank, s.purchased) for s in loadout.selections}


def a_mutation(rng: random.Random, build: Loadout, nodes) -> Loadout:
    """One edit, chosen the way a sweep enumerating them would choose one."""
    node_ids = sorted(nodes)
    match rng.choice(("select", "deselect", "choice", "move", "hero")):
        case "select":
            node_id = rng.choice(node_ids)
            entries = nodes[node_id]
            is_choice = entries[0].node_type in (NODE_CHOICE, NODE_SELECTION)
            return select_node(
                build,
                nodes,
                node_id,
                rank=rng.randint(1, max_ranks_of(entries)),
                choice_index=rng.randrange(len(entries)) if is_choice else None,
            )
        case "deselect":
            return deselect_node(build, rng.choice(node_ids))
        case "choice":
            return set_choice(build, nodes, rng.choice(node_ids), rng.randrange(2))
        case "move":
            return move_rank(
                build,
                nodes,
                source_node=rng.choice(node_ids),
                target_node=rng.choice(node_ids),
                ranks=1,
            )
        case _:
            return swap_hero_tree(build, nodes, rng.choice((51, 52)))


def test_a_mutation_that_changes_the_build_changes_the_hash():
    """The property the encoder exists to guarantee, over many mutations rather than a
    handful of examples -- because the failure it pins was invisible to every example
    anybody wrote.

    A **granted** choice node flipped to its other entry used to encode to a string
    *byte-identical* to the unmutated build's: the format writes no choice index for a
    granted node, so the flip was silently dropped. Nothing downstream could see it --
    the hash is valid, simc runs it, ``validate_loadout`` returns ``legal`` with zero
    findings -- and a talent search would have attributed the base build's DPS to a
    variant it never simulated. That is the worst failure available to this module: not
    a crash, a confident wrong number.

    Two assertions per mutation, and the first is the one that catches it:

    * the round trip reflects the mutation -- ``decode(encode(mutant))`` takes what the
      mutant takes;
    * a mutant that takes something different encodes to a different string.
    """
    rng = random.Random(20260823)
    nodes = mutable_nodes()
    bases = [
        empty(),
        granted_base(nodes),
        select_node(select_node(empty(), nodes, 10, rank=2), nodes, 12, choice_index=0),
        select_node(swap_hero_tree(empty(), nodes, 52), nodes, 31),
    ]

    applied = refused = changed = 0
    for _ in range(600):
        base = rng.choice(bases)
        try:
            mutant = a_mutation(rng, base, nodes)
        except TalentEditError:
            # A sweep meets these constantly -- flipping a node nobody took, moving a
            # rank across two trees. An edit the tree cannot express is refused by
            # name, which is the whole point: it is never quietly performed.
            refused += 1
            continue
        applied += 1

        written = encode_loadout(mutant, nodes)
        assert takes(decode_loadout(written, nodes)) == takes(mutant), (
            f"the hash does not read back as the build that was written: {takes(mutant)}"
        )
        if takes(mutant) != takes(base):
            changed += 1
            assert written != encode_loadout(base, nodes), (
                f"a mutation changed the build and not the hash: {takes(base)} -> {takes(mutant)}"
            )

    assert applied > 200, f"only {applied} of 600 mutations were applied at all"
    assert changed > 100, f"only {changed} mutations changed the build; the walk is too timid"
    assert refused, "no edit was refused; the walk never reached an inexpressible one"


def test_flipping_a_granted_choice_node_is_refused_rather_than_silently_dropped():
    """The single case behind the property test above, stated as an example.

    ``set_choice`` used to copy the granted node's ``purchased=False`` onto a selection
    naming the *other* entry, and the encoder then wrote neither. The build described
    one talent and encoded the one it started from.
    """
    nodes = mutable_nodes()
    build = granted_base(nodes)
    granted = selected(build, 12)
    assert (granted.purchased, granted.rank, granted.entry_id) == (False, 1, 120)

    with pytest.raises(TalentEditError, match="granted"):
        set_choice(build, nodes, 12, 1)

    # The expressible edit is to buy the node, and it does change the hash.
    bought = select_node(build, nodes, 12, choice_index=1)
    assert encode_loadout(bought, nodes) != encode_loadout(build, nodes)
    assert selected(decode_loadout(encode_loadout(bought, nodes), nodes), 12).entry_id == 121


def test_the_encoder_refuses_a_granted_selection_it_cannot_write():
    """The same guard one layer down, where a loadout assembled by ``replace`` arrives.
    The editor cannot build one of these; ``dataclasses.replace`` can, and the tests in
    this file use it."""
    from dataclasses import replace

    nodes = mutable_nodes()
    build = granted_base(nodes)
    granted = selected(build, 12)

    lying = _replaced(build, replace(granted, entry_id=121, choice_index=1))
    with pytest.raises(TalentEncodeError, match="choice index"):
        encode_loadout(lying, nodes)

    ranked = _replaced(build, replace(granted, rank=2))
    with pytest.raises(TalentEncodeError, match="rank"):
        encode_loadout(ranked, nodes)


def _replaced(loadout: Loadout, selection) -> Loadout:
    from dataclasses import replace

    return replace(
        loadout,
        selections=tuple(
            selection if s.node_id == selection.node_id else s for s in loadout.selections
        ),
    )
