"""What a tier set and an outside Power Infusion are worth.

Both are toggles against a spec's own profile, so both are differences rather than
levels. These tests pin the three decisions that make the numbers mean something.
"""

from __future__ import annotations

from pathlib import Path

from wowdps import buffsweep
from wowdps.simc_runner import ProfilesetResult

SETS_INC = """// Set bonus data
static constexpr std::array<item_set_bonus_t, 3> __set_bonus_data { {
  { "Jade Warlord's Dominion", "midnight_season_2", "MID2", 18, 1957, 2, 1, -1, -1, 1, {0} },
  { "Jade Warlord's Dominion", "midnight_season_2", "MID2", 18, 1957, 4, 1, -1, -1, 2, {0} },
  { "Old Thing", "midnight_season_1", "MID1", 17, 1900, 2, 1, -1, -1, 3, {0} },
} };
"""


def write_sets(tmp_path: Path) -> Path:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "item_set_bonus.inc").write_text(SETS_INC, encoding="utf-8")
    return tmp_path


def test_a_set_is_one_entry_per_class_not_one_per_bonus_row(tmp_path):
    """The table has a row per (set, class, spec, 2pc/4pc); the sweep wants the set."""
    sets = buffsweep.parse_tier_sets(write_sets(tmp_path))

    assert [entry.name for entry in sets] == ["Old Thing", "Jade Warlord's Dominion"]
    assert buffsweep.sets_for_tier(sets, "MID2")[0].option == "midnight_season_2"
    assert [entry.tier for entry in buffsweep.sets_for_tier(sets, "MID1")] == ["MID1"]


def test_the_setless_variant_is_an_override_not_an_absence():
    """A profile that already wears the set would otherwise measure a gain of zero."""
    keys = {variant.key: variant.options for variant in buffsweep.set_variants("x")}

    assert keys["set__none"] == ("set_bonus=x_2pc=0", "set_bonus=x_4pc=0")
    assert keys["set__2pc"] == ("set_bonus=x_2pc=1", "set_bonus=x_4pc=0")
    assert keys["set__4pc"] == ("set_bonus=x_2pc=1", "set_bonus=x_4pc=1")


def test_the_four_piece_is_reported_over_the_two_piece():
    """Nobody chooses between four pieces and none; they choose the third and fourth."""
    result = buffsweep.BuffResult(
        spec_id="a", display_name="A", wow_class="Mage", spec="Fire", hero_talent="Sunfury"
    )
    measured = {
        "set__none": ProfilesetResult(key="set__none", dps=100_000, dps_error=0.1, iterations=3000),
        "set__2pc": ProfilesetResult(key="set__2pc", dps=104_000, dps_error=0.1, iterations=3000),
        "set__4pc": ProfilesetResult(key="set__4pc", dps=110_000, dps_error=0.1, iterations=3000),
    }

    filled = buffsweep._read_sets(result, measured)

    assert filled.base_dps == 100_000
    assert filled.two_piece_gain == 4_000
    # 110k over the 104k two-piece, not over the 100k baseline.
    assert filled.four_piece_gain == 6_000
    published = filled.to_json()
    assert published["twoPiecePercent"] == 0.04
    assert published["fourPiecePercent"] == 0.06


def test_power_infusion_is_a_usage_pattern_rather_than_a_count():
    """The option takes times, so the pattern is the model and has to be published."""
    assert buffsweep.power_infusion_times(300) == (0.0, 120.0, 240.0)
    # A fight too short for a second cast gets one, and one too short for any still
    # gets the pull cast rather than an empty option that would silently disable it.
    assert buffsweep.power_infusion_times(100) == (0.0,)
    assert buffsweep.power_infusion_times(10) == (0.0,)

    options = {v.key: v.options for v in buffsweep.power_infusion_variants((0.0, 120.0))}
    assert options["pi__none"] == ("external_buffs.power_infusion=",)
    assert options["pi__oncooldown"] == ("external_buffs.power_infusion=0/120",)


def test_a_class_with_no_set_for_the_tier_gets_none_rather_than_another_class_set(tmp_path):
    from wowdps.profiles import SpecProfile

    sets = buffsweep.sets_for_tier(buffsweep.parse_tier_sets(write_sets(tmp_path)), "MID2")
    warrior = SpecProfile(
        path=Path("x"),
        tier="MID2",
        wow_class="Warrior",
        spec="Fury",
        hero_talent=None,
        role="attack",
        talent_hash=None,
    )
    mage = SpecProfile(
        path=Path("x"),
        tier="MID2",
        wow_class="Mage",
        spec="Fire",
        hero_talent=None,
        role="spell",
        talent_hash=None,
    )

    # class_id 1 is Warrior in simc's player_e order, which is what the table keys on.
    assert buffsweep.class_id_of(warrior, sets) is not None
    assert buffsweep.class_id_of(mage, sets) is None
