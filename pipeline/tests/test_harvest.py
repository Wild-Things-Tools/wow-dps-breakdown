"""Harvesting builds off Warcraft Logs, pinned against hand-written payloads.

Nothing here reaches the network. Two things are therefore established by
construction rather than by measurement, and both are stated in the module under
test: the *shape* of ``playerDetails`` is Warcraft Logs' and is documented as not
frozen, and the point cost of a query is unknown until a live run reads it back.

What these tests do pin is everything between: which fields are read, which
observations are refused and why, that a refusal is published rather than dropped,
that two spellings of one build collapse to one, and that a quiet re-run leaves the
file alone.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from wowdps import equipment, harvest, talenttree, warcraftlogs
from wowdps.talenttree import Trait
from wowdps.warcraftlogs import talent_codes_query

# --------------------------------------------------------------------------------
# A tiny synthetic class tree, and an encoder for it
# --------------------------------------------------------------------------------

#: **Death Knight and Hunter rather than Mage, and the fixture is doing work here.**
#: Warcraft Logs spells a class and a spec without spaces and simc spells them with,
#: so a fixture whose class and spec are both one word cannot tell the two apart. A
#: `"type": "DeathKnight"` row is the same string on both sides -- which is how
#: `CLASS_IDS.get("DeathKnight")` -> None survived a whole feature: every Death
#: Knight and Demon Hunter observation came back `unknown_class`, every Beast
#: Mastery one `unknown_spec`, and `spec_key` emitted ids that join to nothing.
#:
#: So the default row is a multi-word **class** (`DeathKnight`) and the second
#: standard row is a multi-word **spec** (`BeastMastery`). Between them every
#: default fixture in this file exercises the fold.
DEATH_KNIGHT = talenttree.CLASS_IDS["Death Knight"]
HUNTER = talenttree.CLASS_IDS["Hunter"]
FROST, UNHOLY = 251, 252
BEAST_MASTERY, MARKSMANSHIP = 253, 254


def build_traits(class_id: int, main_spec: int, other_spec: int) -> list[Trait]:
    """Four nodes: two plain spec talents, one only ``other_spec`` may take, and the
    hero SELECTION node -- which is what names the hero tree and is written with the
    choice bit set."""

    def trait(node_id: int, entry_id: int, **overrides) -> Trait:
        base = dict(
            tree_index=talenttree.TREE_SPEC,
            class_id=class_id,
            entry_id=entry_id,
            node_id=node_id,
            max_ranks=1,
            req_points=0,
            spell_id=entry_id * 10,
            row=1,
            col=1,
            selection_index=0,
            name=f"Talent {entry_id}",
            spec_ids=(main_spec,),
            sub_tree=0,
            node_type=talenttree.NODE_NORMAL,
        )
        base.update(overrides)
        return Trait(**base)

    return [
        trait(100, 1000),
        trait(101, 1001, max_ranks=2),
        trait(102, 1002, spec_ids=(other_spec,)),
        trait(
            103,
            1003,
            tree_index=talenttree.TREE_SELECTION,
            node_type=talenttree.NODE_SELECTION,
            sub_tree=77,
            spec_ids=(),
            name="0",
        ),
        trait(
            103,
            1004,
            tree_index=talenttree.TREE_SELECTION,
            node_type=talenttree.NODE_SELECTION,
            sub_tree=88,
            spec_ids=(),
            name="0",
        ),
    ]


DK_NODES = talenttree.nodes_for_class(build_traits(DEATH_KNIGHT, FROST, UNHOLY), DEATH_KNIGHT)
HUNTER_NODES = talenttree.nodes_for_class(build_traits(HUNTER, BEAST_MASTERY, MARKSMANSHIP), HUNTER)


class _BitWriter:
    """The mirror image of ``talenttree._BitReader``.

    Written out here rather than imported because there is no encoder in the
    pipeline -- and because an independent encoder is worth more as a test than a
    shared one would be: a round trip through two separately written halves cannot
    agree by sharing a bug.
    """

    def __init__(self) -> None:
        self.bits: list[int] = []

    def write(self, value: int, width: int) -> None:
        for offset in range(width):
            self.bits.append((value >> offset) & 1)

    def text(self) -> str:
        out = []
        for start in range(0, len(self.bits), talenttree.CHAR_BITS):
            chunk = self.bits[start : start + talenttree.CHAR_BITS]
            value = sum(bit << offset for offset, bit in enumerate(chunk))
            out.append(talenttree.BASE64[value])
        return "".join(out)


def encode(spec_id: int, picks: dict[int, tuple[int, int]], version: int = 2, nodes=None) -> str:
    """``{node id: (choice index, rank)}`` -> a loadout string.

    Every selected node is written as purchased, partially ranked and choosing an
    entry, which is the longest of the encodings the reader accepts and therefore
    the one that exercises the most of it.
    """
    nodes = DK_NODES if nodes is None else nodes
    writer = _BitWriter()
    writer.write(version, talenttree.VERSION_BITS)
    writer.write(spec_id, talenttree.SPEC_BITS)
    writer.write(0, talenttree.TREE_BITS)  # simc writes the tree hash as zeros
    for node_id in sorted(nodes):
        if node_id not in picks:
            writer.write(0, 1)
            continue
        index, rank = picks[node_id]
        writer.write(1, 1)  # selected
        writer.write(1, 1)  # purchased
        writer.write(1, 1)  # partially ranked
        writer.write(rank, talenttree.RANK_BITS)
        writer.write(1, 1)  # choice
        writer.write(index, talenttree.CHOICE_BITS)
    return writer.text()


FROST_BUILD = {100: (0, 1), 101: (0, 2), 103: (0, 1)}
#: Node 102 is the one only the other spec may take, so this is a build the spec
#: rule lets through for Unholy and would refuse for Frost.
UNHOLY_BUILD = {102: (0, 1), 103: (0, 1)}
#: The multi-word *spec*. `BeastMastery` is what Warcraft Logs sends; simc says
#: `Beast Mastery` and the dataset's id is `hunter_beast_mastery`.
BEAST_MASTERY_BUILD = {100: (0, 1), 103: (0, 1)}


def tables() -> harvest.TalentTables:
    """simc's spelling on the left of every key, which is the whole point.

    `Beast Mastery` with a space, because that is what simc's own table says and
    what the dataset's `hunter_beast_mastery` is built from. Warcraft Logs will send
    `BeastMastery`; resolving between the two is `TalentTables.canonical_names`.
    """
    return harvest.TalentTables(
        nodes={DEATH_KNIGHT: DK_NODES, HUNTER: HUNTER_NODES},
        spec_ids={
            ("Death Knight", "Frost"): FROST,
            ("Death Knight", "Unholy"): UNHOLY,
            ("Hunter", "Beast Mastery"): BEAST_MASTERY,
            ("Hunter", "Marksmanship"): MARKSMANSHIP,
        },
        sub_trees={77: "Deathbringer", 88: "Rider of the Apocalypse"},
    )


def test_the_synthetic_encoder_round_trips_through_the_shipped_decoder():
    """Control. Everything else here rests on these hashes being real ones."""
    loadout = talenttree.decode_loadout(encode(FROST, FROST_BUILD), DK_NODES)
    assert loadout.version == 2
    assert loadout.spec_id == FROST
    assert {s.node_id: s.rank for s in loadout.selections} == {100: 1, 101: 2, 103: 1}
    assert loadout.sub_tree == 77
    # The stream is consumed to within the padding of a 6-bit boundary, which is the
    # same termination check `talenttree` uses against simc's real hashes.
    assert loadout.spare_bits < talenttree.CHAR_BITS


# --------------------------------------------------------------------------------
# The queries
# --------------------------------------------------------------------------------


def test_one_request_asks_for_every_actors_talent_code():
    """Two report-level queries per kill is the whole cost argument. If this ever
    becomes one request per actor, a twenty-player pull costs twenty."""
    query = talent_codes_query([12, 7, 3])
    assert query.count("talentImportCode") == 3
    assert query.count("query TalentCodes") == 1
    for actor in (12, 7, 3):
        assert f"a{actor}: talentImportCode(actorID: {actor})" in query


def test_an_actor_id_that_is_not_an_integer_cannot_reach_the_query_text():
    """The actor ids are the one payload value written into a document rather than
    passed as a variable, because the number of them varies per fight."""
    with pytest.raises((ValueError, TypeError)):
        talent_codes_query(["12) evil"])
    with pytest.raises(ValueError):
        talent_codes_query([])


# --------------------------------------------------------------------------------
# Reading playerDetails
# --------------------------------------------------------------------------------

GEAR_ENTRY = {
    "id": 250215,
    "itemLevel": 334,
    "permanentEnchant": 7967,
    "gems": [{"id": 240906, "itemLevel": 1}],
    "bonusIDs": [12854, 13440],
    "setID": 1900,
}

#: The default row. `type` and `icon` carry **Warcraft Logs'** spelling, unspaced,
#: because that is what the service sends -- see the note beside `DEATH_KNIGHT`.
DPS_ROW = {
    "name": "Somebody",
    "id": 12,
    "guid": 999,
    "type": "DeathKnight",
    "server": "Somewhere",
    "icon": "DeathKnight-Frost",
    "specs": [{"spec": "Frost", "role": "dps"}],
    "minItemLevel": 330,
    "maxItemLevel": 340,
    "combatantInfo": {"gear": [GEAR_ENTRY]},
}

#: The second standard row, and the multi-word **spec**: Warcraft Logs sends
#: `BeastMastery`, simc says `Beast Mastery`, and the dataset joins on
#: `hunter_beast_mastery`.
HUNTER_ROW = {
    "name": "Nobody",
    "id": 13,
    "guid": 998,
    "type": "Hunter",
    "server": "Elsewhere",
    "icon": "Hunter-BeastMastery",
    "specs": [{"spec": "BeastMastery", "role": "dps"}],
    "minItemLevel": 330,
    "maxItemLevel": 340,
    "combatantInfo": {"gear": [GEAR_ENTRY]},
}

#: The talent code each of the two standard rows hands back, encoded against its own
#: class's tree.
ROSTER_CODES = {
    12: lambda: encode(FROST, FROST_BUILD),
    13: lambda: encode(BEAST_MASTERY, BEAST_MASTERY_BUILD, nodes=HUNTER_NODES),
}


def details_payload(rows=None) -> dict:
    dps = list(rows if rows is not None else [DPS_ROW])
    return {"data": {"dps": dps, "healers": [], "tanks": []}}


def test_player_details_is_read_through_the_data_wrapper():
    rows, buckets = harvest.player_detail_rows(details_payload())
    assert [row["id"] for row in rows] == [12]
    assert buckets == ["dps", "healers", "tanks"]


def test_player_details_survives_arriving_as_a_json_string():
    rows, _ = harvest.player_detail_rows(json.dumps(details_payload()))
    assert len(rows) == 1


def test_the_buckets_that_were_present_are_reported_when_dps_is_not():
    """'the payload had no dps bucket' and 'the payload was not this shape at all'
    are different findings, and only the bucket list tells them apart."""
    rows, buckets = harvest.player_detail_rows({"data": {"healers": [], "tanks": []}})
    assert rows == []
    assert buckets == ["healers", "tanks"]


def test_a_spec_swap_mid_fight_is_refused_rather_than_guessed():
    """The talent code is one build and the damage is two, so neither name is right."""
    row = dict(DPS_ROW, specs=[{"spec": "Frost"}, {"spec": "Unholy"}])
    assert harvest.spec_of_row(row) is None
    assert harvest.spec_of_row(dict(DPS_ROW, specs=[])) == "Frost"  # falls back to the icon


# --------------------------------------------------------------------------------
# Gear
# --------------------------------------------------------------------------------


def test_gear_keeps_the_gem_and_the_enchant_and_the_simc_line_omits_bonus_ids():
    """Measured in this project: bonus ids are inert for rings and trinkets alike,
    an explicit ilevel overrides what they would set, and the enchant is worth
    twelve times a ten-item-level step. Dropping the gem or the enchant is the
    order-of-magnitude error; dropping the bonus ids costs nothing measurable."""
    pieces, skipped = harvest.gear_from_row(DPS_ROW)
    assert skipped == 0
    (piece,) = pieces
    assert piece.gem_ids == (240906,)
    assert piece.enchant_id == 7967
    assert piece.bonus_ids == (12854, 13440)

    resolved, _ = harvest.resolve_slots(pieces, {250215: equipment.INVENTORY_TYPES["trinket"]})
    line = resolved[0].simc_line()
    assert line == "trinket1=,id=250215,ilevel=334,gem_id=240906,enchant_id=7967"
    assert "bonus_id" not in line


#: A `playerDetails` dps row exactly as the live service returned it when
#: `includeCombatantInfo` was left at its default. Not invented: `combatantInfo` is
#: an **empty list**, and this is what fourteen of fourteen players looked like in CI
#: run 32660348582 on 2026-08-23.
NO_COMBATANT_INFO_ROW = dict(DPS_ROW, combatantInfo=[])


def test_an_empty_combatant_info_is_named_rather_than_printed_as_a_type():
    """`combatantInfo keys: list` is a type name standing where keys belong.

    That one line is why the first live probe was diagnosed as a shape this code
    could not parse, when it was an argument this code had not sent. The probe now
    has to say which of the two it is, in words.
    """
    described = harvest.describe_combatant_info(NO_COMBATANT_INFO_ROW)
    assert "empty list" in described
    assert "includeCombatantInfo" in described
    assert described != "list"

    # The control: a row that really does carry the block is described by its keys,
    # so the sentence above is about the empty case and not about every case.
    assert harvest.describe_combatant_info(DPS_ROW) == "dict, keys ['gear']"


def test_a_non_empty_combatant_info_list_is_described_and_not_parsed():
    """Nobody has ever seen one. A branch that read it would be a guess with the
    authority of code, so it is reported as unobserved instead."""
    row = dict(DPS_ROW, combatantInfo=[{"gear": [GEAR_ENTRY]}])
    described = harvest.describe_combatant_info(row)
    assert "never observed" in described
    assert harvest.gear_from_row(row) == ([], 0)


def test_a_row_with_no_gear_array_is_a_different_answer_from_a_bare_one():
    """`([], 0)` meant both "this player wears nothing" and "no gear was requested".

    `gear_entries_skipped` cannot tell them apart -- there were no entries to skip --
    so the whole failure published as builds with `simcGear: []` and every count in
    the file reading healthy.
    """
    assert harvest.gear_array(NO_COMBATANT_INFO_ROW) is None
    assert harvest.gear_array(DPS_ROW) == [GEAR_ENTRY]
    assert harvest.gear_array(dict(DPS_ROW, combatantInfo={"gear": []})) == []


def test_the_probe_says_how_many_rows_carried_no_gear_at_all():
    """The number that would have named blocker 1 in its own run output."""
    lines = harvest.probe_shapes(
        {"data": {"dps": [NO_COMBATANT_INFO_ROW, NO_COMBATANT_INFO_ROW], "healers": []}}, {}, None
    )
    printed = "\n".join(lines)
    assert "2 of 2 dps row(s) carry no gear array at all" in printed
    assert "includeCombatantInfo" in printed


#: A kit shaped like a real one: a head with no socket and no enchant, a ring with
#: both, a back with an enchant only. The keys are the ones the live payload used
#: on 2026-08-23.
HEAD_ENTRY = {"id": 250100, "itemLevel": 340, "slot": 0, "quality": 4, "name": "A Helm"}
RING_ENTRY = {
    "id": 250200,
    "itemLevel": 340,
    "slot": 10,
    "gems": [{"id": 240906}],
    "permanentEnchant": 7967,
}
BACK_ENTRY = {"id": 250300, "itemLevel": 340, "slot": 14, "permanentEnchant": 7331}


def _kit(*entries):
    return [dict(DPS_ROW, combatantInfo={"gear": list(entries)})]


def test_the_first_gear_entry_cannot_settle_whether_the_adornments_are_sent():
    """The correction that produced this function, stated as the test.

    A kit's first entry is the head slot: no socket, and usually no enchant. So an
    entry without `gems` or `permanentEnchant` is what you see whether Warcraft Logs
    omits an empty key, never sends the key, or sends it under another name -- and a
    conclusion drawn from that one entry is under-determined. Reading every entry is
    what separates them, because a Mythic raider's rings and back carry both.
    """
    printed = "\n".join(harvest.describe_gear_keys(_kit(HEAD_ENTRY)))
    assert "gems: 0 of 1 entries" in printed
    assert "absent from the whole kit" in printed

    # The same reader over a kit that has a ring and a back: the keys are there, and
    # the slot each sits on is named.
    printed = "\n".join(harvest.describe_gear_keys(_kit(HEAD_ENTRY, RING_ENTRY, BACK_ENTRY)))
    assert "gems: 1 of 3 entries carry the key (1 non-empty), on slot(s) 10" in printed
    assert "permanentEnchant: 2 of 3 entries carry the key (2 non-empty)" in printed
    assert "slot(s) 10, 14" in printed


def test_the_union_of_gear_keys_is_reported_not_one_entrys_keys():
    """An intersection would hide exactly the keys that matter, since they sit on
    two slots of sixteen. The union is what says whether they are ever sent."""
    lines = harvest.describe_gear_keys(_kit(HEAD_ENTRY, RING_ENTRY, BACK_ENTRY))
    union = next(line for line in lines if "union over" in line)
    assert "'gems'" in union and "'permanentEnchant'" in union
    assert "union over 3 entries of 1 dps row(s)" in union

    beyond = next(line for line in lines if "beyond the first entry's eight" in line)
    assert "'gems'" in beyond and "'permanentEnchant'" in beyond


def test_a_kit_whose_keys_are_only_the_first_entrys_eight_says_so():
    """The other outcome, and it has to be legible as a measured absence rather than
    as nothing having been checked."""
    lines = harvest.describe_gear_keys(_kit(HEAD_ENTRY, dict(HEAD_ENTRY, id=250101, slot=1)))
    beyond = next(line for line in lines if "beyond the first entry's eight" in line)
    assert beyond.endswith("none")


def test_the_probe_reads_every_gear_entry_not_the_first():
    printed = "\n".join(
        harvest.probe_shapes(
            {"data": {"dps": _kit(HEAD_ENTRY, RING_ENTRY, BACK_ENTRY), "healers": []}}, {}, None
        )
    )
    assert "first gear entry keys:" in printed
    assert "union over 3 entries" in printed
    # And that the reader gets them out. A key present in the payload and read
    # under a name Warcraft Logs does not use fails exactly as silently as a key
    # that is never sent, so the union alone does not close this.
    assert "parsed pieces: 3, of which 1 carry a gem and 2 an enchant" in printed


def test_an_empty_slot_is_counted_rather_than_ignored():
    row = {"combatantInfo": {"gear": [{"id": 0}, GEAR_ENTRY, "nonsense"]}}
    pieces, skipped = harvest.gear_from_row(row)
    assert [p.item_id for p in pieces] == [250215]
    assert skipped == 2


def test_the_slot_is_derived_from_simc_not_from_the_position_in_the_array():
    """Warcraft Logs hands gear back as a positional array whose meaning is stated
    nowhere in its schema. An item's slot is a property of the item, and simc ships
    it, so the index is evidence and never the answer."""
    inventory = {
        11: equipment.INVENTORY_TYPES["trinket"],
        22: equipment.INVENTORY_TYPES["neck"],
        33: equipment.INVENTORY_TYPES["finger"],
    }
    pieces = [harvest.GearPiece(index=i, item_id=item) for i, item in enumerate((33, 11, 22))]
    resolved, unresolved = harvest.resolve_slots(pieces, inventory)
    assert [p.slot for p in resolved] == ["finger1", "trinket1", "neck"]
    assert unresolved == ()


def test_paired_slots_are_numbered_by_order_of_appearance():
    inventory = {1: equipment.INVENTORY_TYPES["finger"], 2: equipment.INVENTORY_TYPES["finger"]}
    pieces = [harvest.GearPiece(index=0, item_id=1), harvest.GearPiece(index=1, item_id=2)]
    resolved, _ = harvest.resolve_slots(pieces, inventory)
    assert [p.slot for p in resolved] == ["finger1", "finger2"]


def test_a_dual_wielders_off_hand_is_not_published_as_a_main_hand():
    """Most modern one-handers are INVTYPE_WEAPON (13) for *both* copies, so a
    single slot name per inventory type produced `['main_hand=,id=111',
    'main_hand=,id=222']` and in a profile the second overwrote the first. A Rogue,
    Havoc, Fury, Enhancement or Frost Death Knight build -- several of them the
    unprofiled specs this exists for -- published wearing its off-hand as its main
    hand, with nothing in the document saying so."""
    pieces = [harvest.GearPiece(index=15, item_id=111), harvest.GearPiece(index=16, item_id=222)]
    resolved, unresolved = harvest.resolve_slots(pieces, {111: 13, 222: 13})
    assert [p.slot for p in resolved] == ["main_hand", "off_hand"]
    assert unresolved == ()


def test_an_item_that_can_only_be_an_off_hand_gets_the_off_hand():
    """A shield keeps the only socket it can occupy whichever end of the array it is
    at. This module's whole premise is that the array's order means nothing that was
    written down anywhere, so neither answer may depend on it."""
    pieces = [harvest.GearPiece(index=15, item_id=111), harvest.GearPiece(index=16, item_id=222)]
    resolved, _ = harvest.resolve_slots(pieces, {111: 13, 222: 14})
    assert [p.slot for p in resolved] == ["main_hand", "off_hand"]

    flipped = [harvest.GearPiece(index=15, item_id=222), harvest.GearPiece(index=16, item_id=111)]
    resolved, _ = harvest.resolve_slots(flipped, {111: 13, 222: 14})
    assert [p.slot for p in resolved] == ["off_hand", "main_hand"]


def test_a_flexible_weapon_cannot_crowd_out_one_that_has_only_one_socket():
    """The case the ordering exists for, and the one an "in array order" allocation
    gets wrong: a one-hander (either hand) listed *before* a main-hand-only weapon
    takes the main hand, and the item that cannot go anywhere else is left with
    nothing -- reported as unplaced while a socket it could have filled sits
    occupied by an item that had a second choice.

    Constrained items are therefore placed first. The sort is stable, so this
    changes nothing for two rings, two trinkets or two one-handers, which are the
    ordinary cases.
    """
    pieces = [harvest.GearPiece(index=15, item_id=111), harvest.GearPiece(index=16, item_id=222)]
    resolved, unresolved = harvest.resolve_slots(pieces, {111: 13, 222: 21})
    assert [p.slot for p in resolved] == ["off_hand", "main_hand"]
    assert unresolved == ()


def test_two_two_handers_are_both_placed_because_fury_carries_two():
    pieces = [harvest.GearPiece(index=15, item_id=1), harvest.GearPiece(index=16, item_id=2)]
    resolved, _ = harvest.resolve_slots(pieces, {1: 17, 2: 17})
    assert [p.slot for p in resolved] == ["main_hand", "off_hand"]


def test_a_third_weapon_is_reported_rather_than_overwriting_one():
    """There is no third hand. An item with no socket left cannot go in a profile,
    so it is reported for the same reason an unknown item is."""
    pieces = [harvest.GearPiece(index=i, item_id=i) for i in (1, 2, 3)]
    resolved, unresolved = harvest.resolve_slots(pieces, {1: 13, 2: 13, 3: 13})
    assert [p.slot for p in resolved] == ["main_hand", "off_hand", None]
    assert unresolved == (3,)


def test_the_shoulder_and_wrist_lines_are_readable_by_this_repositorys_own_reader():
    """simc accepts `shoulders=` and `shoulder=` alike and its shipped profiles write
    the plural -- measured 2026-08-23 -- so the emitted line is the plural. The trap
    is the other side: `gearpool._EQUIP_LINE`, which `equipped_item_ids` reads every
    profile through, accepted only the singular and matched 14 of MID2 Arcane Mage's
    16 gear lines."""
    from wowdps import gearpool

    pieces = [harvest.GearPiece(index=2, item_id=333), harvest.GearPiece(index=8, item_id=444)]
    resolved, _ = harvest.resolve_slots(pieces, {333: 3, 444: 9})
    lines = [piece.simc_line() for piece in resolved]
    assert lines == ["shoulders=,id=333", "wrists=,id=444"]
    for line in lines:
        assert gearpool._EQUIP_LINE.match(line), line


def test_an_item_simc_never_heard_of_gets_no_slot_and_is_reported():
    """It cannot be written into a profile, so claiming a slot for it would be a
    claim with nothing behind it -- the refusal `gearpool` already makes."""
    pieces = [harvest.GearPiece(index=0, item_id=424242)]
    resolved, unresolved = harvest.resolve_slots(pieces, {})
    assert resolved[0].slot is None
    assert resolved[0].simc_line() is None
    assert unresolved == (424242,)


# --------------------------------------------------------------------------------
# Validating and deduplicating
# --------------------------------------------------------------------------------


def observation(hash_string: str | None, spec: str = "Frost", **overrides) -> harvest.Observation:
    base = dict(
        report="aBcD1234",
        fight_id=7,
        actor_id=12,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        wow_class="Death Knight",
        spec=spec,
        item_level=340.0,
        talent_hash=hash_string,
    )
    base.update(overrides)
    return harvest.Observation(**base)


def test_warcraft_logs_spellings_resolve_to_simcs_before_anything_is_looked_up():
    """The join that decided whether two of the nine target specs were harvestable.

    Executed against the code this replaces: `CLASS_IDS.get("DeathKnight")` -> None
    and `CLASS_IDS.get("DemonHunter")` -> None, so **every** Death Knight and Demon
    Hunter observation came back `unknown_class` -- Havoc and Devourer being two of
    the specs the harvest exists for -- and `spec_ids.get(("Hunter", "BeastMastery"))`
    -> None, because simc says "Beast Mastery".
    """
    assert harvest.canonical_class("DeathKnight") == "Death Knight"
    assert harvest.canonical_class("DemonHunter") == "Demon Hunter"
    assert harvest.canonical_class("Mage") == "Mage"
    # Idempotent, so a caller that resolves twice is not punished for it.
    assert harvest.canonical_class("Death Knight") == "Death Knight"
    assert harvest.canonical_class("Kobold") is None

    assert tables().canonical_names("Hunter", "BeastMastery") == ("Hunter", "Beast Mastery")
    assert tables().canonical_names("DeathKnight", "Frost") == ("Death Knight", "Frost")


def test_the_published_spec_id_is_the_one_the_rest_of_the_dataset_joins_on():
    """`deathknight_frost` and `hunter_beastmastery` are ids nothing else in this
    repository has ever written. A file full of them looks complete and joins to
    nothing."""
    row = observation(encode(FROST, FROST_BUILD), wow_class="DeathKnight")
    assert observation(encode(FROST, FROST_BUILD)).spec_key == "death_knight_frost"
    # And the raw spelling is still validated, because `validate` resolves too --
    # so an observation built by hand somewhere downstream cannot lose the join.
    assert harvest.validate(row, tables()).reason == harvest.REASON_OK


def test_a_class_simc_does_not_name_is_still_reported_as_such():
    """The control: resolving must not turn every unknown into a plausible answer."""
    verdict = harvest.validate(
        observation(encode(FROST, FROST_BUILD), wow_class="Tinker"), tables()
    )
    assert verdict.reason == harvest.REASON_UNKNOWN_CLASS
    assert "Tinker" in verdict.detail


def test_a_resolvable_class_with_an_unknown_spec_reports_the_spec():
    """`unknown_class` and `unknown_spec` are different findings and the fallback
    has to keep them apart -- otherwise a new spec of a known class reads as the
    class having disappeared."""
    verdict = harvest.validate(
        observation(encode(FROST, FROST_BUILD), wow_class="DeathKnight", spec="Frostfire"),
        tables(),
    )
    assert verdict.reason == harvest.REASON_UNKNOWN_SPEC


def test_a_loadable_build_passes_all_four_checks():
    verdict = harvest.validate(observation(encode(FROST, FROST_BUILD)), tables())
    assert verdict.reason == harvest.REASON_OK
    assert verdict.loadout is not None


def test_a_hash_that_will_not_decode_is_reported_never_dropped():
    """The single most valuable thing this command can turn up: either the loadout
    format moved or the bit reader is desynchronised. A silent drop would present a
    thin harvest as a complete one."""
    document = harvest.build_document(
        "MID2", 5, [observation("!!!not base64!!!")], tables(), encounters=[]
    )
    (spec,) = document["specs"]
    assert spec["builds"] == []
    assert spec["killsHarvested"] == 1
    assert spec["killsUsable"] == 0
    (rejection,) = spec["rejected"]
    assert rejection["reason"] == harvest.REASON_DECODE
    assert rejection["report"] == "aBcD1234"
    assert document["coverage"]["rejectedTotal"] == 1


def test_a_wrong_serialization_version_is_reported_as_a_decode_failure():
    verdict = harvest.validate(observation(encode(FROST, FROST_BUILD, version=3)), tables())
    assert verdict.reason == harvest.REASON_DECODE
    assert "version 3" in verdict.detail


def test_a_missing_talent_code_is_its_own_finding():
    """`talentImportCode` is documented to be null for a non-player actor and for a
    pre-Dragonflight fight. Either would mean the actor selection is wrong, which is
    not the same as a player having run no talents."""
    verdict = harvest.validate(observation(None), tables())
    assert verdict.reason == harvest.REASON_NO_CODE


def test_a_hash_whose_spec_disagrees_with_the_log_is_rejected():
    """Two sources describing one character. A disagreement means one read is wrong,
    and pooling the build under either name would bury it."""
    verdict = harvest.validate(observation(encode(UNHOLY, FROST_BUILD)), tables())
    assert verdict.reason == harvest.REASON_SPEC_MISMATCH
    assert "252" in verdict.detail


def test_simcs_own_spec_rule_is_applied_offline():
    """Node 102 belongs to Fire. Harvesting a build simc will not load would waste
    the whole downstream sweep, so it is decided here rather than by running simc."""
    verdict = harvest.validate(observation(encode(FROST, {**FROST_BUILD, 102: (0, 1)})), tables())
    assert verdict.reason == harvest.REASON_SPEC_RULE
    assert "not available to player's spec" in verdict.detail


def test_a_spec_simc_does_not_name_is_reported_rather_than_assumed():
    verdict = harvest.validate(observation(encode(FROST, FROST_BUILD), spec="Frostfire"), tables())
    assert verdict.reason == harvest.REASON_UNKNOWN_SPEC


def test_two_hash_strings_for_one_loadout_are_one_build():
    """simc writes the 128-bit tree hash as zeros and skips it on parse, so two
    exports of one build need not be the same string. Keying on the string would
    report thirty copies of one build as thirty builds -- which is precisely the
    number this command exists to produce."""
    same = encode(FROST, FROST_BUILD)
    # A different tree hash, same selections: still one build.
    writer = _BitWriter()
    writer.write(2, talenttree.VERSION_BITS)
    writer.write(FROST, talenttree.SPEC_BITS)
    writer.write(1, talenttree.TREE_BITS)
    tail = _BitWriter()
    for node_id in sorted(DK_NODES):
        if node_id not in FROST_BUILD:
            tail.write(0, 1)
            continue
        index, rank = FROST_BUILD[node_id]
        tail.write(1, 1)
        tail.write(1, 1)
        tail.write(1, 1)
        tail.write(rank, talenttree.RANK_BITS)
        tail.write(1, 1)
        tail.write(index, talenttree.CHOICE_BITS)
    writer.bits.extend(tail.bits)
    other = writer.text()
    assert other != same

    builds, rejected = harvest.group_builds(
        [observation(same), observation(other, actor_id=13)], tables()
    )
    assert rejected == {}
    (build,) = builds["death_knight_frost"]
    assert build.seen_in == 2


def test_a_different_loadout_is_a_different_build():
    builds, _ = harvest.group_builds(
        [
            observation(encode(FROST, FROST_BUILD)),
            observation(encode(FROST, {100: (0, 1), 103: (1, 1)}), actor_id=13),
        ],
        tables(),
    )
    assert len(builds["death_knight_frost"]) == 2
    assert {b.hero_tree(tables())[1] for b in builds["death_knight_frost"]} == {
        "Deathbringer",
        "Rider of the Apocalypse",
    }


def test_the_distinct_build_count_is_published_per_spec():
    """That number is itself the finding: one means a settled spec, ten over ten
    kills means there is no consensus to harvest."""
    document = harvest.build_document(
        "MID2",
        5,
        [
            observation(encode(FROST, FROST_BUILD)),
            observation(encode(FROST, FROST_BUILD), actor_id=13),
            observation(encode(FROST, {100: (0, 1), 103: (1, 1)}), actor_id=14),
        ],
        tables(),
        encounters=[],
    )
    (spec,) = document["specs"]
    assert spec["distinctBuilds"] == 2
    assert [b["seenInKills"] for b in spec["builds"]] == [2, 1]
    assert document["coverage"]["buildsTotal"] == 2


# --------------------------------------------------------------------------------
# Honesty: difficulty, dates, what the file says about itself
# --------------------------------------------------------------------------------


def test_a_fight_of_another_difficulty_is_refused_rather_than_filtered():
    """A Heroic build and a Mythic build answer different questions. The rankings
    query is already scoped to one difficulty, so a fight arriving with another one
    means the scoping did not hold -- and dropping it quietly would hide that while
    still producing a plausible file."""
    with pytest.raises(harvest.DifficultyMixed):
        harvest.check_difficulty(4, 5, "fight 7 of aBcD1234")


def test_a_fight_that_states_no_difficulty_is_allowed_through():
    """Unknown is not the same as wrong, and the same three-way rule already governs
    `firstkills.kills_from_report`."""
    harvest.check_difficulty(None, 5, "fight 7")


def test_the_difficulty_and_the_date_range_travel_with_the_document():
    document = harvest.build_document(
        "MID2",
        5,
        [
            observation(encode(FROST, FROST_BUILD)),
            observation(encode(FROST, FROST_BUILD), actor_id=13, killed_at_ms=1_755_864_000_000),
        ],
        tables(),
        encounters=[{"id": 3470, "name": "Nek'zali the Soulcoiler"}],
    )
    assert document["source"]["difficulty"] == 5
    span = document["source"]["killedBetween"]
    assert span["first"] == "2025-08-12T12:00:00+00:00"
    assert span["spanDays"] == 10.0
    assert document["source"]["rankedParsesOnly"] is True
    assert "ranked parses" in document["source"]["note"]
    assert "not evidence that the build is optimal" in document["source"]["evidence"]


def test_the_published_source_can_be_reopened():
    """A row nobody can check is a row nobody has to believe."""
    document = harvest.build_document(
        "MID2", 5, [observation(encode(FROST, FROST_BUILD))], tables(), encounters=[]
    )
    (source,) = document["specs"][0]["builds"][0]["sources"]
    assert source["report"] == "aBcD1234"
    assert source["fightID"] == 7
    assert source["actorID"] == 12
    assert source["killedAt"].startswith("2025-08-12")


def test_no_character_or_server_name_is_written_to_disk():
    """The artefact is a build, not a person. The names are dropped at extraction
    rather than filtered at publication, so they never reach the document at all."""
    rows, _ = harvest.player_detail_rows(details_payload())
    found, _ = harvest.observations_from_fight(
        rows,
        {12: encode(FROST, FROST_BUILD)},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        inventory={250215: equipment.INVENTORY_TYPES["trinket"]},
        tables=tables(),
    )
    document = harvest.build_document("MID2", 5, found, tables(), encounters=[])
    blob = json.dumps(document)
    assert "Somebody" not in blob
    assert "Somewhere" not in blob
    # ... while the observation itself is complete enough to be re-opened.
    assert '"actorID": 12' in json.dumps(document, indent=2)


def test_a_kill_yields_every_damage_player_and_its_gear():
    """And the ids it yields are simc's, not Warcraft Logs'.

    The payload says `DeathKnight` and `BeastMastery`; the file has to say
    `death_knight_frost` and `hunter_beast_mastery`, because that is what the rest
    of the dataset joins on. Looked up in simc's spaced tables as they arrive, the
    first is an unknown class and the second an unknown spec, and both observations
    are thrown away with a rejection reason that reads like a finding about the
    game.
    """
    rows, buckets = harvest.player_detail_rows(details_payload([DPS_ROW, HUNTER_ROW]))
    found, unresolved = harvest.observations_from_fight(
        rows,
        {actor: make() for actor, make in ROSTER_CODES.items()},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        inventory={250215: equipment.INVENTORY_TYPES["trinket"]},
        tables=tables(),
    )
    assert [o.spec_key for o in found] == ["death_knight_frost", "hunter_beast_mastery"]
    assert [(o.wow_class, o.spec) for o in found] == [
        ("Death Knight", "Frost"),
        ("Hunter", "Beast Mastery"),
    ]
    assert buckets == ["dps", "healers", "tanks"]
    assert unresolved == ()
    assert found[0].gear[0].slot == "trinket1"
    assert found[0].item_level == 340.0
    # And both decode, which is the other half: an unknown class never reaches the
    # decoder at all.
    assert [harvest.validate(o, tables()).reason for o in found] == ["ok", "ok"]


def test_a_spec_filter_narrows_the_players_read_out_of_a_kill():
    """Per player. Which kills are *sampled* is `spec_targets`, tested with the sweep."""
    rows, _ = harvest.player_detail_rows(details_payload([DPS_ROW, HUNTER_ROW]))
    found, _ = harvest.observations_from_fight(
        rows,
        {},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="x",
        difficulty=5,
        killed_at_ms=1,
        inventory={},
        tables=tables(),
        only_specs=("hunter_beast_mastery",),
    )
    assert [o.spec_key for o in found] == ["hunter_beast_mastery"]


# --------------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------------


def test_a_counter_that_did_not_move_is_unmeasured_and_not_zero():
    """The exact mistake that shipped here once: "0 points for a nine-boss pass" is
    a number that reads as a measurement and is the absence of one."""
    lines = harvest.describe_cost(
        {
            "pointsSpentThisRun": 0.0,
            "limitPerHour": 3600,
            "firstReading": 1.0,
            "lastReading": 1.0,
        },
        harvest.QueryPlan(rankings=1, player_details=1, talent_codes=1),
        kills=1,
    )
    joined = "\n".join(lines)
    assert "UNMEASURED" in joined
    assert "not the same as free" in joined
    assert "points per sampled kill" not in joined


def test_a_run_served_from_the_cache_says_which_kind_of_unmeasured_it_is():
    """The bracketing readings are never cached, so they report the hour honestly --
    but a pass whose payload queries were all cache hits really did cost nothing, and
    no number of re-runs against that cache will say more. The workflow restores a
    previous run's cache, so the *second* probe dispatch is exactly when this
    happens, and from the output alone the two kinds of UNMEASURED look identical."""
    lines = harvest.describe_cost(
        {
            "pointsSpentThisRun": 0.0,
            "limitPerHour": 3600,
            "firstReading": 41.0,
            "lastReading": 41.0,
            "queries": 2,
            "cacheHits": 3,
        },
        harvest.QueryPlan(rankings=1, player_details=1, talent_codes=1),
        kills=1,
    )
    joined = "\n".join(lines)
    assert "UNMEASURED" in joined
    assert "3 of 5 queries came from the response cache" in joined
    assert "empty --cache directory" in joined


def test_an_extrapolated_full_pass_says_that_it_is_an_extrapolation():
    lines = harvest.describe_cost(
        {
            "pointsSpentThisRun": 20.0,
            "limitPerHour": 3600,
            "firstReading": 0.0,
            "lastReading": 20.0,
        },
        harvest.QueryPlan(rankings=1, player_details=2, talent_codes=2),
        kills=2,
    )
    joined = "\n".join(lines)
    assert "10.00 points per sampled kill (measured)" in joined
    assert "EXTRAPOLATION, not a measurement" in joined
    assert f"{10.0 * harvest.FULL_PASS_KILLS:.0f} points" in joined


def test_no_reading_at_all_is_reported_as_unmeasured_too():
    lines = harvest.describe_cost({"pointsSpentThisRun": None}, harvest.QueryPlan(), kills=0)
    assert "unmeasured" in "\n".join(lines)


def test_the_query_plan_counts_requests_and_says_they_are_not_points():
    plan = harvest.QueryPlan(rankings=8, player_details=10, talent_codes=10)
    assert plan.total == 30
    assert "not points" in plan.to_json()["note"]


# --------------------------------------------------------------------------------
# The queries against the shape they assume
# --------------------------------------------------------------------------------

#: The fields and arguments these queries touch.
#:
#: **`Report.playerDetails`, `WorldData.encounter` and `Encounter` are now
#: introspected from the live service** rather than transcribed from a mirror:
#: `wowdps wcl-schema --type Report --type Encounter`, CI run 32660759853,
#: 2026-08-23. That is what settled blocker 1 -- the mirrors this file was written
#: against do not carry `includeCombatantInfo` at all, so no amount of reading them
#: would have found it, and the live server states it with its default:
#:
#:     playerDetails: JSON
#:         ... killType: KillType = All
#:         translate: Boolean = true
#:         includeCombatantInfo: Boolean = false
#:
#: `ReportFight.talentImportCode` is still the mirrored shape and says so.
#: What this test pins either way is the shape the code assumes: a query that grows
#: an argument nobody has established exists fails here rather than in CI an hour
#: into a pass. It failed here when `includeCombatantInfo` was added, which is the
#: reason this comment could be written from a measurement.
SCHEMA_EXCERPT = """
scalar JSON
enum KillType { All Encounters Kills Wipes Trash }
type ReportFight {
  id: Int!
  talentImportCode(actorID: Int!): String
}
type Report {
  code: String!
  startTime: Float!
  fights(fightIDs: [Int]): [ReportFight]
  playerDetails(
    difficulty: Int
    encounterID: Int
    endTime: Float
    fightIDs: [Int]
    killType: KillType
    startTime: Float
    translate: Boolean
    includeCombatantInfo: Boolean
  ): JSON
}
type Encounter { id: Int! name: String! }
type WorldData { encounter(id: Int): Encounter }
type ReportData { report(code: String!): Report }
type RateLimitData { limitPerHour: Int! pointsSpentThisHour: Float! pointsResetIn: Int! }
type Query { reportData: ReportData worldData: WorldData rateLimitData: RateLimitData }
"""


def test_the_harvest_queries_match_the_schema_shape_they_assume():
    graphql = pytest.importorskip("graphql")
    from wowdps.warcraftlogs import ENCOUNTER_NAME_QUERY, PLAYER_DETAILS_QUERY

    schema = graphql.build_schema(SCHEMA_EXCERPT)
    for label, document in (
        ("playerDetails", PLAYER_DETAILS_QUERY),
        ("encounterName", ENCOUNTER_NAME_QUERY),
        ("talentImportCode", talent_codes_query([12, 7])),
    ):
        errors = graphql.validate(schema, graphql.parse(document))
        assert not errors, f"{label}: {[str(error) for error in errors]}"


def test_the_gear_query_asks_for_the_combatant_info_it_then_reads():
    """The argument, not the parse, was blocker 1.

    `includeCombatantInfo` defaults to **false** -- introspected, see above -- and
    the server then answers with every row's `combatantInfo` present and empty
    rather than absent. So the harvest read fourteen real players and published no
    gear at all, with every other number in the run healthy: 14 dps rows, 14 talent
    codes, "0 readable, 0 skipped" (CI run 32660348582, 2026-08-23).

    Asserted against the query text because that is where the defect was. A test of
    the parser cannot see this: the parser was right.
    """
    from wowdps.warcraftlogs import PLAYER_DETAILS_QUERY

    assert "includeCombatantInfo: true" in PLAYER_DETAILS_QUERY


# --------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------


def test_a_quiet_re_run_leaves_the_file_alone(tmp_path):
    """Same rule as the manifest and fights.json: a wall-clock stamp that rewrites
    itself every run means every run commits, and 'a diff means something moved'
    stops being true. `cost` travels with the stamp because it measures the run and
    not the game -- a harvest served out of a warm cache costs nothing and would
    otherwise rewrite the file to say so."""
    rows = [observation(encode(FROST, FROST_BUILD))]
    first = harvest.build_document(
        "MID2", 5, rows, tables(), encounters=[], ledger={"pointsSpentThisRun": 12.0}
    )
    path = harvest.write_harvested_builds(tmp_path, first)
    before = path.read_text()

    second = harvest.build_document(
        "MID2", 5, rows, tables(), encounters=[], ledger={"pointsSpentThisRun": 0.0}
    )
    second["generatedAt"] = "2099-01-01T00:00:00+00:00"
    harvest.write_harvested_builds(tmp_path, second)
    assert path.read_text() == before


def test_a_changed_harvest_does_rewrite_the_stamp(tmp_path):
    """The control. A settle that never lets go is a file that never updates."""
    path = harvest.write_harvested_builds(
        tmp_path,
        harvest.build_document(
            "MID2", 5, [observation(encode(FROST, FROST_BUILD))], tables(), encounters=[]
        ),
    )
    before = json.loads(path.read_text())
    changed = harvest.build_document(
        "MID2",
        5,
        [
            observation(encode(FROST, FROST_BUILD)),
            observation(encode(FROST, {100: (0, 1), 103: (1, 1)}), actor_id=14),
        ],
        tables(),
        encounters=[],
    )
    changed["generatedAt"] = "2099-01-01T00:00:00+00:00"
    harvest.write_harvested_builds(tmp_path, changed)
    after = json.loads(path.read_text())
    assert after["generatedAt"] == "2099-01-01T00:00:00+00:00"
    assert after["generatedAt"] != before["generatedAt"]


# --------------------------------------------------------------------------------
# The sweep, end to end, with no network
# --------------------------------------------------------------------------------


class StubClient:
    """Every call the sweep makes, counted.

    The count is the point. "Two report-level queries per sampled kill regardless of
    how many players are wanted out of it" is the whole affordability argument for
    this command, and it is the kind of claim that decays silently -- a loop moved
    one level in becomes one request per player, the sweep still works, and the cost
    goes up twentyfold with nothing to show for it.
    """

    def __init__(self, kills, roster, names=None, ranked_ids=None):
        self.kills = kills
        self.roster = roster
        self.calls: list[str] = []
        #: ``id -> name``. The default answers one name for every id, which is what
        #: every test written before PTR ids existed assumes.
        self.names = names
        #: The ids that have ranked parses. ``None`` means all of them, again so the
        #: older tests read unchanged.
        self.ranked_ids = ranked_ids

    def _name(self, encounter_id):
        if self.names is None:
            return "Nek'zali the Soulcoiler"
        return self.names.get(encounter_id)

    def _page(self, encounter_id):
        ranked = self.ranked_ids is None or encounter_id in self.ranked_ids
        return {
            "id": encounter_id,
            "name": self._name(encounter_id),
            "characterRankings": {
                "rankings": [
                    {
                        "startTime": 1_755_000_000_000 + index,
                        "report": {"code": code, "fightID": fight},
                    }
                    for index, (code, fight) in enumerate(self.kills)
                ]
                if ranked
                else []
            },
        }

    def encounter_name(self, encounter_id):
        self.calls.append(f"encounter-name:{encounter_id}")
        return self._name(encounter_id)

    def player_details(self, code, fight_id):
        self.calls.append(f"player-details:{code}:{fight_id}")
        return details_payload(self.roster)

    def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
        self.calls.append(f"rankings:{encounter_id}:{page}")
        return self._page(encounter_id)

    def spec_rankings(
        self, encounter_id, class_name, spec_name, difficulty=5, metric="dps", page=1
    ):
        self.calls.append(f"spec-rankings:{encounter_id}:{class_name}/{spec_name}:{page}")
        return self._page(encounter_id)

    def talent_import_codes(self, code, fight_id, actor_ids):
        self.calls.append(f"talent-codes:{code}:{fight_id}:{len(actor_ids)}")
        return {
            actor: ROSTER_CODES[actor]() if actor in ROSTER_CODES else encode(UNHOLY, UNHOLY_BUILD)
            for actor in actor_ids
        }

    #: `check_budget` reads this. A stub that reported no budget at all would be
    #: exercising a different branch than a real run does.
    ledger = warcraftlogs.PointLedger(limit_per_hour=3600, first_reading=0.0, last_reading=1.0)


def test_the_sweep_runs_end_to_end_and_costs_two_report_queries_per_kill():
    client = StubClient([("aBcD1234", 7), ("eFgH5678", 3)], [DPS_ROW, HUNTER_ROW])
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=2, rankings_pages=1
    )
    plan = harvest.QueryPlan()

    found, summary, buckets = harvest.harvest_encounter(
        client, 3470, settings, {250215: equipment.INVENTORY_TYPES["trinket"]}, plan, tables()
    )

    assert summary["killsRead"] == 2
    assert summary["playersRead"] == 4  # two kills, two damage players each
    assert buckets == ["dps", "healers", "tanks"]

    # One rankings query, then exactly two per kill -- not two per player.
    assert plan.rankings == 1
    assert plan.player_details == 2
    assert plan.talent_codes == 2
    assert plan.total == 2 + 1 + 2 + 2
    assert client.calls == [
        "rankings:3470:1",
        "player-details:aBcD1234:7",
        "talent-codes:aBcD1234:7:2",
        "player-details:eFgH5678:3",
        "talent-codes:eFgH5678:3:2",
    ]

    document = harvest.build_document(
        "MID2", 5, found, tables(), [summary], query_plan=plan.to_json()
    )
    assert document["source"]["killsSampled"] == 2
    assert {row["specId"] for row in document["specs"]} == {
        "death_knight_frost",
        "hunter_beast_mastery",
    }
    assert document["coverage"]["rejectedTotal"] == 0
    # Two players of one spec ran the same build in two different kills.
    frost = next(row for row in document["specs"] if row["specId"] == "death_knight_frost")
    assert frost["distinctBuilds"] == 1
    assert frost["builds"][0]["seenInKills"] == 2


def test_an_encounter_with_fewer_kills_than_asked_for_says_so():
    """A fact about the encounter, not a failure of the pass -- the same distinction
    the fight probe draws with `searchExhausted`."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=30)
    _, summary, _ = harvest.harvest_encounter(
        client, 3470, settings, {}, harvest.QueryPlan(), tables()
    )
    assert summary["killsRead"] == 1
    assert summary["fewerKillsThanRequested"] is True


