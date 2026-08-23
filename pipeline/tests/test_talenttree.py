"""The loadout decode, checked against real hashes and hand-built streams.

The strongest checks available offline do not need simc's trait table at all: the
header of a real loadout string carries the specialization id, and the dataset already
says which spec each build is. Fifteen correct spec ids cannot come out of a wrong
alphabet or a wrong bit order.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from wowdps import profiles
from wowdps.talenttree import (
    BASE64,
    CLASS_IDS,
    TREE_CLASS,
    TREE_HERO,
    TREE_SPEC,
    Loadout,
    Selection,
    TalentDecodeError,
    TalentEncodeError,
    Trait,
    _BitReader,
    _generated,
    decode_loadout,
    encode_loadout,
    max_ranks_of,
    nodes_for_class,
    parse_sub_tree_names,
    parse_trait_data,
    spec_rule_violation,
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


def test_the_layout_counts_a_tiered_nodes_ranks_the_way_the_decoder_does():
    """One rule, one expression. ``max_ranks_of`` says in its own docstring that getting
    the tiered case wrong moves the partially ranked bit and desynchronises everything
    after it, and every other reader of the rule routes through it; the layout kept an
    inline copy, on the one path the site actually draws."""
    nodes = {
        10: [
            trait(10, 100, node_type=1, max_ranks=1, name="Tier A"),
            trait(10, 101, node_type=1, max_ranks=2, name="Tier B"),
        ],
        20: [trait(20, 200, node_type=2, max_ranks=1), trait(20, 201, node_type=2, max_ranks=1)],
    }
    layout = {node["id"]: node["maxRanks"] for node in tree_layout(nodes, 62, sub_tree=None)}
    assert layout == {10: 3, 20: 1}
    assert layout[10] == max_ranks_of(nodes[10])


def test_the_layout_drops_hero_trees_the_build_does_not_play():
    nodes = {
        10: [trait(10, 100, tree=TREE_HERO, sub_tree=33)],
        20: [trait(20, 200, tree=TREE_HERO, sub_tree=32)],
    }
    assert [node["id"] for node in tree_layout(nodes, 251, sub_tree=33)] == [10]


# --------------------------------------------------------------------------------
# Hero tree names, which simc did not always ship
# --------------------------------------------------------------------------------

SUB_TREE_TABLE = (
    "#define MAX_HERO_TREES_PER_CLASS (4)\n"
    "\n"
    "// Hero trees, wow build 12.1.0.69404\n"
    "static constexpr std::array<std::tuple<unsigned, const char*, unsigned>, 3> "
    "__trait_sub_tree_data { {\n"
    '  { 24, "Elune\'s Chosen", 11 },\n'
    '  { 51, "Trickster", 4 },\n'
    '  { 65, "Shado-Pan", 10 },\n'
    "} };\n"
)


def write_trait_file(root: Path, name: str, body: str) -> None:
    generated = root / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / name).write_text(body, encoding="utf-8")


def test_the_hero_tree_names_come_out_of_simcs_own_table(tmp_path):
    """The whole reason a tree nobody plays can be named at all."""
    write_trait_file(tmp_path, "trait_data.inc", SUB_TREE_TABLE)
    names = parse_sub_tree_names(tmp_path)
    assert {key: entry.name for key, entry in names.items()} == {
        24: "Elune's Chosen",
        51: "Trickster",
        65: "Shado-Pan",
    }
    assert names[65].class_id == 10


def test_the_ptr_file_names_the_array_differently_and_is_still_read(tmp_path):
    """simc calls it `__ptr_trait_sub_tree_data` in the PTR file, and **the current
    tier runs in PTR mode**. Anchoring on the live array name found nothing there, so
    the PTR names were never read; it went unnoticed only because the two tables'
    rows are byte-identical on 22b442e. A PTR-only or renamed tree would have come
    back unnamed or stale -- the regression this module exists to prevent."""
    write_trait_file(tmp_path, "trait_data.inc", SUB_TREE_TABLE)
    write_trait_file(
        tmp_path,
        "trait_data_ptr.inc",
        SUB_TREE_TABLE.replace("__trait_sub_tree_data", "__ptr_trait_sub_tree_data").replace(
            '"Trickster"', '"Trickster on PTR"'
        ),
    )
    # The PTR name, not the live one: proof the PTR file was actually read rather
    # than quietly fallen back from.
    assert parse_sub_tree_names(tmp_path, ptr=True)[51].name == "Trickster on PTR"
    assert parse_sub_tree_names(tmp_path)[51].name == "Trickster"


def test_a_checkout_without_the_table_names_nothing_rather_than_raising(tmp_path):
    write_trait_file(tmp_path, "trait_data.inc", "// an older checkout\n")
    assert parse_sub_tree_names(tmp_path) == {}


# --------------------------------------------------------------------------------
# simc's own refusal, reproduced offline
# --------------------------------------------------------------------------------


def test_a_node_the_spec_may_not_take_is_refused_in_simcs_own_words():
    """The wording and the ids are simc's: its 2026-08-22 CI log says "Selected node
    110203 entry 136735 is not available to player's spec" for Arms Warrior, and this
    is what lets the site say *which* profile will not load without running simc."""
    nodes = {
        10: [trait(10, 100, spec_ids=(71,))],
        20: [trait(20, 200, spec_ids=(72,))],
    }
    loadout = decode_loadout(encode(header_bits(71) + [1, 1, 0, 0, 1, 1, 0, 0]), nodes)
    assert spec_rule_violation(loadout, nodes) == (
        "Selected node 20 entry 200 is not available to player's spec"
    )


def test_a_node_with_no_spec_restriction_is_available_to_everyone():
    nodes = {10: [trait(10, 100, spec_ids=())]}
    loadout = decode_loadout(encode(header_bits(71) + [1, 1, 0, 0]), nodes)
    assert spec_rule_violation(loadout, nodes) is None


def test_the_spec_rule_does_not_apply_to_hero_nodes():
    """simc exempts them, and it has to: a hero node can belong to two trees and
    carries the specs of both."""
    nodes = {10: [trait(10, 100, tree=TREE_HERO, sub_tree=33, spec_ids=(250,))]}
    loadout = decode_loadout(encode(header_bits(251) + [1, 1, 0, 0]), nodes)
    assert spec_rule_violation(loadout, nodes) is None


# --------------------------------------------------------------------------------
# The encoder: the inverse of the decode, and the thing a talent search needs
# --------------------------------------------------------------------------------


def rank_bits(rank: int) -> list[int]:
    return [(rank >> i) & 1 for i in range(6)]


def test_encoding_a_decoded_loadout_reproduces_the_string_exactly():
    """The property the whole feature rests on. Anything that reads back the same
    build but writes different bytes will drift a profile the first time a sweep
    round-trips one."""
    nodes = {
        10: [trait(10, 100, max_ranks=2)],
        20: [trait(20, 200, node_type=2), trait(20, 201, node_type=2)],
        30: [trait(30, 300, max_ranks=3)],
    }
    original = encode(
        header_bits(62)
        + [1, 1, 0, 0]  # node 10: purchased, full rank, no choice
        + [1, 1, 0, 1, 1, 0]  # node 20: choice index 1
        + [1, 1, 1]
        + rank_bits(2)
        + [0]  # node 30: partial rank 2 of 3
    )
    loadout = decode_loadout(original, nodes)
    assert encode_loadout(loadout, nodes) == original


def test_the_tree_hash_is_written_as_zeros():
    """simc's own exporter does this -- ``put_bit( tree_bits, 0 )``, commented
    "0-filled to bypass validation, as GetTreeHash() is unavailable externally". All 85
    shipped MID1+MID2 hashes carry zeros there, so nothing is lost by not having it."""
    nodes = {10: [trait(10, 100)]}
    written = encode_loadout(decode_loadout(encode(header_bits(62) + [1, 1, 0, 0]), nodes), nodes)
    reader = _BitReader(written)
    reader.read(8)
    reader.read(16)
    assert reader.read(128) == 0


def test_a_granted_node_keeps_its_missing_purchased_bit():
    """A node the game grants is *selected* without being *purchased*. Writing the
    purchased bit anyway would add a partial-rank bit and a choice bit behind it and
    desynchronise every node after it -- and 277 of the 6,422 selected records in the
    shipped profiles are granted ones, so this is the common case, not a corner."""
    nodes = {10: [trait(10, 100, max_ranks=3)], 20: [trait(20, 200)]}
    original = encode(header_bits(62) + [1, 0] + [1, 1, 0, 0])
    loadout = decode_loadout(original, nodes)
    assert loadout.selections[0].purchased is False
    assert loadout.selections[0].rank == 1
    assert encode_loadout(loadout, nodes) == original


def test_the_hero_selection_node_is_written_with_the_choice_bit():
    """The fact that costs a hero tree if it is missed. simc's exporter sets
    ``is_choice`` for ``NODE_CHOICE`` **or** ``NODE_SELECTION``, so the sub-tree
    selection node carries a choice index like any other choice node. An encoder that
    tested only ``NODE_CHOICE`` writes no index, and the build silently reverts to
    whichever hero tree the node happens to list first."""
    nodes = {
        30: [
            trait(30, 300, tree=4, sub_tree=52, node_type=3, name="0"),
            trait(30, 301, tree=4, sub_tree=51, node_type=3, name="0"),
        ]
    }
    original = encode(header_bits(260) + [1, 1, 0, 1, 1, 0])
    loadout = decode_loadout(original, nodes)
    assert loadout.sub_tree == 51
    written = encode_loadout(loadout, nodes)
    assert written == original
    assert decode_loadout(written, nodes).sub_tree == 51


def test_a_plain_node_is_written_without_a_choice_bit():
    nodes = {10: [trait(10, 100)]}
    original = encode(header_bits(62) + [1, 1, 0, 0])
    assert encode_loadout(decode_loadout(original, nodes), nodes) == original


def test_the_partial_bit_is_derived_from_the_rank_when_it_was_not_recorded():
    """What every hand-built and mutated selection relies on: ``partial=None`` means
    "work it out from the rank", which is exactly what simc's exporter does."""
    nodes = {10: [trait(10, 100, max_ranks=3)]}
    full = Selection(
        node_id=10,
        entry_id=100,
        name="T",
        spell_id=1,
        rank=3,
        tree_index=TREE_CLASS,
        sub_tree=0,
        row=1,
        col=1,
        node_type=0,
        max_ranks=3,
    )
    part = replace(full, rank=2)
    empty = Loadout(version=2, spec_id=62, selections=(), spare_bits=0)
    assert encode_loadout(replace(empty, selections=(full,)), nodes) == encode(
        header_bits(62) + [1, 1, 0, 0]
    )
    assert encode_loadout(replace(empty, selections=(part,)), nodes) == encode(
        header_bits(62) + [1, 1, 1] + rank_bits(2) + [0]
    )


