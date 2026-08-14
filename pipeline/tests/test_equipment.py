"""Item pools, eligibility and the two things that are read rather than assumed.

Two facts about the gear sweep are worth pinning here because getting them wrong
produces plausible-looking numbers rather than an error:

* **Eligibility** is about the item's *primary* stat budget. A Strength trinket on a
  Mage sims fine and reports a number; the number is just meaningless, because the
  item's main stat is dead weight. Combined stats ("str_agi_int") resolve to whatever
  the wearer uses and so are eligible everywhere.
* **The baseline is worn at the lower item level.** Mythic+ gear tops out below
  Mythic raid gear, and pricing the thing being compared against at the raid's top
  level would flatter every raid drop.
"""

from __future__ import annotations

import json

import pytest

from wowdps import equipment
from wowdps.equipment import TRINKET, GearItem, ItemLevel, SlotPool


def item(item_id: int, stat: str | None, source: str = "raid", **kwargs) -> GearItem:
    return GearItem(
        item_id=item_id,
        name=f"Item {item_id}",
        slug=f"item_{item_id}",
        primary_stat=stat,
        secondary_stat=kwargs.pop("secondary", None),
        source=source,
        base_ilevel=219,
        base_quality=4,
        **kwargs,
    )


# --------------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stat", "intellect", "agility", "strength"),
    [
        ("intellect", True, False, False),
        ("agility", False, True, False),
        ("strength", False, False, True),
        # Combined stats resolve to whatever the wearer uses.
        ("str_agi_int", True, True, True),
        ("agi_int", True, True, False),
        ("str_agi", False, True, True),
        ("str_int", True, False, True),
        # Secondary-only trinkets have no primary budget to waste.
        (None, True, True, True),
    ],
)
def test_eligibility_follows_the_primary_stat(stat, intellect, agility, strength):
    candidate = item(1, stat)
    assert candidate.usable_by("intellect") is intellect
    assert candidate.usable_by("agility") is agility
    assert candidate.usable_by("strength") is strength


def test_unknown_primary_stat_excludes_everything_rather_than_guessing():
    assert item(1, "intellect").usable_by("spirit") is False


# --------------------------------------------------------------------------------
# simc option rendering
# --------------------------------------------------------------------------------


def test_simc_item_uses_the_bare_id_and_ilevel_form():
    # Verified equivalent to passing the profile's full bonus_id list: for trinkets
    # those ids only add quality, sockets and flavour text, and an explicit ilevel
    # overrides the scaling they would otherwise set.
    assert item(270164, "str_agi_int").simc_item("trinket1", 344) == (
        "trinket1=,id=270164,ilevel=344"
    )


def test_simc_item_keeps_bonus_ids_when_an_item_needs_them():
    # Rings and necks take sockets from bonus ids, and a socket is real stats.
    ring = item(268266, "intellect", bonus_ids=(13668,))
    assert ring.simc_item("finger1", 334) == "finger1=,id=268266,bonus_id=13668,ilevel=334"


# --------------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------------


def pool(**kwargs) -> SlotPool:
    defaults = dict(
        tier="MID2",
        slot=TRINKET,
        items=(
            item(1, "intellect", "raid"),
            item(2, "strength", "raid"),
            item(3, None, "raid"),
            item(4, "intellect", "mythicplus"),
            item(5, "agility", "mythicplus"),
        ),
        item_levels=(
            ItemLevel("mythic", "Mythic", 344),
            ItemLevel("heroic", "Heroic", 334),
        ),
        baseline_source="mythicplus",
        candidate_source="raid",
    )
    defaults.update(kwargs)
    return SlotPool(**defaults)


def test_pools_split_by_source_and_filter_by_stat():
    assert [i.item_id for i in pool().candidates("intellect")] == [1, 3]
    assert [i.item_id for i in pool().baseline_candidates("intellect")] == [4]
    assert [i.item_id for i in pool().baseline_candidates("agility")] == [5]


def test_baseline_is_priced_at_the_lower_item_level():
    # Declaration order deliberately puts the higher level first, so this cannot
    # pass by accident.
    assert pool().baseline_ilevel().ilevel == 334


# --------------------------------------------------------------------------------
# The shipped pool file
# --------------------------------------------------------------------------------