def test_a_kill_whose_roster_is_empty_costs_no_talent_query():
    """Asking for the talent codes of nobody is a GraphQL error, not an empty
    answer, and it would abort a pass over one bad report."""
    client = StubClient([("aBcD1234", 7)], [])
    plan = harvest.QueryPlan()
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)
    found, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, plan, tables())
    assert found == []
    assert plan.player_details == 1
    assert plan.talent_codes == 0


# --------------------------------------------------------------------------------
# Which encounter id the kills are actually under
# --------------------------------------------------------------------------------

#: MID2's own filed ids and the live twins CI measured them against.
SSZORAK_PTR = 53420
NEKZALI_PTR, NEKZALI_LIVE = 53470, 3470
NEKZALI = "Nek'zali the Soulcoiler"


def test_a_ptr_id_is_its_live_id_with_a_five_in_front():
    """The rule CLAUDE.md records from two independent zone pairs. Purely the shape
    of the number: nothing acts on it without checking the name."""
    assert harvest.live_twin_id(NEKZALI_PTR) == NEKZALI_LIVE
    assert harvest.live_twin_id(SSZORAK_PTR) == 3420
    assert harvest.live_twin_id(53176) == 3176

    # A live id is not a PTR id of something else.
    assert harvest.live_twin_id(NEKZALI_LIVE) is None
    assert harvest.live_twin_id(3176) is None
    # And a remainder with a leading zero is not a live id at all, so 50123 is not
    # "the PTR copy of 123" -- reading it as one would address a different boss.
    assert harvest.live_twin_id(50123) is None
    assert harvest.live_twin_id(5) is None
    assert harvest.live_twin_id(0) is None


