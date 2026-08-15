"""The loadout decode, checked against real hashes and hand-built streams.

The strongest checks available offline do not need simc's trait table at all: the
header of a real loadout string carries the specialization id, and the dataset already
says which spec each build is. Fifteen correct spec ids cannot come out of a wrong
alphabet or a wrong bit order.
"""

import json
from pathlib import Path

import pytest

from wowdps.talenttree import (
    BASE64,
    CLASS_IDS,
    TREE_CLASS,
    TREE_HERO,
    TREE_SPEC,
    TalentDecodeError,
    Trait,
    _BitReader,
    decode_loadout,
    nodes_for_class,
    parse_trait_data,
    tree_layout,
)

DATA = Path(__file__).resolve().parents[2] / "web" / "public" / "data" / "MID2" / "specs"

#: The canonical WoW specialization ids, which is what the loadout header carries.
#: Written out rather than derived so the test states what it expects.
EXPECTED_SPEC_IDS = {
    ("Death Knight", "Frost"): 251,
    ("Death Knight", "Unholy"): 252,
    ("Hunter", "Beast Mastery"): 253,
    ("Hunter", "Marksmanship"): 254,
    ("Hunter", "Survival"): 255,
    ("Mage", "Arcane"): 62,
    ("Mage", "Fire"): 63,
    ("Mage", "Frost"): 64,
    ("Priest", "Shadow"): 258,
    ("Rogue", "Assassination"): 259,
    ("Rogue", "Subtlety"): 261,
    ("Shaman", "Elemental"): 262,
    ("Shaman", "Enhancement"): 263,
    ("Warlock", "Affliction"): 265,
    ("Warlock", "Destruction"): 267,
}


def real_builds():
    if not DATA.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(DATA.glob("*.json"))]


# --------------------------------------------------------------------------------
# The bit reader
# --------------------------------------------------------------------------------


def test_bits_are_read_least_significant_first_within_each_character():
    """'B' is index 1, so its six bits are 100000 read in stream order."""
    reader = _BitReader("B")
    assert reader.read(1) == 1
    assert reader.read(5) == 0


def test_a_multi_bit_value_spans_characters():
    # 'A' is 0 and '/' is 63; reading eight bits takes all of 'A' then two set bits.
    reader = _BitReader("A/")
    assert reader.read(8) == 0b11000000


def test_reading_past_the_end_yields_zeros_rather_than_raising():
    """simc does the same, and a loadout that takes nothing late in the tree relies
    on it: the stream simply stops and the remaining nodes are unselected."""
    reader = _BitReader("A")
    assert reader.read(64) == 0


def test_a_character_outside_the_alphabet_is_refused():
    with pytest.raises(TalentDecodeError):
        _BitReader("not a loadout!")


def test_the_alphabet_is_blizzards_not_standard_base64_padding():
    assert len(BASE64) == 64
    assert BASE64.startswith("ABC") and BASE64.endswith("+/")
    assert "=" not in BASE64


# --------------------------------------------------------------------------------
# Real hashes: the header check that needs no trait table
# --------------------------------------------------------------------------------


@pytest.mark.skipif(not real_builds(), reason="no committed MID2 dataset")
def test_every_shipped_build_decodes_to_its_own_specialization():
    """The check that pins the whole format. A wrong alphabet, a wrong bit order or a
    wrong header layout cannot produce fifteen correct canonical spec ids."""
    seen = {}
    for build in real_builds():
        loadout = build.get("talentHash")
        assert loadout, f"{build['id']} carries no loadout string"
        reader = _BitReader(loadout)
        assert reader.read(8) == 2, f"{build['id']} is not serialization version 2"
        spec_id = reader.read(16)
        seen.setdefault((build["class"], build["spec"]), set()).add(spec_id)

    for key, ids in seen.items():
        assert len(ids) == 1, f"{key} decoded to several spec ids: {ids}"
        expected = EXPECTED_SPEC_IDS.get(key)
        if expected is not None:
            assert next(iter(ids)) == expected, f"{key} decoded to {ids}, expected {expected}"


