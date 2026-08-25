"""Coverage over (damage spec x hero tree), which is the half a spec-level count hides.

A spec plays two hero trees and a tier routinely ships a build for one of them. Until
every tree could be named there was no way to say which half was missing, so the panel
could only ever report the spec.
"""

from __future__ import annotations

from wowdps.specindex import (
    HeroTree,
    builds_by_sub_tree,
    hero_tree_coverage,
    refused_profiles,
)

TREES = {
    42: HeroTree(sub_tree=42, wow_class="Hunter", spec_ids=[254, 255], name="Sentinel"),
    43: HeroTree(sub_tree=43, wow_class="Hunter", spec_ids=[253, 255], name="Pack Leader"),
    44: HeroTree(sub_tree=44, wow_class="Hunter", spec_ids=[253, 254], name="Dark Ranger"),
}
SPEC_IDS = {
    ("Hunter", "Beast Mastery"): 253,
    ("Hunter", "Marksmanship"): 254,
    ("Hunter", "Survival"): 255,
}


def manifest(builds, **coverage):
    block = {"damageSpecsKnown": 3, "shipped": [], "unvalidated": [], "missing": [], "broken": []}
    block.update(coverage)
    return {"specs": builds, "coverage": block}


def build(build_id, spec):
    return {"id": build_id, "class": "Hunter", "spec": spec}


def test_a_spec_with_one_of_its_two_trees_simulated_is_half_covered():
    """Survival is simulated and Sentinel Survival is; Pack Leader Survival is not.
    The spec-level count says "Survival is covered" and cannot say the second half."""
    document = manifest(
        [build("hunter_survival_default", "Survival")],
        shipped=[{"class": "Hunter", "spec": "Survival"}],
    )
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {"hunter_survival_default": 42})
    assert coverage is not None
    assert (coverage["covered"], coverage["cells"]) == (1, 2)
    assert coverage["uncovered"] == [
        {
            "class": "Hunter",
            "spec": "Survival",
            "specId": 255,
            "subTree": 43,
            "tree": "Pack Leader",
            "state": "shipped",
            "reason": None,
        }
    ]


def test_each_cell_carries_the_state_of_its_spec_rather_than_re_deriving_it():
    """Three reasons a spec can be absent, and only one of them is something a reader
    can help with. Re-deriving them here would let the two panels drift apart."""
    document = manifest(
        [build("hunter_marksmanship_default", "Marksmanship")],
        shipped=[],
        unvalidated=[{"class": "Hunter", "spec": "Marksmanship"}],
        missing=[{"class": "Hunter", "spec": "Survival"}],
        broken=[{"class": "Hunter", "spec": "Beast Mastery"}],
    )
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {"hunter_marksmanship_default": 42})
    assert coverage["covered"] == 1
    assert coverage["cells"] == 6
    states = {(row["spec"], row["state"]) for row in coverage["uncovered"]}
    assert states == {
        ("Beast Mastery", "broken"),
        ("Marksmanship", "unvalidated"),
        ("Survival", "missing"),
    }


def test_a_tree_the_table_places_on_no_spec_is_reported_rather_than_counted():
    """simc's trait table carries Annihilator with no id_spec on any of its nodes, and
    a MID2 build plainly plays it. Counting the pair would invent a pairing; dropping
    it silently would lose the only evidence the table has a hole."""
    trees = dict(TREES)
    trees[124] = HeroTree(sub_tree=124, wow_class="Hunter", spec_ids=[], name="Nowhere")
    document = manifest(
        [build("hunter_survival_default", "Survival")],
        shipped=[{"class": "Hunter", "spec": "Survival"}],
    )
    coverage = hero_tree_coverage(document, trees, SPEC_IDS, {"hunter_survival_default": 124})
    assert coverage["unplaced"] == [
        {"build": "hunter_survival_default", "subTree": 124, "tree": "Nowhere"}
    ]
    assert coverage["covered"] == 0


def test_a_manifest_without_a_coverage_block_reports_nothing_rather_than_completeness():
    assert hero_tree_coverage({"specs": []}, TREES, SPEC_IDS, {}) is None