def test_shipped_pool_loads_and_covers_every_primary_stat():
    pools = equipment.load_pools("MID2")
    trinkets = pools.slots["trinket"]
    assert trinkets.baseline_source == "mythicplus"
    assert trinkets.candidate_source == "raid"

    for primary in ("intellect", "agility", "strength"):
        # A spec with fewer than two usable baseline items cannot form a baseline
        # pair at all, so the pool has to clear that bar for every primary stat.
        assert len(trinkets.baseline_candidates(primary)) >= 2, primary
        assert trinkets.candidates(primary), primary


def test_every_shipped_item_declares_a_source():
    pools = equipment.load_pools("MID2")
    sources = {item.source for item in pools.slots["trinket"].items}
    assert sources == {"raid", "mythicplus"}


def test_unknown_tier_says_how_to_add_one(tmp_path):
    path = tmp_path / "pools.json"
    path.write_text(json.dumps({"tiers": {}}), encoding="utf-8")
    with pytest.raises(KeyError, match="gear-candidates"):
        equipment.load_pools("MID9", path)


# --------------------------------------------------------------------------------
# Primary stat, read off the profile
# --------------------------------------------------------------------------------


def write_profile(tmp_path, body: str):
    path = tmp_path / "profile.simc"
    path.write_text(body, encoding="utf-8")
    return path


def test_primary_stat_comes_from_the_profiles_own_gear_summary(tmp_path):
    path = write_profile(tmp_path, "mage=X\nspec=arcane\n# Gear Summary\n# gear_intellect=2751\n")
    assert equipment.primary_stat(path) == "intellect"


def test_primary_stat_takes_the_largest_allocation(tmp_path):
    # Elemental Shaman really does carry 94 strength from an off-hand next to 2760
    # intellect; the largest allocation is the one the spec is built around.
    path = write_profile(
        tmp_path,
        "shaman=X\nspec=elemental\n# gear_strength=94\n# gear_intellect=2760\n",
    )
    assert equipment.primary_stat(path) == "intellect"


def test_primary_stat_refuses_to_guess_when_the_summary_is_missing(tmp_path):
    path = write_profile(tmp_path, "mage=X\nspec=arcane\n")
    with pytest.raises(ValueError, match="gear_"):
        equipment.primary_stat(path)


# --------------------------------------------------------------------------------
# Enumeration from simc's generated tables
# --------------------------------------------------------------------------------

ITEM_DATA_INC = """\
// Item stats
static const dbc_item_data_t::stats_t __item_stats_data[3] = {
  {  5,  6666, 0.000000f },
  {  7,  1000, 0.000000f },
  { 32,  6666, 0.000000f },
} };
static const std::array<dbc_item_data_t, 2> __item_data_chunk0 { {
  { "Test Trinket", 270161, 0x00080000, 0x00002000, 0x00, 219, 90, 0, 0, 4, 12, 4, 0, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[0], 2, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 0, 0, 0, 0 },
  { "Test Helm", 271874, 0x00080000, 0x00002000, 0x00, 219, 90, 0, 0, 4, 1, 4, 1, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[2], 1, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 0, 0, 0, 0 },
} };
"""

ITEM_EFFECT_INC = """\
// Item effects
static constexpr std::array<item_effect_t, 2> __item_effect_data { {
  { 233385, 1292291, 270161,   0,   1,    0,      -1,      -1 }, // Test Trinket
  { 116657,  317388,      0,   0,   1,    0,      -1,      -1 }, // no item
} };
"""


def fake_simc(tmp_path):
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "item_data.inc").write_text(ITEM_DATA_INC, encoding="utf-8")
    (generated / "item_effect.inc").write_text(ITEM_EFFECT_INC, encoding="utf-8")
    return tmp_path


def test_discover_items_filters_by_inventory_type_and_reads_stats(tmp_path):
    found = equipment.discover_items(fake_simc(tmp_path), equipment.INVENTORY_TYPES["trinket"])
    assert len(found) == 1
    trinket = found[0]
    assert trinket.item_id == 270161
    assert trinket.slug == "test_trinket"
    assert trinket.base_ilevel == 219
    assert trinket.base_quality == 4
    assert trinket.primary_stat == "intellect"
    # Stamina is on every item and says nothing about who can use it.
    assert trinket.secondary_stat is None
    assert trinket.has_effect is True


