"""The pool is derived from the journal, not inferred from item levels.

The case these tests are built around is the one that motivated the module: three
Midnight dungeon trinkets that are field-for-field identical to the current ones in
simc's table, and are last season's. No rule over simc's data can separate them, so
the only test that means anything is whether the *journal* separates them.
"""

from wowdps.equipment import DiscoveredItem, GearItem
from wowdps.gearpool import (
    MIN_RAID_ENCOUNTER_MATCH,
    build_pool,
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


def test_the_raid_is_found_by_matching_the_tiers_own_boss_list():
    """No hard-coded instance id: a new tier needs its bosses listed and nothing else."""
    index = index_of(
        drop(1, "A", "Imperator Averzian", 1300, "Crucible of Night", "raid"),
        drop(2, "B", "Vorasius", 1300, "Crucible of Night", "raid"),
        drop(3, "C", "Some Old Boss", 900, "A Previous Raid", "raid"),
    )
    match = identify_tier_raid(index, ["Imperator Averzian", "Vorasius"])
    assert match is not None
    assert match.instance_id == 1300
    assert match.share == 1.0


def test_names_that_differ_only_in_punctuation_or_an_epithet_still_match():
    """Warcraft Logs and the journal do not spell a boss the same way."""
    index = index_of(
        drop(1, "A", "Chimaerus, the Undreamt God", 1300, "Crucible", "raid"),
        drop(2, "B", "Vaelgor and Ezzorak", 1300, "Crucible", "raid"),
    )
    match = identify_tier_raid(index, ["Chimaerus", "Vaelgor & Ezzorak"])
    assert match is not None
    assert len(match.matched) == 2


def test_a_raid_that_matches_almost_nothing_is_refused():
    """The most damaging failure available: a well-formed pool from the wrong raid."""
    index = index_of(drop(1, "A", "Some Old Boss", 900, "A Previous Raid", "raid"))
    assert identify_tier_raid(index, ["Imperator Averzian", "Vorasius", "Belo'ren"]) is None
    assert MIN_RAID_ENCOUNTER_MATCH == 0.5


def test_an_unmatched_boss_is_reported_rather_than_swallowed():
    index = index_of(
        drop(1, "A", "Imperator Averzian", 1300, "Crucible", "raid"),
        drop(2, "B", "Vorasius", 1300, "Crucible", "raid"),
    )
    match = identify_tier_raid(index, ["Imperator Averzian", "Vorasius", "Belo'ren"])
    assert match is not None
    assert match.missing == ("Belo'ren",)


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
        ["Vorasius"],
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
        ["Vorasius"],
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
        ["Vorasius"],
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
        ["Vorasius"],
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
        "MID2", "finger", index, rotation_of((1200, "D")), [item(1, "Ring", 219, 4)], ["Vorasius"]
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
        ["Vorasius"],
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