def test_an_id_with_ranked_parses_is_harvested_as_filed_and_asks_nothing():
    """The ordinary case must cost no extra query, or every encounter pays for the
    PTR ones."""

    def never(encounter_id):
        raise AssertionError(f"no lookup should be sent, got {encounter_id}")

    choice = harvest.choose_encounter_id(NEKZALI_LIVE, NEKZALI, True, never)
    assert (choice.used, choice.substituted, choice.refused) == (NEKZALI_LIVE, False, False)
    assert "as filed" in choice.reason


def test_a_ptr_id_with_no_parses_is_read_as_its_live_twin_when_the_name_agrees():
    """Measured in CI on 2026-08-23: 53420 returns 0 kills and 0 characterRankings,
    3470 returns 1 kill, 100 characterRankings, 14 dps rows and 14 talent codes."""
    choice = harvest.choose_encounter_id(NEKZALI_PTR, NEKZALI, False, {NEKZALI_LIVE: NEKZALI}.get)
    assert choice.used == NEKZALI_LIVE
    assert choice.substituted is True
    assert str(NEKZALI_LIVE) in choice.reason and NEKZALI in choice.reason
    assert choice.to_json() == {
        "requested": NEKZALI_PTR,
        "used": NEKZALI_LIVE,
        "substituted": True,
        "reason": choice.reason,
    }