def test_the_builds_of_a_tree_are_recorded_on_it():
    trees = {
        key: HeroTree(key, value.wow_class, list(value.spec_ids), value.name)
        for key, value in TREES.items()
    }
    document = manifest(
        [
            build("hunter_survival_default", "Survival"),
            build("hunter_marksmanship_default", "Marksmanship"),
        ],
        shipped=[
            {"class": "Hunter", "spec": "Survival"},
            {"class": "Hunter", "spec": "Marksmanship"},
        ],
    )
    hero_tree_coverage(
        document,
        trees,
        SPEC_IDS,
        {"hunter_survival_default": 42, "hunter_marksmanship_default": 42},
    )
    assert trees[42].builds == ["hunter_marksmanship_default", "hunter_survival_default"]
    assert trees[43].builds == []


def test_the_build_to_tree_join_reads_the_talent_documents_own_key_names():
    """``build["specId"]`` in talent-trees.json holds the dataset's *build* id, not a
    specialization id, and ``tree`` is ``<specId>-<subTree>``. Reading either the
    other way round builds a table that joins to nothing."""
    document = {
        "builds": [
            {"specId": "rogue_outlaw_default", "tree": "260-51"},
            {"specId": "rogue_subtlety_default", "tree": "261-53"},
            {"specId": "broken", "tree": None},
        ]
    }
    assert builds_by_sub_tree(document) == {
        "rogue_outlaw_default": 51,
        "rogue_subtlety_default": 53,
    }


def test_a_cell_whose_profile_will_not_load_carries_the_reason_and_the_node():
    """ "No numbers for Arms Warrior" and "simc wrote a profile whose talent hash the
    current tree refuses at node 110203" are different findings, and only the second
    tells a reader what would have to change."""
    document = manifest(
        [],
        unvalidated=[{"class": "Hunter", "spec": "Survival"}],
    )
    refused = [
        {
            "class": "Hunter",
            "spec": "Survival",
            "profile": "MID2_Hunter_Survival",
            "heroTree": None,
            "unvalidated": True,
            "reason": "Selected node 110203 entry 136735 is not available to player's spec",
        }
    ]
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {}, refused)
    assert {row["reason"] for row in coverage["uncovered"]} == {
        "Selected node 110203 entry 136735 is not available to player's spec"
    }


def test_a_refusal_that_names_a_hero_tree_answers_for_that_cell_only():
    """Havoc's two disabled builds are refused separately. A refusal attached to the
    spec would claim the other tree is broken too, which is a different claim."""
    document = manifest([], unvalidated=[{"class": "Hunter", "spec": "Survival"}])
    refused = [
        {
            "class": "Hunter",
            "spec": "Survival",
            "profile": "MID2_Hunter_Survival_Sentinel",
            "heroTree": "Sentinel",
            "unvalidated": True,
            "reason": "choice index 1 out of bounds for node 91020 (1 entries)",
        }
    ]
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {}, refused)
    by_tree = {row["tree"]: row["reason"] for row in coverage["uncovered"]}
    assert by_tree["Sentinel"] == "choice index 1 out of bounds for node 91020 (1 entries)"
    assert by_tree["Pack Leader"] is None


def test_a_refusal_naming_the_tree_beats_one_that_names_none():
    """Retribution ships two disabled builds refused at two different nodes, one of
    them unnamed. Taking the first match printed the unnamed build's node against the
    named build's tree."""
    document = manifest([], unvalidated=[{"class": "Hunter", "spec": "Survival"}])
    refused = [
        {
            "class": "Hunter",
            "spec": "Survival",
            "profile": "MID2_Hunter_Survival",
            "heroTree": None,
            "unvalidated": True,
            "reason": "node 81527",
        },
        {
            "class": "Hunter",
            "spec": "Survival",
            "profile": "MID2_Hunter_Survival_Sentinel",
            "heroTree": "Sentinel",
            "unvalidated": True,
            "reason": "node 81532",
        },
    ]
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {}, refused)
    by_tree = {row["tree"]: row["reason"] for row in coverage["uncovered"]}
    assert by_tree["Sentinel"] == "node 81532"
    assert by_tree["Pack Leader"] == "node 81527"