def test_a_recorded_partial_bit_that_disagrees_with_the_rank_is_still_reproduced():
    """One shipped MID1 profile writes the partial bit on a node that holds all its
    ranks. simc refuses that string, but the encoder is the *inverse of the decoder*,
    not a corrector: re-encoding has to give back what was read, or the refusal moves
    from simc's parser to a diff nobody can explain."""
    nodes = {10: [trait(10, 100, max_ranks=2)]}
    original = encode(header_bits(62) + [1, 1, 1] + rank_bits(2) + [0])
    loadout = decode_loadout(original, nodes)
    assert loadout.selections[0].partial is True
    assert loadout.selections[0].rank == loadout.selections[0].max_ranks
    assert encode_loadout(loadout, nodes) == original


def test_a_source_that_carried_a_longer_tail_is_reproduced():
    """Three shipped MID2 profiles -- all three Hunters -- carry a whole extra
    character of zeros past the node stream, because the exporter knew nodes simc's
    trait table does not. The bits mean nothing; reproducing them is what makes those
    three round-trip byte-identically."""
    nodes = {10: [trait(10, 100)]}
    shortest = encode(header_bits(62) + [1, 1, 0, 0])
    padded = shortest + "A"
    loadout = decode_loadout(padded, nodes)
    assert encode_loadout(loadout, nodes) == padded
    assert encode_loadout(loadout, nodes, preserve_framing=False) == shortest