def test_discover_items_ignores_effect_rows_with_no_item(tmp_path):
    # Most item_effect rows carry item_id 0 because the effect attaches through a
    # bonus id. Counting those as "this item has an effect" would mark everything.
    found = equipment.discover_items(fake_simc(tmp_path), 1)
    assert [(i.item_id, i.has_effect) for i in found] == [(271874, False)]


def _pool_with(rotation, dungeons):
    """A two-item Mythic+ pool, so rotation filtering can be exercised directly."""
    from wowdps.equipment import SLOTS_BY_ID, GearItem, ItemLevel, SlotPool

    items = tuple(
        GearItem(
            item_id=1000 + index,
            name=f"Trinket {index}",
            slug=f"trinket_{index}",
            primary_stat=None,
            secondary_stat=None,
            source="mythicplus",
            base_ilevel=108,
            base_quality=3,
            dungeon=dungeon,
        )
        for index, dungeon in enumerate(dungeons)
    )
    return SlotPool(
        tier="MID2",
        slot=SLOTS_BY_ID["trinket"],
        items=items,
        item_levels=(ItemLevel(id="heroic", label="Heroic", ilevel=334, evidence=""),),
        baseline_source="mythicplus",
        candidate_source="raid",
        rotation=tuple(rotation),
    )


def test_an_undeclared_rotation_filters_nothing():
    """Dropping every item whose dungeon is merely unrecorded would empty the pool."""
    pool = _pool_with([], [None, "Some Dungeon"])
    assert len(pool.baseline_candidates("intellect")) == 2
    assert pool.rotation_is_stated() is False


def test_a_declared_rotation_drops_what_this_season_cannot_farm():
    pool = _pool_with(["Dungeon A"], ["Dungeon A", "Dungeon B"])
    kept = pool.baseline_candidates("intellect")
    assert [item.dungeon for item in kept] == ["Dungeon A"]


def test_a_declared_rotation_reports_items_nobody_placed():
    """An unplaced item is silently excluded, so it has to be countable."""
    pool = _pool_with(["Dungeon A"], ["Dungeon A", None])
    assert [item.item_id for item in pool.unplaced()] == [1001]
    assert len(pool.baseline_candidates("intellect")) == 1


def test_rotation_never_filters_the_raid_pool():
    """A raid drop is not gated on the dungeon rotation."""
    from wowdps.equipment import GearItem

    pool = _pool_with(["Dungeon A"], ["Dungeon A"])
    raid_item = GearItem(
        item_id=270160,
        name="Raid Trinket",
        slug="raid_trinket",
        primary_stat=None,
        secondary_stat=None,
        source="raid",
        base_ilevel=219,
        base_quality=4,
    )
    assert pool.in_rotation(raid_item) is True


def test_a_named_rotation_with_nothing_placed_still_filters_nothing():
    """Naming the dungeons before tagging the items must not empty the pool."""
    pool = _pool_with(["Dungeon A", "Dungeon B"], [None, None])
    assert pool.rotation_is_stated() is True
    assert len(pool.baseline_candidates("intellect")) == 2
    assert len(pool.unplaced()) == 2


def test_filtering_switches_on_once_something_is_placed():
    pool = _pool_with(["Dungeon A"], ["Dungeon A", None])
    kept = pool.baseline_candidates("intellect")
    assert [item.dungeon for item in kept] == ["Dungeon A"]


def test_a_derived_rotation_answer_beats_the_hand_written_one():
    """Once the API has spoken, neither the rotation list nor a dungeon matters."""
    from wowdps.equipment import DerivedSource

    pool = _pool_with(["Dungeon A"], ["Dungeon A", "Dungeon A"])
    kept, dropped = pool.items
    object.__setattr__(
        dropped,
        "derived",
        DerivedSource(
            source="mythicplus",
            encounter="Some Boss",
            encounter_id=1,
            instance="Dungeon Z",
            instance_id=2,
            expansion="Midnight",
            in_rotation=False,
        ),
    )
    ids = [item.item_id for item in pool.baseline_candidates("intellect")]
    assert dropped.item_id not in ids
    assert kept.item_id in ids