def test_a_twin_that_names_another_boss_is_refused_rather_than_harvested():
    """The failure this check exists for is not an empty file. It is a **full** set
    of real builds filed under the wrong fight, which nothing downstream could
    detect."""
    choice = harvest.choose_encounter_id(
        NEKZALI_PTR, NEKZALI, False, {NEKZALI_LIVE: "Somebody Else Entirely"}.get
    )
    assert choice.refused and choice.used is None
    assert "different boss" in choice.reason
    assert NEKZALI in choice.reason and "Somebody Else Entirely" in choice.reason


def test_a_twin_the_schema_does_not_know_is_refused_with_that_reason():
    choice = harvest.choose_encounter_id(NEKZALI_PTR, NEKZALI, False, lambda _: None)
    assert choice.refused
    assert "not an encounter Warcraft Logs knows" in choice.reason


def test_an_unnamed_requested_encounter_can_never_verify_a_twin():
    """Nothing to compare against is not the same as a match, and the flattering
    reading of it would substitute an unverified id."""
    choice = harvest.choose_encounter_id(NEKZALI_PTR, None, False, {NEKZALI_LIVE: NEKZALI}.get)
    assert choice.refused
    assert harvest.names_agree(None, NEKZALI) is False
    assert harvest.names_agree(NEKZALI, NEKZALI) is True
    # Two rows of one table differing in case or trailing space are one boss.
    assert harvest.names_agree(" nek'zali the soulcoiler ", NEKZALI) is True