def test_no_hero_tree_coverage_at_all_rather_than_every_spec_reported_uncovered():
    """Placing a build in a tree needs `talent-trees.json`. Without it every build is
    unplaceable, and answering anyway reported cells=53, covered=0 over the real MID2
    manifest -- all 34 shipped specs listed as having no build for either tree, with
    both of Arcane Mage's sitting in the ranking directly above. The caller publishes
    null and the panel falls back to spec-level coverage, which is exactly what its
    own warning promised."""
    document = manifest(
        [build("hunter_survival_default", "Survival")],
        shipped=[{"class": "Hunter", "spec": "Survival"}],
    )
    assert hero_tree_coverage(document, TREES, SPEC_IDS, {}) is None
    # A tier that published no builds at all is not this case: there is nothing to
    # misreport, and the spec-level lists already say the dataset is empty.
    assert hero_tree_coverage(manifest([]), TREES, SPEC_IDS, {}) is not None


# --------------------------------------------------------------------------------
# Why a profile is refused: our sentence and simc's, and the count in both
# --------------------------------------------------------------------------------

_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _trait(node_id, entry_id, **kw):
    from wowdps.talenttree import TREE_CLASS, Trait

    return Trait(
        tree_index=kw.get("tree", TREE_CLASS),
        class_id=1,
        entry_id=entry_id,
        node_id=node_id,
        max_ranks=1,
        req_points=0,
        spell_id=1000 + entry_id,
        row=1,
        col=1,
        selection_index=100,
        name=kw.get("name", f"Talent {entry_id}"),
        spec_ids=kw.get("spec_ids", ()),
        sub_tree=0,
        node_type=kw.get("node_type", 0),
    )


def _hash(spec_id: int, node_bits: list[int], version: int = 2) -> str:
    """A loadout string, packed the way Blizzard's exporter does.

    Built here rather than through ``encode_loadout`` on purpose: the strings this
    tests are ones the *encoder refuses to write*, so going through it would make the
    fixture impossible to state.
    """
    bits = [(version >> i) & 1 for i in range(8)]
    bits += [(spec_id >> i) & 1 for i in range(16)]
    bits += [0] * 128 + node_bits
    text = ""
    for start in range(0, len(bits), 6):
        chunk = bits[start : start + 6]
        text += _BASE64[sum(bit << index for index, bit in enumerate(chunk))]
    return text


#: One node, selected and purchased, at full rank, carrying a choice index.
def _picked(index: int) -> list[int]:
    return [1, 1, 0, 1] + [(index >> i) & 1 for i in range(2)]


#: One node, selected and purchased, at full rank, carrying no choice index.
_PLAIN = [1, 1, 0, 0]


def test_a_refusal_reason_is_our_sentence_and_simcs_line_is_beside_it_not_inside_it():
    """Issue #43. What was published as the reason for four of MID2's six refused
    profiles was ``decode_loadout``'s own message -- "choice index 1 out of bounds for
    node 91020 (1 entries)" -- a sentence simc never emits. simc's line for that node
    is a different one of its refusals entirely, so a reader grepping a real run's
    stderr for the published text finds nothing.

    Two fields, two claims: ``reason`` is ours and reads on from the panel's own "simc
    will not load it:", ``simcMessage`` is simc's with simc's punctuation.
    """
    from wowdps.specindex import _refusal

    nodes = {10: [_trait(10, 100, name="Unbound Chaos")]}
    reason, simc_message = _refusal(_hash(577, _picked(1)), nodes)

    assert simc_message == "Node 10 is not a choice node but has index selection."
    # Ours, and not simc's dressed up as ours: it must not be that string.
    assert reason != simc_message
    assert "10" in reason and "Unbound Chaos" in reason
    assert "no choice" in reason
    # And nothing that reads as a decoder's internal complaint.
    assert "out of bounds" not in reason