@pytest.mark.skipif(not real_builds(), reason="no committed MID2 dataset")
def test_every_class_in_the_dataset_has_a_simc_class_id():
    """`nodes_for_class` silently returns nothing for a class it cannot name, which
    would publish an empty tree rather than an error."""
    for build in real_builds():
        assert build["class"] in CLASS_IDS


# --------------------------------------------------------------------------------
# The node stream, against a hand-built tree
# --------------------------------------------------------------------------------


def trait(node_id, entry_id, tree=TREE_CLASS, **kw):
    return Trait(
        tree_index=tree,
        class_id=1,
        entry_id=entry_id,
        node_id=node_id,
        max_ranks=kw.get("max_ranks", 1),
        req_points=0,
        spell_id=kw.get("spell_id", 1000 + entry_id),
        row=kw.get("row", 1),
        col=kw.get("col", 1),
        selection_index=kw.get("selection_index", 100),
        name=kw.get("name", f"Talent {entry_id}"),
        spec_ids=kw.get("spec_ids", ()),
        sub_tree=kw.get("sub_tree", 0),
        node_type=kw.get("node_type", 0),
    )


def encode(bits: list[int]) -> str:
    """Pack a bit list the way Blizzard's exporter does, so the reader has something
    independent to read. Written here rather than in the module because the module
    only ever needs to read."""
    text = ""
    for start in range(0, len(bits), 6):
        chunk = bits[start : start + 6]
        value = sum(bit << index for index, bit in enumerate(chunk))
        text += BASE64[value]
    return text


def header_bits(spec_id: int) -> list[int]:
    bits = [(2 >> i) & 1 for i in range(8)]
    bits += [(spec_id >> i) & 1 for i in range(16)]
    bits += [0] * 128
    return bits


def test_a_selected_purchased_node_takes_all_its_ranks():
    nodes = {10: [trait(10, 100, max_ranks=2)]}
    loadout = decode_loadout(encode(header_bits(62) + [1, 1, 0, 0]), nodes)
    assert loadout.spec_id == 62
    assert [(s.node_id, s.rank) for s in loadout.selections] == [(10, 2)]


def test_a_granted_node_is_rank_one_not_max():
    """The 'purchased' bit is off: the game gave it, so it sits at one rank."""
    nodes = {10: [trait(10, 100, max_ranks=3)]}
    loadout = decode_loadout(encode(header_bits(62) + [1, 0]), nodes)
    assert loadout.selections[0].rank == 1


def test_a_partially_ranked_node_reads_its_rank_from_the_stream():
    nodes = {10: [trait(10, 100, max_ranks=3)]}
    rank_bits = [(2 >> i) & 1 for i in range(6)]
    loadout = decode_loadout(encode(header_bits(62) + [1, 1, 1] + rank_bits + [0]), nodes)
    assert loadout.selections[0].rank == 2


def test_a_choice_node_takes_the_entry_the_index_names():
    nodes = {
        10: [
            trait(10, 100, name="First", node_type=2),
            trait(10, 101, name="Second", node_type=2),
        ]
    }
    loadout = decode_loadout(encode(header_bits(62) + [1, 1, 0, 1, 1, 0]), nodes)
    assert loadout.selections[0].name == "Second"
    assert loadout.selections[0].entry_id == 101


def test_a_choice_index_past_the_end_is_refused_rather_than_wrapped():
    nodes = {10: [trait(10, 100, node_type=2)]}
    with pytest.raises(TalentDecodeError):
        decode_loadout(encode(header_bits(62) + [1, 1, 0, 1, 1, 1]), nodes)


def test_nodes_are_read_in_ascending_node_id_order():
    """simc keys them in a std::map, so this ordering *is* the stream format. Getting
    it wrong desynchronises everything after the first difference."""
    nodes = {
        30: [trait(30, 300, name="Third")],
        10: [trait(10, 100, name="First")],
        20: [trait(20, 200, name="Second")],
    }
    loadout = decode_loadout(encode(header_bits(62) + [1, 1, 0, 0, 0, 1, 1, 0, 0]), nodes)
    assert [s.name for s in loadout.selections] == ["First", "Third"]