def test_a_source_that_stopped_before_the_node_stream_did_is_reproduced():
    """The mirror case, and one MID1 profile is in it: the string ends before the node
    stream does and the reader supplies zeros for the rest. Re-encoding without the
    framing writes those zeros out and produces a longer -- equivalent, but not
    identical -- string."""
    nodes = {node_id: [trait(node_id, node_id * 10)] for node_id in range(10, 110, 10)}
    full = encode(header_bits(62) + [1, 1, 0, 0] + [0] * 9)
    original = full[:-1]
    loadout = decode_loadout(original, nodes)
    assert [s.node_id for s in loadout.selections] == [10]
    assert loadout.spare_bits < 0, "this case is a string shorter than its own node stream"
    assert encode_loadout(loadout, nodes) == original
    assert encode_loadout(loadout, nodes, preserve_framing=False) == full


def test_framing_never_truncates_a_bit_that_carries_a_selection():
    """The guard that keeps the framing replay from corrupting a mutation. A build that
    takes a node late in the tree needs more room than the source had, and the right
    answer is a longer string -- never a shorter one that reads back as a different
    build."""
    nodes = {node_id: [trait(node_id, node_id * 10)] for node_id in range(10, 110, 10)}
    original = encode(header_bits(62) + [1, 1, 0, 0] + [0] * 9)[:-1]
    loadout = decode_loadout(original, nodes)
    grown = replace(
        loadout,
        selections=loadout.selections
        + (
            Selection(
                node_id=100,
                entry_id=1000,
                name="Late",
                spell_id=1,
                rank=1,
                tree_index=TREE_CLASS,
                sub_tree=0,
                row=1,
                col=1,
                node_type=0,
                max_ranks=1,
            ),
        ),
    )
    written = encode_loadout(grown, nodes)
    assert [s.node_id for s in decode_loadout(written, nodes).selections] == [10, 100]


