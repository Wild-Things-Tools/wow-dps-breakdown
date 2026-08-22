"""Coverage over (damage spec x hero tree), which is the half a spec-level count hides.

A spec plays two hero trees and a tier routinely ships a build for one of them. Until
every tree could be named there was no way to say which half was missing, so the panel
could only ever report the spec.
"""

from __future__ import annotations

from wowdps.specindex import HeroTree, builds_by_sub_tree, hero_tree_coverage

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
        [],
        shipped=[],
        unvalidated=[{"class": "Hunter", "spec": "Marksmanship"}],
        missing=[{"class": "Hunter", "spec": "Survival"}],
        broken=[{"class": "Hunter", "spec": "Beast Mastery"}],
    )
    coverage = hero_tree_coverage(document, TREES, SPEC_IDS, {})
    assert coverage["covered"] == 0
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
