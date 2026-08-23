"""Mutating a build, and the offline legality check.

Every tree here is hand-built, so nothing depends on a simc checkout. The one thing
that cannot be hand-built is confidence that simc agrees, and that is what the
end-to-end run in the pull request covers: mutants generated through these primitives,
handed to simc at ``iterations=1``.
"""

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
    TREE_CLASS,
    TREE_HERO,
    TREE_SELECTION,
    TREE_SPEC,
    Loadout,
    Trait,
    decode_loadout,
    encode_loadout,
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
