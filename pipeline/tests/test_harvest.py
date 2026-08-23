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

CLASS_ID = talenttree.CLASS_IDS["Mage"]
ARCANE, FIRE = 62, 63


def trait(node_id: int, entry_id: int, **overrides) -> Trait:
    base = dict(
        tree_index=talenttree.TREE_SPEC,
        class_id=CLASS_ID,
        entry_id=entry_id,
        node_id=node_id,
        max_ranks=1,
        req_points=0,
        spell_id=entry_id * 10,
        row=1,
        col=1,
        selection_index=0,
        name=f"Talent {entry_id}",
        spec_ids=(ARCANE,),
        sub_tree=0,
        node_type=talenttree.NODE_NORMAL,
    )
    base.update(overrides)
    return Trait(**base)


#: Four nodes: two plain spec talents, one that only Fire may take, and the hero
#: SELECTION node -- which is what names the hero tree and is written with the
#: choice bit set.
TRAITS = [
    trait(100, 1000),
    trait(101, 1001, max_ranks=2),
    trait(102, 1002, spec_ids=(FIRE,)),
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

NODES = talenttree.nodes_for_class(TRAITS, CLASS_ID)


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


def encode(spec_id: int, picks: dict[int, tuple[int, int]], version: int = 2) -> str:
    """``{node id: (choice index, rank)}`` -> a loadout string.

    Every selected node is written as purchased, partially ranked and choosing an
    entry, which is the longest of the encodings the reader accepts and therefore
    the one that exercises the most of it.
    """
    writer = _BitWriter()
    writer.write(version, talenttree.VERSION_BITS)
    writer.write(spec_id, talenttree.SPEC_BITS)
    writer.write(0, talenttree.TREE_BITS)  # simc writes the tree hash as zeros
    for node_id in sorted(NODES):
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


ARCANE_BUILD = {100: (0, 1), 101: (0, 2), 103: (0, 1)}
#: Node 102 is the one only Fire may take, so this is a build the spec rule lets
#: through for Fire and would refuse for Arcane.
FIRE_BUILD = {102: (0, 1), 103: (0, 1)}


def tables() -> harvest.TalentTables:
    return harvest.TalentTables(
        nodes={CLASS_ID: NODES},
        spec_ids={("Mage", "Arcane"): ARCANE, ("Mage", "Fire"): FIRE},
        sub_trees={77: "Sunfury", 88: "Spellslinger"},
    )


def test_the_synthetic_encoder_round_trips_through_the_shipped_decoder():
    """Control. Everything else here rests on these hashes being real ones."""
    loadout = talenttree.decode_loadout(encode(ARCANE, ARCANE_BUILD), NODES)
    assert loadout.version == 2
    assert loadout.spec_id == ARCANE
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

DPS_ROW = {
    "name": "Somebody",
    "id": 12,
    "guid": 999,
    "type": "Mage",
    "server": "Somewhere",
    "icon": "Mage-Arcane",
    "specs": [{"spec": "Arcane", "role": "dps"}],
    "minItemLevel": 330,
    "maxItemLevel": 340,
    "combatantInfo": {"gear": [GEAR_ENTRY]},
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
    row = dict(DPS_ROW, specs=[{"spec": "Arcane"}, {"spec": "Fire"}])
    assert harvest.spec_of_row(row) is None
    assert harvest.spec_of_row(dict(DPS_ROW, specs=[])) == "Arcane"  # falls back to the icon


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


def observation(hash_string: str | None, spec: str = "Arcane", **overrides) -> harvest.Observation:
    base = dict(
        report="aBcD1234",
        fight_id=7,
        actor_id=12,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        wow_class="Mage",
        spec=spec,
        item_level=340.0,
        talent_hash=hash_string,
    )
    base.update(overrides)
    return harvest.Observation(**base)


def test_a_loadable_build_passes_all_four_checks():
    verdict = harvest.validate(observation(encode(ARCANE, ARCANE_BUILD)), tables())
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
    verdict = harvest.validate(observation(encode(ARCANE, ARCANE_BUILD, version=3)), tables())
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
    verdict = harvest.validate(observation(encode(FIRE, ARCANE_BUILD)), tables())
    assert verdict.reason == harvest.REASON_SPEC_MISMATCH
    assert "63" in verdict.detail


def test_simcs_own_spec_rule_is_applied_offline():
    """Node 102 belongs to Fire. Harvesting a build simc will not load would waste
    the whole downstream sweep, so it is decided here rather than by running simc."""
    verdict = harvest.validate(observation(encode(ARCANE, {**ARCANE_BUILD, 102: (0, 1)})), tables())
    assert verdict.reason == harvest.REASON_SPEC_RULE
    assert "not available to player's spec" in verdict.detail


def test_a_spec_simc_does_not_name_is_reported_rather_than_assumed():
    verdict = harvest.validate(
        observation(encode(ARCANE, ARCANE_BUILD), spec="Frostfire"), tables()
    )
    assert verdict.reason == harvest.REASON_UNKNOWN_SPEC


def test_two_hash_strings_for_one_loadout_are_one_build():
    """simc writes the 128-bit tree hash as zeros and skips it on parse, so two
    exports of one build need not be the same string. Keying on the string would
    report thirty copies of one build as thirty builds -- which is precisely the
    number this command exists to produce."""
    same = encode(ARCANE, ARCANE_BUILD)
    # A different tree hash, same selections: still one build.
    writer = _BitWriter()
    writer.write(2, talenttree.VERSION_BITS)
    writer.write(ARCANE, talenttree.SPEC_BITS)
    writer.write(1, talenttree.TREE_BITS)
    tail = _BitWriter()
    for node_id in sorted(NODES):
        if node_id not in ARCANE_BUILD:
            tail.write(0, 1)
            continue
        index, rank = ARCANE_BUILD[node_id]
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
    (build,) = builds["mage_arcane"]
    assert build.seen_in == 2


def test_a_different_loadout_is_a_different_build():
    builds, _ = harvest.group_builds(
        [
            observation(encode(ARCANE, ARCANE_BUILD)),
            observation(encode(ARCANE, {100: (0, 1), 103: (1, 1)}), actor_id=13),
        ],
        tables(),
    )
    assert len(builds["mage_arcane"]) == 2
    assert {b.hero_tree(tables())[1] for b in builds["mage_arcane"]} == {"Sunfury", "Spellslinger"}


def test_the_distinct_build_count_is_published_per_spec():
    """That number is itself the finding: one means a settled spec, ten over ten
    kills means there is no consensus to harvest."""
    document = harvest.build_document(
        "MID2",
        5,
        [
            observation(encode(ARCANE, ARCANE_BUILD)),
            observation(encode(ARCANE, ARCANE_BUILD), actor_id=13),
            observation(encode(ARCANE, {100: (0, 1), 103: (1, 1)}), actor_id=14),
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
            observation(encode(ARCANE, ARCANE_BUILD)),
            observation(encode(ARCANE, ARCANE_BUILD), actor_id=13, killed_at_ms=1_755_864_000_000),
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
        "MID2", 5, [observation(encode(ARCANE, ARCANE_BUILD))], tables(), encounters=[]
    )
    (source,) = document["specs"][0]["builds"][0]["sources"]
    assert source["report"] == "aBcD1234"
    assert source["fightID"] == 7
    assert source["actorID"] == 12
    assert source["killedAt"].startswith("2025-08-12")


def test_no_character_or_server_name_is_written_to_disk():
    """The artefact is a build, not a person. The names are dropped at extraction
    rather than filtered at publication, so they never reach the document at all."""
    found, _, _ = harvest.observations_from_fight(
        details_payload(),
        {12: encode(ARCANE, ARCANE_BUILD)},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        inventory={250215: equipment.INVENTORY_TYPES["trinket"]},
    )
    document = harvest.build_document("MID2", 5, found, tables(), encounters=[])
    blob = json.dumps(document)
    assert "Somebody" not in blob
    assert "Somewhere" not in blob
    # ... while the observation itself is complete enough to be re-opened.
    assert '"actorID": 12' in json.dumps(document, indent=2)


def test_a_kill_yields_every_damage_player_and_its_gear():
    second = dict(DPS_ROW, id=13, name="Else", icon="Mage-Fire", specs=[{"spec": "Fire"}])
    found, buckets, unresolved = harvest.observations_from_fight(
        details_payload([DPS_ROW, second]),
        {12: encode(ARCANE, ARCANE_BUILD), 13: encode(FIRE, ARCANE_BUILD)},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="Nek'zali the Soulcoiler",
        difficulty=5,
        killed_at_ms=1_755_000_000_000,
        inventory={250215: equipment.INVENTORY_TYPES["trinket"]},
    )
    assert [o.spec_key for o in found] == ["mage_arcane", "mage_fire"]
    assert buckets == ["dps", "healers", "tanks"]
    assert unresolved == ()
    assert found[0].gear[0].slot == "trinket1"
    assert found[0].item_level == 340.0


def test_a_spec_filter_narrows_the_file_and_nothing_else():
    """The queries are per kill, not per player, so this costs exactly the same."""
    second = dict(DPS_ROW, id=13, icon="Mage-Fire", specs=[{"spec": "Fire"}])
    found, _, _ = harvest.observations_from_fight(
        details_payload([DPS_ROW, second]),
        {},
        report="aBcD1234",
        fight_id=7,
        encounter_id=3470,
        encounter_name="x",
        difficulty=5,
        killed_at_ms=1,
        inventory={},
        only_specs=("mage_fire",),
    )
    assert [o.spec_key for o in found] == ["mage_fire"]


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

#: The fields these two queries touch, transcribed from the published v2 schema as
#: mirrored by three independent third-party clients on 2026-08-23. It is **not**
#: the live schema and this test is not a verification against Warcraft Logs -- the
#: first live run is still the schema check, exactly as it was for every other query
#: in this repository. What it does pin is the shape this code assumes: a query that
#: grows a field nobody has established exists fails here rather than in CI an hour
#: into a pass.
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
  playerDetails(fightIDs: [Int], killType: KillType, translate: Boolean): JSON
}
type ReportData { report(code: String!): Report }
type RateLimitData { limitPerHour: Int! pointsSpentThisHour: Float! pointsResetIn: Int! }
type Query { reportData: ReportData rateLimitData: RateLimitData }
"""


def test_the_harvest_queries_match_the_schema_shape_they_assume():
    graphql = pytest.importorskip("graphql")
    from wowdps.warcraftlogs import PLAYER_DETAILS_QUERY

    schema = graphql.build_schema(SCHEMA_EXCERPT)
    for label, document in (
        ("playerDetails", PLAYER_DETAILS_QUERY),
        ("talentImportCode", talent_codes_query([12, 7])),
    ):
        errors = graphql.validate(schema, graphql.parse(document))
        assert not errors, f"{label}: {[str(error) for error in errors]}"


# --------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------


def test_a_quiet_re_run_leaves_the_file_alone(tmp_path):
    """Same rule as the manifest and fights.json: a wall-clock stamp that rewrites
    itself every run means every run commits, and 'a diff means something moved'
    stops being true. `cost` travels with the stamp because it measures the run and
    not the game -- a harvest served out of a warm cache costs nothing and would
    otherwise rewrite the file to say so."""
    rows = [observation(encode(ARCANE, ARCANE_BUILD))]
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
            "MID2", 5, [observation(encode(ARCANE, ARCANE_BUILD))], tables(), encounters=[]
        ),
    )
    before = json.loads(path.read_text())
    changed = harvest.build_document(
        "MID2",
        5,
        [
            observation(encode(ARCANE, ARCANE_BUILD)),
            observation(encode(ARCANE, {100: (0, 1), 103: (1, 1)}), actor_id=14),
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

    def __init__(self, kills, roster):
        self.kills = kills
        self.roster = roster
        self.calls: list[str] = []

    def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
        self.calls.append(f"rankings:{encounter_id}:{page}")
        return {
            "id": encounter_id,
            "name": "Nek'zali the Soulcoiler",
            "characterRankings": {
                "rankings": [
                    {
                        "startTime": 1_755_000_000_000 + index,
                        "report": {"code": code, "fightID": fight},
                    }
                    for index, (code, fight) in enumerate(self.kills)
                ]
            },
        }

    def player_details(self, code, fight_id):
        self.calls.append(f"player-details:{code}:{fight_id}")
        return details_payload(self.roster)

    def talent_import_codes(self, code, fight_id, actor_ids):
        self.calls.append(f"talent-codes:{code}:{fight_id}:{len(actor_ids)}")
        return {
            actor: encode(ARCANE, ARCANE_BUILD) if actor == 12 else encode(FIRE, FIRE_BUILD)
            for actor in actor_ids
        }

    #: `check_budget` reads this. A stub that reported no budget at all would be
    #: exercising a different branch than a real run does.
    ledger = warcraftlogs.PointLedger(limit_per_hour=3600, first_reading=0.0, last_reading=1.0)


def test_the_sweep_runs_end_to_end_and_costs_two_report_queries_per_kill():
    roster = [DPS_ROW, dict(DPS_ROW, id=13, icon="Mage-Fire", specs=[{"spec": "Fire"}])]
    client = StubClient([("aBcD1234", 7), ("eFgH5678", 3)], roster)
    settings = harvest.HarvestSettings(
        encounter_ids=(3470,), difficulty=5, reports=2, rankings_pages=1
    )
    plan = harvest.QueryPlan()

    found, summary, buckets = harvest.harvest_encounter(
        client, 3470, settings, {250215: equipment.INVENTORY_TYPES["trinket"]}, plan
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
    assert {row["specId"] for row in document["specs"]} == {"mage_arcane", "mage_fire"}
    assert document["coverage"]["rejectedTotal"] == 0
    # Two players of one spec ran the same build in two different kills.
    arcane = next(row for row in document["specs"] if row["specId"] == "mage_arcane")
    assert arcane["distinctBuilds"] == 1
    assert arcane["builds"][0]["seenInKills"] == 2


def test_an_encounter_with_fewer_kills_than_asked_for_says_so():
    """A fact about the encounter, not a failure of the pass -- the same distinction
    the fight probe draws with `searchExhausted`."""
    client = StubClient([("aBcD1234", 7)], [DPS_ROW])
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=30)
    _, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, harvest.QueryPlan())
    assert summary["killsRead"] == 1
    assert summary["fewerKillsThanRequested"] is True


def test_a_kill_whose_roster_is_empty_costs_no_talent_query():
    """Asking for the talent codes of nobody is a GraphQL error, not an empty
    answer, and it would abort a pass over one bad report."""
    client = StubClient([("aBcD1234", 7)], [])
    plan = harvest.QueryPlan()
    settings = harvest.HarvestSettings(encounter_ids=(3470,), difficulty=5, reports=1)
    found, summary, _ = harvest.harvest_encounter(client, 3470, settings, {}, plan)
    assert found == []
    assert plan.player_details == 1
    assert plan.talent_codes == 0


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
        observation(build["talentHash"], spec="Fire", wow_class="Mage"), tables
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

    def __init__(self):
        super().__init__([("aBcD1234", 7)], [DPS_ROW])
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
    assert document["specs"][0]["specId"] == "mage_arcane"
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
    assert "mage_arcane: ok" in out


def test_missing_credentials_stop_the_run_rather_than_half_running_it(tmp_path, monkeypatch):
    def refuse(cls):
        raise warcraftlogs.WarcraftLogsError("WCL_CLIENT_ID and WCL_CLIENT_SECRET must be set.")

    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", classmethod(refuse))
    assert harvest.cmd_harvest_builds(_args(tmp_path)) == 2
    assert not (tmp_path / "MID2").exists()