def test_the_comma_in_nekzalis_name_no_longer_refuses_its_own_twin():
    """Issue #40, and it is one comma. Warcraft Logs writes the same encounter as
    ``Nek'zali, the Soulcoiler`` under the PTR id 53470 and ``Nek'zali the Soulcoiler``
    under the live id 3470, and the strict comparison refused the substitution over
    the punctuation alone.

    Nothing about the resolution moves except the comparison: the id is still checked
    for the PTR shape, the twin is still looked up, and the name is still what decides.
    """
    ptr_name = "Nek'zali, the Soulcoiler"
    assert harvest.names_agree(ptr_name, NEKZALI) is True

    choice = harvest.choose_encounter_id(NEKZALI_PTR, ptr_name, False, {NEKZALI_LIVE: NEKZALI}.get)
    assert choice.used == NEKZALI_LIVE
    assert choice.substituted is True


def test_two_different_bosses_are_still_refused_after_the_loosening():
    """The guard, and the point of the change is that this list keeps failing.

    Dropping punctuation is allowed to make *one* boss's two spellings agree. It must
    not make two bosses agree, so every pair below is a way a looser rule could have
    gone wrong: a near-miss on one letter, a name that is a prefix of the other, a
    different second word, and a wholly unrelated boss from this same tier.
    """
    different = [
        ("Nek'zali the Soulcoiler", "Nek'zali the Soulbinder"),
        ("Nek'zali the Soulcoiler", "Nek'zali"),
        ("The Twin Fangs", "The Twin Fang"),
        ("Vaelgor & Ezzorak", "Vaelgor"),
        ("Sszorak", "Ula'tek"),
        ("Entombed Sentinels", "Lightblinded Vanguard"),
    ]
    for left, right in different:
        assert harvest.names_agree(left, right) is False, (left, right)
        # And the refusal is what the caller sees, not just what the predicate says.
        choice = harvest.choose_encounter_id(NEKZALI_PTR, left, False, {NEKZALI_LIVE: right}.get)
        assert choice.refused and choice.used is None, (left, right)
        assert "different boss" in choice.reason