def test_encoding_refuses_a_node_the_class_does_not_have():
    nodes = {10: [trait(10, 100)]}
    stray = Selection(
        node_id=99,
        entry_id=990,
        name="Elsewhere",
        spell_id=1,
        rank=1,
        tree_index=TREE_CLASS,
        sub_tree=0,
        row=1,
        col=1,
        node_type=0,
        max_ranks=1,
    )
    with pytest.raises(TalentEncodeError):
        encode_loadout(Loadout(version=2, spec_id=62, selections=(stray,), spare_bits=0), nodes)


def test_encoding_refuses_a_rank_too_large_for_its_field():
    """Six bits of rank. Writing 64 would drop the high bit and silently encode rank
    zero -- a different build that looks like a successful export."""
    nodes = {10: [trait(10, 100, max_ranks=70)]}
    over = Selection(
        node_id=10,
        entry_id=100,
        name="T",
        spell_id=1,
        rank=64,
        tree_index=TREE_CLASS,
        sub_tree=0,
        row=1,
        col=1,
        node_type=0,
        max_ranks=70,
    )
    with pytest.raises(TalentEncodeError):
        encode_loadout(Loadout(version=2, spec_id=62, selections=(over,), spare_bits=0), nodes)


def test_encoding_refuses_a_choice_index_past_the_last_entry():
    """Two bits of field width is not the only bound. An index that fits the field but
    names no entry writes a string ``decode_loadout`` raises on and simc refuses with
    "Index 2 for choice node 20 out of bounds." -- so the refusal belongs here, where
    the loadout that caused it is still in hand, rather than one round trip later."""
    nodes = {20: [trait(20, 200, node_type=2), trait(20, 201, node_type=2)]}
    original = encode(header_bits(62) + [1, 1, 0, 1, 1, 0])
    loadout = decode_loadout(original, nodes)
    past_the_end = replace(loadout, selections=(replace(loadout.selections[0], choice_index=2),))
    with pytest.raises(TalentEncodeError, match="out of bounds"):
        encode_loadout(past_the_end, nodes)


