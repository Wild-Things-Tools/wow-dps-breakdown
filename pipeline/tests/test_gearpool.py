"""The pool is derived from the journal, not inferred from item levels.

The case these tests are built around is the one that motivated the module: three
Midnight dungeon trinkets that are field-for-field identical to the current ones in
simc's table, and are last season's. No rule over simc's data can separate them, so
the only test that means anything is whether the *journal* separates them.
"""

from wowdps.equipment import DiscoveredItem, GearItem
from wowdps.gearpool import (
    build_pool,
    equipped_item_ids,
    identify_tier_raid,
    render,
)
from wowdps.lootsources import ItemDrop, LootIndex, Rotation, RotationDungeon


def item(item_id: int, name: str, ilevel: int = 108, quality: int = 3) -> DiscoveredItem:
    return DiscoveredItem(
        item_id=item_id,
        name=name,
        slug=name.lower().replace(" ", "_").replace("'", ""),
        base_ilevel=ilevel,
        base_quality=quality,
        primary_stat=None,
        secondary_stat="crit",
        has_effect=True,
    )


def drop(item_id: int, name: str, encounter: str, instance_id: int, instance: str, kind: str):
    return ItemDrop(
        item_id=item_id,
        item_name=name,
        encounter_id=hash(encounter) % 10000,
        encounter=encounter,
        instance_id=instance_id,
        instance=instance,
        expansion="Midnight",
        kind=kind,
    )


def index_of(*drops: ItemDrop) -> LootIndex:
    index = LootIndex(encounters_read=len(drops), encounters_offered=len(drops))
    for one in drops:
        index.add(one)
    return index


def rotation_of(*instances: tuple[int, str]) -> Rotation:
    return Rotation(
        dungeons=tuple(
            RotationDungeon(keystone_id=500 + i, name=name, instance_id=iid, instance=name)
            for i, (iid, name) in enumerate(instances)
        )
    )


# --------------------------------------------------------------------------------
# Identifying the tier's raid
# --------------------------------------------------------------------------------


def test_the_raid_is_the_one_that_drops_what_the_tiers_profiles_wear():
    """Derived from simc, not from a boss list, and this is the case that forced it.

    The first version matched the tier's bosses from fight_profiles.json against the
    journal. That is what Warcraft Logs currently has kills for, so in the week before
    a season turns it names the raid that is *ending* -- for MID2 it named The
    Voidspire while the profiles were geared from The Venomous Abyss, and duly
    reported the correct trinket pool as wrong.
    """
    index = index_of(
        drop(
            270164, "Gebbo's Bottomless Bag", "The Twin Fangs", 1300, "The Venomous Abyss", "raid"
        ),
        drop(
            270160,
            "First Mate's Shellward",
            "The Lost Explorers",
            1300,
            "The Venomous Abyss",
            "raid",
        ),
        drop(999, "Something", "Imperator Averzian", 1400, "The Voidspire", "raid"),
    )
    raid = identify_tier_raid(index, [270164, 270160])
    assert raid is not None
    assert raid.names == ("The Venomous Abyss",)
    assert 1400 not in raid.instance_ids


def test_a_raid_spanning_two_instances_is_kept_whole():
    """MID2's raid is The Venomous Abyss *and* The Tidebound Grotto. A single-instance
    answer silently drops whatever the second one contributes."""
    index = index_of(
        drop(270160, "A", "The Lost Explorers", 1300, "The Venomous Abyss", "raid"),
        drop(270164, "B", "The Twin Fangs", 1300, "The Venomous Abyss", "raid"),
        drop(
            270167,
            "Wavecaller's Seastone",
            "Nymrissa Wavecaller",
            1301,
            "The Tidebound Grotto",
            "raid",
        ),
    )
    raid = identify_tier_raid(index, [270160, 270164, 270167])
    assert raid is not None
    assert set(raid.names) == {"The Venomous Abyss", "The Tidebound Grotto"}


def test_a_legacy_item_does_not_drag_an_old_raid_in():
    """MID2's Arcane Mage wears a Legion ring. The instance that drops it is a raid
    with a genuine hit, and it is not part of this tier."""
    old = ItemDrop(
        item_id=159459,
        item_name="Ritual Binder's Ring",
        encounter_id=9,
        encounter="Some Legion Boss",
        instance_id=900,
        instance="Antorus",
        expansion="Legion",
        kind="raid",
    )
    index = index_of(
        drop(270160, "A", "The Lost Explorers", 1300, "The Venomous Abyss", "raid"),
        drop(270164, "B", "The Twin Fangs", 1300, "The Venomous Abyss", "raid"),
        old,
    )
    raid = identify_tier_raid(index, [270160, 270164, 159459])
    assert raid is not None
    assert raid.names == ("The Venomous Abyss",)
    assert [hit.instance for hit in raid.legacy] == ["Antorus"]