def test_a_wrong_serialization_version_is_refused():
    bits = [(3 >> i) & 1 for i in range(8)] + [0] * 144
    with pytest.raises(TalentDecodeError):
        decode_loadout(encode(bits), {})


def test_hero_nodes_are_narrowed_to_the_chosen_sub_tree():
    """A hero node can belong to two trees, so the SELECTION node is what names one.
    Without this the point total counts talents from a tree nobody plays."""
    nodes = {
        10: [trait(10, 100, tree=TREE_HERO, sub_tree=33, name="Kept")],
        20: [trait(20, 200, tree=TREE_HERO, sub_tree=32, name="Other tree")],
        30: [trait(30, 300, tree=4, sub_tree=33, name="0")],
    }
    loadout = decode_loadout(encode(header_bits(251) + [1, 1, 0, 0] * 3), nodes)
    assert loadout.sub_tree == 33
    assert [s.name for s in loadout.in_tree(TREE_HERO)] == ["Kept"]
    assert loadout.points(TREE_HERO) == 1


# --------------------------------------------------------------------------------
# Reading simc's table, and what the view is handed
# --------------------------------------------------------------------------------


def test_parse_trait_data_reads_the_generated_row_layout(tmp_path):
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "trait_data.inc").write_text(
        "// Player trait definitions, wow build 12.1.0.69299\n"
        "static constexpr std::array<trait_data_t, 2> __trait_data_data { {\n"
        "  { 3,  6, 117659,  95062, 1,  1, 122671,  439843,      0,      0,  1,  3, 100, "
        '"Reaper\'s Mark", {  250,  251,    0,    0 }, {    0,    0,    0,    0 },  33, 0 },\n'
        "  { 6,  6, 112112,  90261, 1,  0, 117117,  386164,      0,      0,  1,  2, 200, "
        '"A Runeforge", {   73,    0,    0,    0 }, {   73,    0,    0,    0 },   0, 0 },\n'
        "} };\n",
        encoding="utf-8",
    )
    traits = parse_trait_data(tmp_path)
    assert len(traits) == 2
    hero = traits[0]
    assert hero.name == "Reaper's Mark"
    assert (hero.node_id, hero.entry_id, hero.spell_id) == (95062, 117659, 439843)
    assert (hero.row, hero.col) == (1, 3)
    assert hero.sub_tree == 33
    assert hero.spec_ids == (250, 251)
    # tree_index 6 is EXPANSION -- not a player trait, and including it in the node
    # stream desynchronises the whole decode.
    assert traits[1].is_player_trait is False
    assert list(nodes_for_class(traits, 6)) == [95062]


def test_the_layout_carries_the_nodes_a_build_did_not_take():
    """A view that draws only the taken nodes is a list. What was passed over is most
    of what a reader is looking at."""
    nodes = {
        10: [trait(10, 100, tree=TREE_CLASS, name="Taken")],
        20: [trait(20, 200, tree=TREE_SPEC, name="Passed over")],
    }
    layout = tree_layout(nodes, spec_id=62, sub_tree=None)
    assert {node["id"] for node in layout} == {10, 20}


def test_the_layout_drops_another_specs_branch():
    nodes = {
        10: [trait(10, 100, tree=TREE_SPEC, spec_ids=(62,), name="Arcane only")],
        20: [trait(20, 200, tree=TREE_SPEC, spec_ids=(63,), name="Fire only")],
    }
    layout = tree_layout(nodes, spec_id=62, sub_tree=None)
    assert [node["id"] for node in layout] == [10]


def test_the_layout_drops_hero_trees_the_build_does_not_play():
    nodes = {
        10: [trait(10, 100, tree=TREE_HERO, sub_tree=33)],
        20: [trait(20, 200, tree=TREE_HERO, sub_tree=32)],
    }
    assert [node["id"] for node in tree_layout(nodes, 251, sub_tree=33)] == [10]