def test_punctuation_is_dropped_as_a_separator_and_not_as_a_deletion():
    """Which of the two normalisations is in force, pinned, because they differ.

    Each mark becomes a space, so word boundaries survive a name being punctuated
    differently -- and two names cannot be joined into one string that a third name
    could also produce. The consequence is stated rather than hidden: a twin that
    drops an apostrophe outright still disagrees, and that is the safe direction.
    """
    assert harvest.names_agree("Fallen-King Salhadaar", "Fallen King Salhadaar") is True
    assert harvest.names_agree("Nek'zali the Soulcoiler", "Nekzali the Soulcoiler") is False
    # Whitespace differences alone were already tolerated and still are.
    assert harvest.names_agree("  Ula'tek\t", "ula'tek") is True


def test_a_live_id_with_no_parses_has_no_twin_to_try():
    """A boss nobody has killed yet is an ordinary state, and it is reported as
    itself rather than as a resolution failure."""
    choice = harvest.choose_encounter_id(NEKZALI_LIVE, NEKZALI, False, lambda _: None)
    assert choice.refused
    assert "not a PTR id" in choice.reason


def test_the_sweep_harvests_the_live_twin_and_publishes_which_id_it_read():
    """End to end, and the whole of blocker 2: MID2 files this boss under 53470,
    that id has no ranked parses, and the kills are under 3470."""
    client = StubClient(
        [("aBcD1234", 7)],
        [DPS_ROW],
        names={NEKZALI_PTR: NEKZALI, NEKZALI_LIVE: NEKZALI},
        ranked_ids={NEKZALI_LIVE},
    )
    plan = harvest.QueryPlan()
    settings = harvest.HarvestSettings(encounter_ids=(NEKZALI_PTR,), difficulty=5, reports=1)

    found, summary, _ = harvest.harvest_encounter(client, NEKZALI_PTR, settings, {}, plan, tables())

    assert summary["killsRead"] == 1
    assert found and {o.encounter_id for o in found} == {NEKZALI_LIVE}
    # The tier's own id is still what the encounter is filed under, so
    # fight_profiles.json keeps joining.
    assert summary["id"] == NEKZALI_PTR
    assert summary["idResolution"]["used"] == NEKZALI_LIVE
    assert summary["idResolution"]["substituted"] is True

    # One name lookup, and the ranking query on both ids -- the empty one and the
    # one that answered. Nothing per kill.
    assert plan.encounter_names == 1
    assert client.calls == [
        f"rankings:{NEKZALI_PTR}:1",
        f"encounter-name:{NEKZALI_LIVE}",
        f"rankings:{NEKZALI_LIVE}:1",
        "player-details:aBcD1234:7",
        "talent-codes:aBcD1234:7:1",
    ]


def test_a_refused_id_reads_no_kills_and_still_appears_in_the_document():
    """An encounter nobody could harvest and an encounter nobody tried are different
    answers, so the refusal keeps its row rather than vanishing from the file."""
    client = StubClient(
        [("aBcD1234", 7)],
        [DPS_ROW],
        names={NEKZALI_PTR: NEKZALI, NEKZALI_LIVE: "A Different Boss"},
        ranked_ids={NEKZALI_LIVE},
    )
    plan = harvest.QueryPlan()
    settings = harvest.HarvestSettings(encounter_ids=(NEKZALI_PTR,), difficulty=5, reports=1)

    found, summary, _ = harvest.harvest_encounter(client, NEKZALI_PTR, settings, {}, plan, tables())

    assert found == []
    assert summary["killsRead"] == 0
    assert summary["idResolution"]["used"] is None
    assert "different boss" in summary["idResolution"]["reason"]
    # Nothing report-level was paid for, and "fewer kills than requested" is not
    # claimed about an encounter that was never read.
    assert plan.player_details == 0
    assert summary["fewerKillsThanRequested"] is False

    document = harvest.build_document("MID2", 5, found, tables(), [summary])
    assert document["source"]["encounters"][0]["idResolution"]["used"] is None


def test_the_gear_of_a_harvested_kill_reaches_the_document():
    """The other half of blocker 1, past the query: a real payload with combatant
    info produces gear, and one without it says how many rows had none."""
    with_gear = StubClient([("aBcD1234", 7)], [DPS_ROW])
    without = StubClient([("aBcD1234", 7)], [NO_COMBATANT_INFO_ROW])
    inventory = {250215: equipment.INVENTORY_TYPES["trinket"]}
    settings = harvest.HarvestSettings(encounter_ids=(NEKZALI_LIVE,), difficulty=5, reports=1)

    found, summary, _ = harvest.harvest_encounter(
        with_gear, NEKZALI_LIVE, settings, inventory, harvest.QueryPlan(), tables()
    )
    assert [piece.item_id for piece in found[0].gear] == [250215]
    assert summary["playersWithoutCombatantInfo"] == 0

    blind, blind_summary, _ = harvest.harvest_encounter(
        without, NEKZALI_LIVE, settings, inventory, harvest.QueryPlan(), tables()
    )
    assert blind[0].gear == ()
    assert blind_summary["playersWithoutCombatantInfo"] == 1
    document = harvest.build_document("MID2", 5, blind, tables(), [blind_summary])
    assert document["coverage"]["playersWithoutCombatantInfo"] == 1