def test_no_raid_at_all_when_nothing_the_tier_wears_is_placed():
    index = index_of(drop(1, "A", "A boss", 1200, "Murder Row", "dungeon"))
    assert identify_tier_raid(index, [270160]) is None
    assert identify_tier_raid(index, []) is None


def test_equipped_item_ids_reads_gear_lines_only(tmp_path):
    """A profile carries `id=` inside other options too; reading those would place
    items nobody wears."""
    directory = tmp_path / "profiles" / "MID2"
    directory.mkdir(parents=True)
    (directory / "MID2_Mage_Arcane.simc").write_text(
        'mage="MID2_Mage_Arcane"\n'
        "neck=aqirbane_reliquary,id=268265,bonus_id=13335,ilevel=344\n"
        "trinket1=freightrunners_flask,id=250215,bonus_id=12854,ilevel=334\n"
        "main_hand=janthrazet,id=271092,ilevel=344\n"
        "shoulders=venomkeepers_mantle,id=271876,ilevel=344\n"
        "wrist=silvermoon_agents_deflectors,id=244576,ilevel=285\n"
        "# a comment mentioning id=111111\n"
        "actions+=/arcane_blast,if=buff.x.up&id=222222\n",
        encoding="utf-8",
    )
    found = equipped_item_ids(tmp_path, "MID2")
    assert found == {268265, 250215, 271092, 271876, 244576}


def test_equipped_item_ids_reads_both_spellings_of_a_slot(tmp_path):
    """This used to be a third parser of simc gear lines, and it read the other half.

    Its own alternation listed ``shoulder`` and ``wrist`` where
    ``gearanchor.GEAR_SLOTS`` listed ``shoulders`` and ``wrists``, so each dropped
    exactly what the other read and neither said so. Measured on simc 69a46e1 before
    the merge: **204 of MID2's 227** equipped item ids and 176 of MID1's 203 -- a
    tenth of the evidence for which raid a tier belongs to.
    """
    directory = tmp_path / "profiles" / "MID2"
    directory.mkdir(parents=True)
    (directory / "plural.simc").write_text(
        "shoulders=a,id=1,ilevel=344\nwrists=b,id=2,ilevel=344\n", encoding="utf-8"
    )
    (directory / "singular.simc").write_text(
        "shoulder=c,id=3,ilevel=344\nwrist=d,id=4,ilevel=344\n", encoding="utf-8"
    )

    assert equipped_item_ids(tmp_path, "MID2") == {1, 2, 3, 4}


def test_a_tier_with_no_profiles_yields_nothing_rather_than_guessing(tmp_path):
    assert equipped_item_ids(tmp_path, "MID9") == frozenset()


# --------------------------------------------------------------------------------
# Building the pool
# --------------------------------------------------------------------------------


def test_last_seasons_trinket_is_dropped_and_this_seasons_is_kept():
    """The whole point. Both items are identical to simc; the journal separates them."""
    index = index_of(
        drop(270160, "First Mate's Shellward", "Vorasius", 1300, "Crucible", "raid"),
        drop(250215, "Freightrunner's Flask", "Boss", 1200, "Altar of Fangs", "dungeon"),
        drop(250144, "Emberwing Feather", "Boss", 1201, "Last Season's Hall", "dungeon"),
    )
    build = build_pool(
        "MID2",
        "trinket",
        index,
        rotation_of((1200, "Altar of Fangs")),
        [
            item(270160, "First Mate's Shellward", 219, 4),
            item(250215, "Freightrunner's Flask"),
            item(250144, "Emberwing Feather"),
        ],
        [270160],
    )
    assert build.usable
    assert [i.item_id for i in build.by_source("raid")] == [270160]
    assert [i.item_id for i in build.by_source("mythicplus")] == [250215]
    assert [r.item_id for r in build.out_of_season] == [250144]
    # The reason travels with it: a checkable claim, not a bare id.
    assert "Last Season's Hall" in build.out_of_season[0].detail