def test_encoding_refuses_the_same_node_twice():
    nodes = {10: [trait(10, 100)]}
    one = Selection(
        node_id=10,
        entry_id=100,
        name="T",
        spell_id=1,
        rank=1,
        tree_index=TREE_CLASS,
        sub_tree=0,
        row=1,
        col=1,
        node_type=0,
        max_ranks=1,
    )
    with pytest.raises(TalentEncodeError):
        encode_loadout(Loadout(version=2, spec_id=62, selections=(one, one), spare_bits=0), nodes)


# --------------------------------------------------------------------------------
# The whole shipped corpus, when a simc checkout is at hand
# --------------------------------------------------------------------------------


def simc_checkout() -> tuple[Path, bool] | None:
    """A real simc checkout and which of its two trait tables to read.

    The trait table is 686 KB of generated C++ and is not committed here, so the only
    honest way to run the corpus check is against a checkout somebody points at with
    ``WOWDPS_SIMC_DIR``. The hand-built tests above cover the format; this one covers
    *simc's actual data*, which is the thing that changes under us.

    The table is chosen by which file is there rather than pinned. ``ptr=True`` was
    pinned, and simc's live branch ships ``generated/`` without ``trait_data_ptr.inc``,
    so a live checkout raised ``FileNotFoundError`` in the middle of the test -- which
    reads as a broken encoder rather than as an unrunnable check. PTR is preferred
    because that is the table the current tier's profiles are written against
    (``manifest.simc.ptr``, which is how every production caller picks); with neither
    file present there is nothing to run and the test skips.
    """
    raw = os.environ.get("WOWDPS_SIMC_DIR")
    if not raw:
        return None
    root = Path(raw)
    if not (root / "profiles").is_dir():
        return None
    for ptr in (True, False):
        if _generated(root, "trait_data", ptr).is_file():
            return root, ptr
    return None


@pytest.mark.skipif(simc_checkout() is None, reason="set WOWDPS_SIMC_DIR to a simc checkout")
def test_every_shipped_profile_round_trips_byte_identically():
    """The correctness bar, against every talent hash simc ships in either tier.

    Measured on simc 69a46e1, 2026-08-23: **72 of 72 decodable hashes come back byte
    for byte** -- all 35 MID2 profiles and 37 of MID1's 50. The other 13 MID1 profiles
    raise ``TalentDecodeError`` and so have no round trip to test; that is the tier rot
    this project already documents, where a stored hash no longer fits the current tree
    and the bit stream desynchronises.

    The corpus comes through ``profiles.discover``, which is the route both production
    callers use. Re-deriving it here -- a copy of ``_CLASS_LINE``, a copy of
    ``_TALENTS_LINE`` and a glob -- meant that a generator convention this project has
    already been caught by once (the unquoted player line) would be fixed in
    ``profiles.py`` while this copy silently matched fewer profiles, and a shrinking
    corpus reads exactly like a corpus that shrank.
    """
    root, ptr = simc_checkout()
    traits = parse_trait_data(root, ptr=ptr)

    # One pass over the trait list per class, not per profile: 85 profiles regrouping
    # the same 13 classes is the slowest thing in the suite. Both production callers
    # cache it the same way.
    nodes_by_class: dict[int, dict[int, list[Trait]]] = {}
    checked = undecodable = 0
    for tier in ("MID1", "MID2"):
        for profile in profiles.discover(root / "profiles", tier, dps_only=False):
            class_id = CLASS_IDS.get(profile.wow_class)
            if not class_id or not profile.talent_hash:
                continue
            nodes = nodes_by_class.setdefault(class_id, nodes_for_class(traits, class_id))
            original = profile.talent_hash
            try:
                loadout = decode_loadout(original, nodes)
            except TalentDecodeError:
                undecodable += 1
                continue
            assert encode_loadout(loadout, nodes) == original, profile.path.name
            checked += 1
    assert checked >= 70, f"only {checked} profiles round-tripped; expected the whole corpus"
    assert undecodable <= 15, f"{undecodable} profiles no longer decode -- has the tree moved?"
