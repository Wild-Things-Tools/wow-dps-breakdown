"""What a tier set and an outside Power Infusion are worth.

Both are toggles against a spec's own profile, so both are differences rather than
levels. These tests pin the three decisions that make the numbers mean something.
"""

from __future__ import annotations

from pathlib import Path

from wowdps import buffsweep, computedbuilds
from wowdps.profiles import SpecProfile
from wowdps.scenarios import SimSettings
from wowdps.simc_runner import Profileset, ProfilesetResult

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


def test_the_season_boundary_is_three_states_not_a_gain():
    """Keeping last season's four-piece is the incumbent option, so it is one of them.

    The question at a season turn is never "what is the new set worth" on its own.
    It is whether to keep the old 4pc, sit in the split state while the first two
    new pieces arrive, or change over fully -- and the split is the state a player
    passes *through*, which is where the decision actually bites.
    """
    keys = {variant.key: variant.options for variant in buffsweep.crossover_variants("s1", "s2")}

    assert keys["cross__prev4"] == (
        "set_bonus=s1_2pc=1",
        "set_bonus=s1_4pc=1",
        "set_bonus=s2_2pc=0",
        "set_bonus=s2_4pc=0",
    )
    assert keys["cross__split"] == (
        "set_bonus=s1_2pc=1",
        "set_bonus=s1_4pc=0",
        "set_bonus=s2_2pc=1",
        "set_bonus=s2_4pc=0",
    )
    assert keys["cross__cur4"] == (
        "set_bonus=s1_2pc=0",
        "set_bonus=s1_4pc=0",
        "set_bonus=s2_2pc=1",
        "set_bonus=s2_4pc=1",
    )


def test_every_token_is_written_including_the_zeroes():
    """A profile already wearing either set would otherwise poison the variant."""
    for variant in buffsweep.crossover_variants("s1", "s2"):
        assert len(variant.options) == 4
        assert (
            sum(option.endswith("=0") for option in variant.options)
            + sum(option.endswith("=1") for option in variant.options)
            == 4
        )


def test_the_three_comparisons_are_published_as_shares_of_what_is_left_behind():
    result = buffsweep.BuffResult(
        spec_id="a",
        display_name="A",
        wow_class="Mage",
        spec="Fire",
        hero_talent="Sunfury",
        previous_set_name="Old Set",
        prev_four_dps=100_000,
        split_dps=103_000,
        current_four_dps=110_000,
    )

    crossover = result.to_json()["crossover"]

    assert crossover["previousSetName"] == "Old Set"
    assert crossover["splitOverPreviousFour"] == 0.03
    assert crossover["currentFourOverSplit"] == round(110_000 / 103_000 - 1, 5)
    assert crossover["currentFourOverPreviousFour"] == 0.1


def test_a_tier_with_no_predecessor_publishes_no_crossover():
    """A first season has nothing to leave behind, and null says so."""
    result = buffsweep.BuffResult(
        spec_id="a", display_name="A", wow_class="Mage", spec="Fire", hero_talent="Sunfury"
    )
    assert result.to_json()["crossover"] is None


# --------------------------------------------------------------------------------
# The computed build as the sweep's base (owner decision 5, stage 2)
# --------------------------------------------------------------------------------


def _profile(tmp_path: Path) -> SpecProfile:
    path = tmp_path / "MID2_Mage_Arcane.simc"
    path.write_text("mage=X\nspec=arcane\n", encoding="utf-8")
    return SpecProfile(
        path=path,
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent="Spellslinger",
        role="spell",
        talent_hash=None,
    )


def _capture(monkeypatch) -> list[list[Profileset]]:
    """Record the variants of every invocation, answering with nothing measurable.

    The three invocations each catch their own exception, so a stub that returns an
    empty table produces an empty result rather than a failure -- which is enough:
    these tests are about the OPTIONS the sweep sends, not about the numbers.
    """
    seen: list[list[Profileset]] = []

    def fake(simc, request, settings, sets, timeout=0):
        seen.append(list(sets))
        return {}

    monkeypatch.setattr(buffsweep, "run_profilesets", fake)
    return seen


def _base() -> computedbuilds.ComputedBase:
    return computedbuilds.ComputedBase(
        build_id="mage_arcane_spellslinger",
        scenario="patchwerk",
        targets=1,
        talents="COMPUTEDHASH",
        label="a computed build",
        margin=0.0259,
        tie_band=0.0007,
    )


def test_every_buff_variant_carries_the_computed_hash(tmp_path, monkeypatch):
    """What a set bonus and an outside cooldown are worth is a property of the build
    being played, which is the whole of decision 5's second stage. Written per variant
    rather than once on the command line -- see ``computedbuilds.with_talents``."""
    seen = _capture(monkeypatch)
    tier_set = buffsweep.TierSet(
        name="Jade Warlord's Dominion", option="midnight_season_2", tier="MID2", class_id=8
    )
    buffsweep.sweep_spec(Path("simc"), _profile(tmp_path), tier_set, SimSettings(), base=_base())

    assert len(seen) == 2, "the set sweep and Power Infusion, with no previous tier"
    for invocation in seen:
        assert invocation
        for one in invocation:
            assert "talents=COMPUTEDHASH" in one.options, one.key


def test_a_spec_with_no_computed_build_sends_the_options_it_always_did(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    buffsweep.sweep_spec(Path("simc"), _profile(tmp_path), None, SimSettings())
    for invocation in seen:
        for one in invocation:
            assert not [option for option in one.options if option.startswith("talents=")]


def test_the_row_says_which_build_its_numbers_were_measured_on(tmp_path, monkeypatch):
    _capture(monkeypatch)
    result = buffsweep.sweep_spec(
        Path("simc"), _profile(tmp_path), None, SimSettings(), base=_base()
    )
    assert result.to_json()["talentsSource"]["talentHash"] == "COMPUTEDHASH"


def test_a_row_swept_on_the_profiles_own_talents_carries_no_such_field(tmp_path, monkeypatch):
    """Absent is a fact rather than an unknown: every buff row ever published was
    measured on the profile's own talents."""
    _capture(monkeypatch)
    result = buffsweep.sweep_spec(Path("simc"), _profile(tmp_path), None, SimSettings())
    assert "talentsSource" not in result.to_json()
