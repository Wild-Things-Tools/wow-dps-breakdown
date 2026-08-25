"""Repairing a hash simc refuses, and refusing to repair one that cannot be read.

Hand-built trees throughout. The half that cannot be tested here -- whether simc
actually loads the result -- is measured in the pull request against a real binary.
"""

import pytest

from wowdps import talentedit, talentrepair
from wowdps.talenttree import (
    NODE_CHOICE,
    TREE_CLASS,
    TREE_HERO,
    TREE_SPEC,
    Loadout,
    TalentDecodeError,
    Trait,
    decode_lenient,
    decode_loadout,
    encode_loadout,
)

SPEC = 260
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


def two_entry_tree():
    """The tree a hash was exported against: node 12 is a choice node with two entries."""
    return {
        10: [trait(10, 100, max_ranks=2, name="Class A")],
        12: [
            trait(12, 120, node_type=NODE_CHOICE, name="Left"),
            trait(12, 121, node_type=NODE_CHOICE, name="Right"),
        ],
        20: [trait(20, 200, tree=TREE_SPEC, max_ranks=2, name="Spec A")],
        30: [trait(30, 300, tree=TREE_HERO, sub_tree=51, name="Hero A")],
    }


def one_entry_tree():
    """The same tree after node 12 lost its second entry -- the real MID2 failure."""
    nodes = two_entry_tree()
    nodes[12] = [trait(12, 120, node_type=0, name="Left")]
    return nodes


def build_on(nodes, spec_id=SPEC, choice=1):
    build = Loadout(version=2, spec_id=spec_id, selections=(), spare_bits=0)
    build = talentedit.select_node(build, nodes, 10, rank=2)
    build = talentedit.select_node(build, nodes, 12, choice_index=choice)
    build = talentedit.select_node(build, nodes, 20, rank=2)
    build = talentedit.select_node(build, nodes, 30)
    return build


def budget_for(nodes, loadouts):
    return talentedit.derive_point_budget(loadouts, source="a test corpus")


# --------------------------------------------------------------------------------
# The lenient reader
# --------------------------------------------------------------------------------


def test_the_strict_reader_still_raises_on_an_out_of_range_choice_index():
    """Unchanged behaviour for every other caller, and the reason: a build that silently
    lost a choice is a build that came back as something else."""
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    with pytest.raises(TalentDecodeError):
        decode_loadout(written, one_entry_tree())


def test_the_lenient_reader_reads_the_rest_of_the_build():
    """The bit stream stays in sync: the writer emitted a choice bit and two index bits
    and the reader consumes exactly those three, so only the value is out of range."""
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    loadout, overflows = decode_lenient(written, one_entry_tree())
    assert [o.node_id for o in overflows] == [12]
    assert overflows[0].written_index == 1
    assert overflows[0].entries == 1
    # Everything before and after the overflowing node survives.
    assert {s.node_id for s in loadout.selections} == {10, 12, 20, 30}
    assert loadout.points(TREE_CLASS) == 3
    assert loadout.points(TREE_SPEC) == 2


def test_the_overflowing_node_comes_back_with_no_choice_index():
    """The state the format writes for a node with no choice, which is what simc's own
    reader gives a node carrying no index. Keeping the out-of-range value would leave
    the loadout unencodable -- the state being repaired."""
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    loadout, _ = decode_lenient(written, one_entry_tree())
    node = next(s for s in loadout.selections if s.node_id == 12)
    assert node.choice_index is None
    assert node.entry_id == 120


def test_the_lenient_reader_returns_no_overflow_for_a_healthy_hash():
    nodes = two_entry_tree()
    written = encode_loadout(build_on(nodes, choice=0), nodes)
    loadout, overflows = decode_lenient(written, nodes)
    assert overflows == ()
    assert encode_loadout(loadout, nodes) == written


# --------------------------------------------------------------------------------
# The soundness screen
# --------------------------------------------------------------------------------


def test_a_framing_range_cannot_be_derived_from_nothing():
    """A screen with no corpus behind it would pass everything."""
    with pytest.raises(talentrepair.RepairError):
        talentrepair.observed_framing([])


def test_a_stream_that_overran_the_string_is_rejected():
    """The signature of a reader out of step with whoever wrote the hash. Repairing one
    produces a valid hash for a build nobody wrote, which is the worst outcome here --
    the number that comes back looks exactly like a real one."""
    nodes = two_entry_tree()
    build = build_on(nodes, choice=0)
    overrun = Loadout(
        version=build.version,
        spec_id=build.spec_id,
        selections=build.selections,
        spare_bits=-77,
    )
    result = talentrepair.check_soundness(overrun, budget_for(nodes, [build]), framing=(0, 9))
    assert not result.ok
    assert "overran" in " ".join(result.reasons)