def test_the_reason_carries_how_many_nodes_are_stale_not_just_the_first():
    """ "One node" was an artifact: ``decode_loadout`` stops at the first failure. Read
    on and MID2's real figures are 5 for Havoc Aldrachi Reaver, 7 and 4 for the two
    Retribution builds, 2 each for Arms and Fury.

    Where the count comes from reading past the point simc stops at, the sentence says
    so -- that reader is out of step with whoever wrote the hash by definition, so the
    number is the extent of the problem and not a tally to quote elsewhere.
    """
    from wowdps.specindex import _refusal

    nodes = {
        10: [_trait(10, 100)],
        20: [_trait(20, 200)],
        30: [_trait(30, 300)],
    }
    reason, _ = _refusal(_hash(577, _picked(1) + _picked(1) + _picked(1)), nodes)
    assert "3 nodes" in reason
    assert "reading on past the point simc stops at" in reason

    # One really is one, and says so rather than being silent about the number.
    single = {10: [_trait(10, 100)], 20: [_trait(20, 200)]}
    reason, _ = _refusal(_hash(577, _picked(1) + _PLAIN), single)
    assert "only node" in reason
    assert "3 nodes" not in reason


def test_a_real_choice_node_given_too_high_an_index_gets_simcs_other_wording():
    """simc has two lines here and the node decides which, not the index. Every
    overflow measured on MID1 and MID2 so far is a plain node, so a version that only
    ever wrote that one would be right today and wrong without warning.
    """
    from wowdps.specindex import _refusal
    from wowdps.talenttree import NODE_CHOICE

    nodes = {
        10: [
            _trait(10, 100, node_type=NODE_CHOICE, name="Either"),
            _trait(10, 101, node_type=NODE_CHOICE, name="Or"),
        ]
    }
    reason, simc_message = _refusal(_hash(577, _picked(3)), nodes)
    assert simc_message == "Index 3 for choice node 10 out of bounds."
    assert "only 2 to choose from" in reason


def test_a_spec_rule_refusal_counts_its_offenders_and_quotes_simc_exactly():
    """The other half of the same defect. This count is from a *strict* decode, so it
    is not a reading past a failure and carries no hedge -- but it is still not one.
    """
    from wowdps.specindex import _refusal

    nodes = {
        10: [_trait(10, 100, spec_ids=(71,))],
        20: [_trait(20, 200, spec_ids=(72,), name="Odyn's Fury")],
        30: [_trait(30, 300, spec_ids=(72,), name="Rend")],
    }
    reason, simc_message = _refusal(_hash(71, _PLAIN * 3), nodes)

    assert simc_message == "Selected node 20 entry 200 is not available to player's spec."
    assert "2 of the selected nodes" in reason
    assert "Odyn's Fury" in reason and "where simc stops" in reason

    # And a single offender is not inflated into a plural.
    nodes.pop(30)
    reason, _ = _refusal(_hash(71, _PLAIN * 2), nodes)
    assert reason.startswith("node 20 (Odyn's Fury, entry 200) belongs to another")


def test_a_hash_that_is_not_a_choice_overflow_quotes_nothing_at_all():
    """A version, alphabet or length failure is a different one of simc's refusals and
    this reader has not established which, so there is nothing to put in
    ``simcMessage`` -- and inventing one would be the whole of issue #43 again.
    """
    from wowdps.specindex import _refusal

    nodes = {10: [_trait(10, 100)]}
    reason, simc_message = _refusal(_hash(577, _PLAIN, version=1), nodes)
    assert simc_message is None
    assert reason.startswith("this project's reader cannot decode the hash")
    assert "serialization version" in reason


def test_a_hash_that_loads_is_not_reported_as_refused():
    """The control. A reason generator that fires on everything says nothing."""
    from wowdps.specindex import _refusal

    nodes = {10: [_trait(10, 100, spec_ids=(71,))]}
    assert _refusal(_hash(71, _PLAIN), nodes) is None


def test_refused_profiles_tolerates_a_tier_simc_no_longer_ships(tmp_path):
    """`build_index` already tolerates it, and `tiers.json` outlives simc's profile
    directories -- so raising here crashes the publish loop on a stale tier."""
    (tmp_path / "profiles" / "MID2").mkdir(parents=True)
    (tmp_path / "engine" / "dbc" / "generated").mkdir(parents=True)
    (tmp_path / "engine" / "dbc" / "generated" / "trait_data.inc").write_text(
        "// nothing\n", encoding="utf-8"
    )
    assert refused_profiles(tmp_path, "TWW3") == []