def test_a_published_candidate_says_which_boss_and_difficulty_it_came_from():
    """Held on the observation and not emitted, so a consumer had to join back to
    `fights.json` -- a file a tier may not have. The document's own
    `source.encounters[]` carries `{id, name}` and is the better of the two
    fallbacks; neither replaces the row saying so itself."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(NEKZALI_LIVE,), difficulty=5, reports=1)
    found, summary, _ = harvest.harvest_encounter(
        client, NEKZALI_LIVE, settings, {}, harvest.QueryPlan(), tables()
    )
    document = harvest.build_document("MID2", 5, found, tables(), [summary])

    source = document["specs"][0]["builds"][0]["sources"][0]
    assert source["encounterID"] == NEKZALI_LIVE
    assert source["encounterName"] == NEKZALI
    assert source["difficulty"] == 5

    encounter = document["source"]["encounters"][0]
    assert (encounter["id"], encounter["name"]) == (NEKZALI_LIVE, NEKZALI)


# --------------------------------------------------------------------------------
# Against real hashes, when a simc checkout is at hand
# --------------------------------------------------------------------------------

DATASET = pathlib.Path(__file__).resolve().parents[2] / "web" / "public" / "data" / "MID2"


def _simc_source() -> pathlib.Path | None:
    """A simc checkout, if the environment names one.

    Skipped rather than required: the pinned tests above are hermetic on purpose,
    and CI's own simc checkout is a job-level artefact rather than a fixture. Set
    `WOWDPS_SIMC_SOURCE` to run this one -- it is the check that the whole join
    works against real strings rather than against strings this file wrote.
    """
    named = os.environ.get("WOWDPS_SIMC_SOURCE")
    if not named:
        return None
    path = pathlib.Path(named)
    return path if (path / "engine" / "dbc" / "generated").is_dir() else None


@pytest.mark.skipif(_simc_source() is None, reason="set WOWDPS_SIMC_SOURCE to a simc checkout")
def test_every_committed_build_hash_survives_the_harvest_validator():
    """The tier's own 36 shipped hashes, put through the harvest's four checks.

    A harvested hash and a shipped hash are the same kind of object -- both are
    Blizzard loadout strings -- so the shipped ones are the only real corpus
    available without credentials. Two things are asserted: every one of them is
    accepted, and the hero tree the harvest reads out of it agrees with the name the
    dataset already carries, which `herotrees.py` derived by a different route.

    The control against "it accepts everything" is the next test.
    """
    tables = harvest.TalentTables.load(_simc_source(), ptr=True)
    checked = agreed = 0
    for path in sorted((DATASET / "specs").glob("*.json")):
        build = json.loads(path.read_text(encoding="utf-8"))
        verdict = harvest.validate(
            observation(build.get("talentHash"), spec=build["spec"], wow_class=build["class"]),
            tables,
        )
        assert verdict.reason == harvest.REASON_OK, f"{build['id']}: {verdict.detail}"
        checked += 1
        _, name = harvest.HarvestedBuild(
            key="k", loadout=verdict.loadout, talent_hash=""
        ).hero_tree(tables)
        if name and build.get("heroTalent") not in (None, "Default"):
            agreed += name == build["heroTalent"]
    assert checked >= 26
    assert agreed == checked


@pytest.mark.skipif(_simc_source() is None, reason="set WOWDPS_SIMC_SOURCE to a simc checkout")
def test_a_real_hash_under_the_wrong_spec_name_is_still_caught():
    """The control for the test above: the checks are not simply saying yes."""
    tables = harvest.TalentTables.load(_simc_source(), ptr=True)
    build = json.loads(
        next(iter(sorted((DATASET / "specs").glob("mage_arcane*.json")))).read_text()
    )
    verdict = harvest.validate(
        observation(build["talentHash"], spec="Unholy", wow_class="Death Knight"), tables
    )
    assert verdict.reason == harvest.REASON_SPEC_MISMATCH


# --------------------------------------------------------------------------------
# The command itself
# --------------------------------------------------------------------------------


class _Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _args(tmp_path, **overrides) -> _Namespace:
    base = dict(
        tier="MID2",
        encounter=[3470],
        difficulty=5,
        metric="dps",
        also_metric=[],
        reports=1,
        rankings_pages=1,
        page=1,
        order="top",
        spec=None,
        simc_source="unused",
        ptr=True,
        probe=False,
        max_sources=3,
        point_ceiling=0.8,
        out=str(tmp_path),
        cache=None,
        profiles_file=None,
    )
    base.update(overrides)
    return _Namespace(**base)


class _FullClient(StubClient):
    """A stub the command can drive, including the bracketing readings."""

    def __init__(self, roster=None):
        super().__init__([("aBcD1234", 7)], [DPS_ROW] if roster is None else roster)
        self.ledger = warcraftlogs.PointLedger(
            limit_per_hour=3600, first_reading=100.0, last_reading=118.0
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rate_limit(self):
        self.calls.append("rate-limit")
        return {"pointsSpentThisHour": 100.0, "limitPerHour": 3600, "pointsResetIn": 900}


def _install(monkeypatch, client):
    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(warcraftlogs, "WarcraftLogsClient", lambda *a, **k: client)
    monkeypatch.setattr(harvest.TalentTables, "load", classmethod(lambda cls, *a, **k: tables()))
    monkeypatch.setattr(
        harvest.equipment,
        "inventory_types",
        lambda _dir: {250215: equipment.INVENTORY_TYPES["trinket"]},
    )


def test_the_command_writes_a_dataset_and_reports_what_it_spent(tmp_path, monkeypatch, capsys):
    client = _FullClient()
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path)) == 0

    document = json.loads((tmp_path / "MID2" / "harvested-builds.json").read_text())
    assert document["tier"] == "MID2"
    assert document["source"]["difficulty"] == 5
    assert document["specs"][0]["specId"] == "death_knight_frost"
    assert document["specs"][0]["builds"][0]["sources"][0]["simcGear"] == [
        "trinket1=,id=250215,ilevel=334,gem_id=240906,enchant_id=7967"
    ]
    # The pass brackets itself with two standalone readings, which is what makes the
    # run total a measurement rather than an attribution guess.
    assert client.calls.count("rate-limit") == 2
    assert document["source"]["queries"]["total"] == 2 + 1 + 1 + 1
    assert "points per sampled kill (measured)" in capsys.readouterr().out


def test_probe_mode_writes_nothing(tmp_path, monkeypatch, capsys):
    """Whether a full pass is affordable is a measurement, and taking it must not
    require running the pass whose cost is in question."""
    _install(monkeypatch, _FullClient())

    assert harvest.cmd_harvest_builds(_args(tmp_path, probe=True)) == 0

    assert not (tmp_path / "MID2").exists()
    out = capsys.readouterr().out
    assert "payload shapes, read from the live service" in out
    assert "dps row keys" in out
    assert "talentImportCode: 1 of the fight's actors returned a code" in out
    assert "death_knight_frost: ok" in out


def test_missing_credentials_stop_the_run_rather_than_half_running_it(tmp_path, monkeypatch):
    def refuse(cls):
        raise warcraftlogs.WarcraftLogsError("WCL_CLIENT_ID and WCL_CLIENT_SECRET must be set.")

    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", classmethod(refuse))
    assert harvest.cmd_harvest_builds(_args(tmp_path, reports=2)) == 2
    assert not (tmp_path / "MID2").exists()


def test_an_item_with_no_slot_reaches_the_published_document(tmp_path, monkeypatch, capsys):
    """The gear half of a harvest is only usable for items simc can name a slot for,
    so a run where many cannot is a run whose gear half is not usable -- and nothing
    else in the file would say so.

    This is a defect that was in the first version of the sweep: the ids were
    collected inside `harvest_encounter` and dropped on the way out, so
    `coverage.itemsWithoutASlot` was permanently empty and read as "all good".
    """
    client = _FullClient()
    _install(monkeypatch, client)
    # simc's table knows nothing about the item this player is wearing.
    monkeypatch.setattr(harvest.equipment, "inventory_types", lambda _dir: {})

    assert harvest.cmd_harvest_builds(_args(tmp_path)) == 0
    capsys.readouterr()

    document = json.loads((tmp_path / "MID2" / "harvested-builds.json").read_text())
    assert document["coverage"]["itemsWithoutASlot"] == [250215]
    assert document["source"]["encounters"][0]["itemsWithoutASlot"] == [250215]
    # The build itself still lands: the talents are readable even when the gear is not.
    assert document["specs"][0]["builds"][0]["sources"][0]["simcGear"] == []


def test_the_sweep_actually_applies_the_difficulty_refusal():
    """`check_difficulty` was written, tested and *not called* -- the defect family
    this repository's notes name over and over: the code looks complete from every
    angle except running it, and the document would have claimed one difficulty per
    run while nothing enforced it."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    original = client.encounter_rankings

    def heroic_row(*args, **kwargs):
        page = original(*args, **kwargs)
        page["characterRankings"]["rankings"][0]["difficulty"] = 4
        return page

    client.encounter_rankings = heroic_row
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)
    with pytest.raises(harvest.DifficultyMixed) as caught:
        harvest.harvest_encounter(client, 3470, settings, {}, harvest.QueryPlan(), tables())
    assert "difficulty 4" in str(caught.value)
    assert "asked for 5" in str(caught.value)


def test_a_ranking_row_stating_no_difficulty_does_not_stop_the_sweep():
    """Unknown is not wrong, and `characterRankings` is untyped -- whether a row
    carries the field at all is not knowable from the schema."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)
    found, summary, _ = harvest.harvest_encounter(
        client, 3470, settings, {}, harvest.QueryPlan(), tables()
    )
    assert summary["killsRead"] == 1
    assert found


# --------------------------------------------------------------------------------
# What a stopped pass keeps, and what the probe measures about itself
# --------------------------------------------------------------------------------


class _BudgetClient(_FullClient):
    """Stops on the point ceiling partway through an encounter's kills."""

    def __init__(self, kills, roster, stop_after: int):
        super().__init__(roster)
        self.kills = kills
        self.stop_after = stop_after
        self.details_calls = 0

    def player_details(self, code, fight_id):
        self.details_calls += 1
        if self.details_calls > self.stop_after:
            from wowdps.fightprobe import PointBudgetExhausted

            raise PointBudgetExhausted("point ceiling reached: 2900 of 3600")
        return super().player_details(code, fight_id)


def test_a_budget_abort_keeps_the_kills_that_encounter_already_paid_for():
    """`fightprobe.probe_encounter` returns the partial observation plus the reason,
    and this function's own docstring cited that as the contract it follows while
    doing the opposite: the exception escaped, so kills 1-8 of the current encounter
    were discarded after they had been paid for, and the encounter had no summary at
    all."""
    client = _BudgetClient([("a", 1), ("b", 2), ("c", 3)], [DPS_ROW, HUNTER_ROW], stop_after=2)
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=3)
    found, summary, _ = harvest.harvest_encounter(
        client, 3470, settings, {}, harvest.QueryPlan(), tables()
    )

    assert summary["killsRead"] == 2
    assert len(found) == 4
    assert "point ceiling" in summary["stoppedBy"]
    # The encounter is not also accused of being thin: `stoppedBy` says why, and
    # claiming both would report an encounter that has plenty of kills as short.
    assert summary["fewerKillsThanRequested"] is False


def test_a_stopped_pass_publishes_what_it_read_and_exits_two(tmp_path, monkeypatch, capsys):
    client = _BudgetClient([("a", 1), ("b", 2)], [DPS_ROW], stop_after=1)
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path, reports=2)) == 2
    capsys.readouterr()

    document = json.loads((tmp_path / "MID2" / "harvested-builds.json").read_text())
    assert document["source"]["encounters"][0]["killsRead"] == 1
    assert document["specs"][0]["specId"] == "death_knight_frost"


def test_a_difficulty_refusal_keeps_the_encounters_that_ran_clean(tmp_path, monkeypatch, caplog):
    """It propagated through a loop catching only PointBudgetExhausted and
    WarcraftLogsError and out of `cli.main`, which has no handler -- so a pass that
    had harvested eight encounters printed a traceback, wrote no file and burned the
    points for nothing."""
    client = _FullClient()
    original = client.encounter_rankings
    seen: list[int] = []

    def rankings(encounter_id, difficulty=5, metric="dps", page=1):
        seen.append(encounter_id)
        page_payload = original(encounter_id, difficulty, metric, page)
        if encounter_id == 3471:
            page_payload["characterRankings"]["rankings"][0]["difficulty"] = 4
        return page_payload

    client.encounter_rankings = rankings
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path, encounter=[3470, 3471])) == 1

    # Not a traceback: the refusal's own wording, and the file that ran clean.
    assert "refusing to pool difficulties" in caplog.text
    document = json.loads((tmp_path / "MID2" / "harvested-builds.json").read_text())
    assert [entry["id"] for entry in document["source"]["encounters"]] == [3470]
    assert document["source"]["difficulty"] == 5


def test_the_probe_counts_every_request_it_made_and_makes_no_others(tmp_path, monkeypatch, capsys):
    """Driven through this stub, the probe made **eight** client calls while printing
    "queries sent: 5" -- it re-fetched the rankings, the player details and the
    talent codes the sweep had already paid for, at page 1 even when --page named
    another. Without a warm cache those are billed, so the measured points were
    divided by five queries and one kill and the extrapolation came out about 1.6x
    too large: enough to call an affordable pass unaffordable, which is the one
    decision the probe exists to inform."""
    client = _FullClient()
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path, probe=True)) == 0
    out = capsys.readouterr().out

    assert client.calls == [
        "rate-limit",
        "rankings:3470:1",
        "player-details:aBcD1234:7",
        "talent-codes:aBcD1234:7:1",
        "rate-limit",
    ]
    assert f"queries sent: {len(client.calls)}" in out


def test_the_probe_describes_the_payload_even_when_it_could_read_nothing(
    tmp_path, monkeypatch, capsys
):
    """The case the probe exists for. Handed the *parsed* result, a run whose
    `playerDetails` had changed shape -- or whose one kill --spec simply filtered
    out -- printed nothing at all, and a --spec probe read as a failed schema check
    when the schema was fine."""
    client = _FullClient()
    # A bucket rename: the payload is there and nothing can be read out of it.
    client.player_details = lambda code, fight_id: {"data": {"damage": [DPS_ROW]}}
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path, probe=True)) == 0
    out = capsys.readouterr().out

    assert "payload shapes, read from the live service" in out
    assert "buckets none found" in out
    assert "Top-level payload shape: ['data']" in out