def test_a_stream_that_stopped_far_short_is_rejected():
    nodes = two_entry_tree()
    build = build_on(nodes, choice=0)
    short = Loadout(
        version=build.version,
        spec_id=build.spec_id,
        selections=build.selections,
        spare_bits=40,
    )
    result = talentrepair.check_soundness(short, budget_for(nodes, [build]), framing=(0, 9))
    assert not result.ok
    assert "stopped" in " ".join(result.reasons)


def test_a_build_over_the_tier_s_own_point_ceiling_is_rejected():
    nodes = two_entry_tree()
    thin = talentedit.select_node(
        Loadout(version=2, spec_id=SPEC, selections=(), spare_bits=0), nodes, 10, rank=1
    )
    fat = build_on(nodes, choice=0)
    result = talentrepair.check_soundness(fat, budget_for(nodes, [thin]), framing=(0, 9))
    assert not result.ok
    assert "above the" in " ".join(result.reasons)


def test_a_healthy_build_clears_the_screen():
    nodes = two_entry_tree()
    build = build_on(nodes, choice=0)
    result = talentrepair.check_soundness(build, budget_for(nodes, [build]), framing=(0, 9))
    assert result.ok
    assert result.reasons == ()


# --------------------------------------------------------------------------------
# The repair
# --------------------------------------------------------------------------------


def test_a_choice_index_overflow_is_repaired_and_the_result_re_reads():
    nodes = one_entry_tree()
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    reference, _ = decode_lenient(written, nodes)
    result = talentrepair.repair(
        "test_build", written, nodes, budget_for(nodes, [reference]), framing=(0, 9)
    )
    assert result.ok
    assert [c.kind for c in result.corrections] == ["choice-index-dropped"]
    # The whole point: what comes out reads back cleanly under the *strict* reader.
    again = decode_loadout(result.repaired_hash, nodes)
    assert {s.node_id for s in again.selections} == {10, 12, 20, 30}


def test_a_node_belonging_to_another_spec_is_dropped_and_named():
    nodes = two_entry_tree()
    nodes[13] = [trait(13, 130, tree=TREE_SPEC, spec_ids=(OTHER_SPEC,), name="Someone Else's")]
    build = talentedit.select_node(build_on(nodes, choice=0), nodes, 13)
    written = encode_loadout(build, nodes)
    result = talentrepair.repair(
        "test_build", written, nodes, budget_for(nodes, [build]), framing=(0, 9)
    )
    assert result.ok
    assert [c.kind for c in result.corrections] == ["node-deselected"]
    assert result.corrections[0].node_id == 13
    assert "Someone Else's" in result.corrections[0].detail
    assert 13 not in {s.node_id for s in decode_loadout(result.repaired_hash, nodes).selections}


def test_an_unsound_decode_is_refused_rather_than_repaired():
    """A repair on a desynchronised read is a valid hash for a build nobody wrote."""
    nodes = one_entry_tree()
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    reference, _ = decode_lenient(written, nodes)
    result = talentrepair.repair(
        "test_build",
        written,
        nodes,
        budget_for(nodes, [reference]),
        # A framing range the build cannot be inside, standing in for a real desync.
        framing=(100, 200),
    )
    assert not result.ok
    assert result.repaired_hash is None
    assert "soundness screen" in result.refused


def test_a_hash_with_nothing_wrong_with_it_is_not_repaired():
    """ "Nothing to repair" is a distinct answer from "repaired", and returning the
    original under a repair's label would attribute a correction that never happened."""
    nodes = two_entry_tree()
    build = build_on(nodes, choice=0)
    written = encode_loadout(build, nodes)
    result = talentrepair.repair(
        "test_build", written, nodes, budget_for(nodes, [build]), framing=(0, 9)
    )
    assert not result.ok
    assert result.repaired_hash is None
    assert "nothing to repair" in result.refused


def test_a_repair_never_claims_to_be_an_optimisation():
    """The claim a repair makes is checkable and small; borrowing the word "best" would
    make it neither."""
    nodes = one_entry_tree()
    written = encode_loadout(build_on(two_entry_tree()), two_entry_tree())
    reference, _ = decode_lenient(written, nodes)
    result = talentrepair.repair(
        "b", written, nodes, budget_for(nodes, [reference]), framing=(0, 9)
    )
    text = " ".join(result.caveats())
    assert "not an optimised build" in text
    assert "can only reject" in text


def test_freed_points_are_reported_rather_than_re_spent():
    """Dropping a spec-illegal node frees a point. Spending it is a search decision and
    a repair makes none -- but the count has to be published, or a build four points
    light reads as a build that spent them."""
    nodes = two_entry_tree()
    nodes[13] = [trait(13, 130, tree=TREE_SPEC, spec_ids=(OTHER_SPEC,), name="Wrong Spec")]
    build = talentedit.select_node(build_on(nodes, choice=0), nodes, 13)
    written = encode_loadout(build, nodes)
    result = talentrepair.repair("b", written, nodes, budget_for(nodes, [build]), framing=(0, 9))
    assert result.ok
    assert sum(result.unspent) >= 1
    assert "unspent" in " ".join(result.caveats())