def test_a_rotation_dungeon_from_an_older_expansion_is_picked_up():
    """The failure a blacklist could never fix: you cannot name your way *into* a pool.

    Ruby Life Pools is a Dragonflight dungeon in Midnight Season 2's rotation, so its
    trinket carries an id and a base item level from a different block entirely -- the
    structural rule misses it, and the journal does not.
    """
    index = index_of(
        drop(270160, "Raid Trinket", "Vorasius", 1300, "Crucible", "raid"),
        drop(190510, "Whispering Incarnate Icon", "Boss", 1202, "Ruby Life Pools", "dungeon"),
    )
    build = build_pool(
        "MID2",
        "trinket",
        index,
        rotation_of((1202, "Ruby Life Pools")),
        [item(270160, "Raid Trinket", 219, 4), item(190510, "Whispering Incarnate Icon", 40, 4)],
        [270160],
    )
    assert [i.item_id for i in build.by_source("mythicplus")] == [190510]
    assert build.by_source("mythicplus")[0].instance == "Ruby Life Pools"


def test_an_item_the_journal_never_placed_is_reported_not_guessed():
    index = index_of(drop(270160, "Raid Trinket", "Vorasius", 1300, "Crucible", "raid"))
    build = build_pool(
        "MID2",
        "trinket",
        index,
        rotation_of((1200, "Altar of Fangs")),
        [item(270160, "Raid Trinket", 219, 4), item(12345, "Some Vanilla Trinket", 40, 2)],
        [270160],
    )
    assert [r.item_id for r in build.unplaced] == [12345]
    assert 12345 not in {i.item_id for i in build.items}


def test_a_truncated_walk_refuses_to_build_a_pool():
    """An unread dungeon and an out-of-season one are indistinguishable, and the
    result would be quietly too small with a perfectly plausible shape."""
    index = index_of(drop(270160, "Raid Trinket", "Vorasius", 1300, "Crucible", "raid"))
    index.truncated = True
    build = build_pool(
        "MID2",
        "trinket",
        index,
        rotation_of((1200, "Altar of Fangs")),
        [item(270160, "Raid Trinket", 219, 4)],
        [270160],
    )
    assert not build.usable
    assert any("stopped early" in w for w in build.warnings)


def test_a_missing_rotation_refuses_rather_than_publishing_every_dungeon():
    index = index_of(drop(270160, "Raid Trinket", "Vorasius", 1300, "Crucible", "raid"))
    build = build_pool(
        "MID2", "trinket", index, Rotation(), [item(270160, "Raid Trinket", 219, 4)], ["Vorasius"]
    )
    assert not build.usable
    assert any("rotation" in w for w in build.warnings)


def test_gems_and_enchants_survive_a_rebuild():
    """Measured at twelve times a ten-item-level step, so losing one silently would
    move every number in the comparison."""
    index = index_of(drop(1, "Ring", "Vorasius", 1300, "Crucible", "raid"))
    build = build_pool(
        "MID2", "finger", index, rotation_of((1200, "D")), [item(1, "Ring", 219, 4)], [1]
    )
    carried = GearItem(
        item_id=1,
        name="Ring",
        slug="ring",
        primary_stat=None,
        secondary_stat=None,
        source="raid",
        base_ilevel=219,
        base_quality=4,
        gem_ids=(240892,),
        enchant_id=7346,
    )
    rebuilt = build.items[0].to_gear_item(carried)
    assert rebuilt.gem_ids == (240892,)
    assert rebuilt.enchant_id == 7346


def test_the_report_shows_the_diff_against_the_curated_pool():
    """What somebody reads before deciding to write: what left, what arrived, why."""
    index = index_of(
        drop(270160, "Raid Trinket", "Vorasius", 1300, "Crucible", "raid"),
        drop(250144, "Emberwing Feather", "Boss", 1201, "Last Season's Hall", "dungeon"),
    )
    build = build_pool(
        "MID2",
        "trinket",
        index,
        rotation_of((1200, "Altar of Fangs")),
        [item(270160, "Raid Trinket", 219, 4), item(250144, "Emberwing Feather")],
        [270160],
    )
    previous = (
        GearItem(
            item_id=250144,
            name="Emberwing Feather",
            slug="emberwing_feather",
            primary_stat=None,
            secondary_stat=None,
            source="mythicplus",
            base_ilevel=108,
            base_quality=3,
        ),
    )
    text = "\n".join(render(build, previous))
    assert "1 added, 1 removed" in text
    assert "Emberwing Feather" in text
    assert "Last Season's Hall" in text


def test_a_slot_with_no_previous_pool_reports_everything_as_added():
    """Deriving a ring pool from scratch must not look like a diff against nothing."""
    index = index_of(drop(1, "Ring", "Vorasius", 1300, "Crucible", "raid"))
    build = build_pool(
        "MID2", "finger", index, rotation_of((1200, "D")), [item(1, "Ring", 219, 4)], [1]
    )
    text = "\n".join(render(build, ()))
    assert "1 item(s)" in text