def test_the_gear_entries_nobody_could_read_are_counted_in_the_document(
    tmp_path, monkeypatch, capsys
):
    """`pieces, _skipped = gear_from_row(...)` -- computed, documented as
    load-bearing, thrown away one frame up, which is the defect this PR reports
    fixing twice elsewhere. If Warcraft Logs renames `combatantInfo.gear[].id`,
    every entry is skipped, every build publishes `simcGear: []` and
    `itemsWithoutASlot: []`, and the file reads as "these builds wear nothing worth
    writing down" rather than "the gear payload moved"."""
    moved = dict(DPS_ROW, combatantInfo={"gear": [{"itemID": 250215}, {"itemID": 1}]})
    client = _FullClient(roster=[moved])
    _install(monkeypatch, client)

    assert harvest.cmd_harvest_builds(_args(tmp_path)) == 0
    capsys.readouterr()

    document = json.loads((tmp_path / "MID2" / "harvested-builds.json").read_text())
    assert document["coverage"]["gearEntriesSkipped"] == 2
    assert document["coverage"]["itemsWithoutASlot"] == []
    assert document["source"]["encounters"][0]["gearEntriesSkipped"] == 2


# --------------------------------------------------------------------------------
# --spec, the settings object, and the ranking pages
# --------------------------------------------------------------------------------


def test_a_spec_selection_targets_the_ranking_it_samples_from():
    """The specs this command exists for -- Havoc, Arms, Fury, Feral, Devourer --
    are the ones least likely to be in a top guild's roster, so kills taken from the
    encounter's overall damage ranking and filtered afterwards can come back with
    nothing and an empty `specs` array with nothing saying why."""
    only, targets, unknown = harvest.resolve_spec_selection(("hunter_beast_mastery",), tables())
    assert only == ("hunter_beast_mastery",)
    assert targets == (("Hunter", "Beast Mastery"),)
    assert unknown == ()

    client = StubClient([("aBcD1234", 7)], [DPS_ROW, HUNTER_ROW])
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=1, only_specs=only, spec_targets=targets
    )
    found, _, _ = harvest.harvest_encounter(
        client, 3470, settings, {}, harvest.QueryPlan(), tables()
    )
    # One ranking query, for that spec's parses, and no overall ranking beside it.
    assert client.calls == [
        "spec-rankings:3470:Hunter/Beast Mastery:1",
        "player-details:aBcD1234:7",
        "talent-codes:aBcD1234:7:2",
    ]
    assert [o.spec_key for o in found] == ["hunter_beast_mastery"]


def test_warcraft_logs_own_spelling_of_a_spec_key_resolves_too():
    """`deathknight_frost` is what somebody reading a log will type."""
    only, targets, unknown = harvest.resolve_spec_selection(("deathknight_frost",), tables())
    assert only == ("death_knight_frost",)
    assert targets == (("Death Knight", "Frost"),)
    assert unknown == ()


def test_a_spec_nobody_can_resolve_stops_the_run_rather_than_emptying_the_file(
    tmp_path, monkeypatch
):
    _install(monkeypatch, _FullClient())
    assert harvest.cmd_harvest_builds(_args(tmp_path, spec=["mage_frostfire"])) == 2
    assert not (tmp_path / "MID2").exists()


def test_probe_mode_settings_describe_the_pass_the_probe_actually_runs(
    tmp_path, monkeypatch, capsys
):
    """`encounter_ids = encounter_ids[:1]` ran *after* the settings were built, so
    `settings.encounter_ids` held every boss of the tier while the loop read one.
    Any future code reading the list off the settings object -- the obvious place --
    would sweep the whole tier in the mode whose contract is "one kill of one
    encounter, write nothing"."""
    seen: list[harvest.HarvestSettings] = []
    real = harvest.harvest_encounter

    def spy(client, encounter_id, settings, *args, **kwargs):
        seen.append(settings)
        return real(client, encounter_id, settings, *args, **kwargs)

    monkeypatch.setattr(harvest, "harvest_encounter", spy)
    _install(monkeypatch, _FullClient())

    assert harvest.cmd_harvest_builds(_args(tmp_path, encounter=[3470, 3471], probe=True)) == 0
    capsys.readouterr()
    assert seen[0].encounter_ids == (3470,)
    assert len(seen) == 1


def test_the_settings_default_matches_the_flag_that_documents_it():
    """`add_arguments` documents one page as deliberate; the dataclass said eight, so
    any caller building settings directly -- five tests here, and any future
    scheduled job -- got eight times the ranking queries with no flag set."""
    import argparse

    parser = argparse.ArgumentParser()
    harvest.add_arguments(parser)
    flag_default = parser.parse_args([]).rankings_pages
    assert harvest.HarvestSettings(encounter_ids=(), difficulty=5).rankings_pages == flag_default


def test_an_exhausted_ranking_list_stops_the_page_loop():
    """Pages were gathered unconditionally, so an encounter whose rankings hold one
    page still paid for every page asked for."""

    class _OnePage(StubClient):
        def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
            self.calls.append(f"rankings:{encounter_id}:{page}")
            if page > 1:
                return {"id": encounter_id, "characterRankings": {"rankings": []}}
            return super().encounter_rankings(encounter_id, difficulty, metric, page)

    client = _OnePage([("aBcD1234", 7)], [DPS_ROW])
    plan = harvest.QueryPlan()
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=1, rankings_pages=8
    )
    harvest.harvest_encounter(client, 3470, settings, {}, plan, tables())
    assert plan.rankings == 2  # page 1, then the empty page 2 that ends it


def test_a_kills_player_details_payload_is_parsed_once():
    """The actor ids for the talent query and the observations come out of the same
    rows. Parsed twice, a kill whose `playerDetails` arrives as a JSON string -- a
    shape this code explicitly supports -- meant a second `json.loads` of a
    twenty-player table on every kill."""
    parsed: list[object] = []
    real = harvest.player_detail_rows

    def counting(payload, bucket="dps"):
        parsed.append(payload)
        return real(payload, bucket)

    client = StubClient([("aBcD1234", 7)], [DPS_ROW, HUNTER_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)
    import unittest.mock

    with unittest.mock.patch.object(harvest, "player_detail_rows", counting):
        harvest.harvest_encounter(client, 3470, settings, {}, harvest.QueryPlan(), tables())
    assert len(parsed) == 1


# --------------------------------------------------------------------------------
# The double pass: a second ranking metric (#111)
# --------------------------------------------------------------------------------


class _MetricClient(StubClient):
    """A stub whose ranking answer depends on the metric asked for.

    The base stub ignores the metric, which is the right default for every test
    written before a second pass existed -- but it also means those tests cannot
    tell a second metric that reached new kills from one that re-ranked the same
    ones. That distinction is the whole point of the pass, so it needs a stub that
    can express both.
    """

    def __init__(self, per_metric, roster, **kwargs):
        super().__init__(per_metric["dps"], roster, **kwargs)
        self.per_metric = per_metric

    def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
        self.calls.append(f"rankings:{encounter_id}:{metric}:{page}")
        self.kills = self.per_metric.get(metric, [])
        return self._page(encounter_id)


def _passes(summary):
    return {row["metric"]: (row["reports"], row["newReports"]) for row in summary["metricPasses"]}


def test_a_second_ranking_metric_widens_the_pool_and_publishes_what_it_added():
    """`bossdps` ranks the players who did the most to the BOSS, which is a different
    question from total damage and can surface kills the damage ranking does not."""
    client = _MetricClient(
        {
            "dps": [("shared01", 7), ("dpsOnly1", 2)],
            "bossdps": [("shared01", 7), ("bossOnly", 4)],
        },
        [DPS_ROW],
    )
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=10, also_metrics=("bossdps",)
    )
    plan = harvest.QueryPlan()

    found, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, plan, tables())

    # Three distinct reports out of two two-kill rankings: the shared one is read
    # once, which is `select_report_fights`'s one-fight-per-report rule doing the
    # deduplication rather than a second copy of it here.
    assert summary["killsRead"] == 3
    assert [call for call in client.calls if call.startswith("player-details")] == [
        "player-details:shared01:7",
        "player-details:dpsOnly1:2",
        "player-details:bossOnly:4",
    ]
    assert len(found) == 3

    # Two ranking queries, and nothing extra per kill -- the affordability claim.
    assert plan.rankings == 2
    assert plan.player_details == 3
    assert _passes(summary) == {"dps": (2, 2), "bossdps": (2, 1)}


def test_a_second_metric_that_ranks_the_same_kills_is_published_as_having_added_nothing():
    """The finding this field exists for. `bossdps` reaching nothing new is a real
    answer about the encounter, and an unpublished one would read as untried."""
    client = _MetricClient(
        {"dps": [("shared01", 7)], "bossdps": [("shared01", 7)]},
        [DPS_ROW],
    )
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=10, also_metrics=("bossdps",)
    )
    plan = harvest.QueryPlan()

    _, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, plan, tables())

    assert summary["killsRead"] == 1
    assert _passes(summary) == {"dps": (1, 1), "bossdps": (1, 0)}
    # It still cost its own query. That is what makes zero worth publishing.
    assert plan.rankings == 2


def test_a_single_metric_pass_publishes_no_metric_passes_key_at_all():
    """A field on every encounter of every past run would move bytes nobody asked to
    move, and `metricPasses` over one metric says nothing a reader can use."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)

    _, summary, _ = harvest.harvest_encounter(
        client, 3470, settings, {}, harvest.QueryPlan(), tables()
    )

    assert "metricPasses" not in summary


def test_the_second_metric_asks_the_id_the_first_one_resolved():
    """Which id a boss's kills live under is a property of the encounter, not of the
    metric. Asking `bossdps` about the PTR id would spend a query to learn what the
    first pass already established, and on a tier filed under PTR ids that is every
    boss."""
    client = _MetricClient(
        {"dps": [("aBcD1234", 7)], "bossdps": [("aBcD1234", 7)]},
        [DPS_ROW],
        names={NEKZALI_PTR: NEKZALI, NEKZALI_LIVE: NEKZALI},
        ranked_ids={NEKZALI_LIVE},
    )
    settings = harvest.HarvestSettings(
        encounter_ids=(NEKZALI_PTR,), difficulty=5, reports=1, also_metrics=("bossdps",)
    )

    _, summary, _ = harvest.harvest_encounter(
        client, NEKZALI_PTR, settings, {}, harvest.QueryPlan(), tables()
    )

    assert summary["idResolution"]["used"] == NEKZALI_LIVE
    assert [call for call in client.calls if call.startswith("rankings")] == [
        f"rankings:{NEKZALI_PTR}:dps:1",
        f"rankings:{NEKZALI_LIVE}:dps:1",
        f"rankings:{NEKZALI_LIVE}:bossdps:1",
    ]


def test_naming_the_primary_metric_again_costs_no_second_query():
    """`--metric bossdps --also-metric bossdps` is a plausible thing to type and
    would otherwise pay twice for one ranking and publish a pass that added nothing
    because it was the same pass."""
    client = _MetricClient({"dps": [("aBcD1234", 7)]}, [DPS_ROW])
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=1, also_metrics=("dps",)
    )
    plan = harvest.QueryPlan()

    _, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, plan, tables())

    assert plan.rankings == 1
    assert "metricPasses" not in summary
