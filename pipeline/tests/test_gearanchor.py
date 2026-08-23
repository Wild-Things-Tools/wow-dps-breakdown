"""Normalized gear for computed builds.

Every test here pins a decision that a measurement paid for. The measurements
themselves are in the module docstring and in CLAUDE.md; what is pinned is that the
code still does the thing the measurement justified.
"""

from __future__ import annotations

import pytest

from wowdps import gearanchor
from wowdps.buffsweep import TierSet
from wowdps.profiles import SpecProfile

# Two set ids: 2060 is "this tier's Mage set", 1900 last season's. Field 24 of
# ``dbc_item_data_t`` counting the name out separately -- see ``parse_item_sets``.
ITEM_DATA_INC = """\
static const std::array<dbc_item_data_t, 4> __item_data_chunk0 { {
  { "Tier Helm", 500001, 0x00080000, 0x00002000, 0x00, 219, 90, 0, 0, 4, 1, 4, 1, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[0], 1, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 2060, 0, 0, 0 },
  { "Tier Robe", 500002, 0x00080000, 0x00002000, 0x00, 219, 90, 0, 0, 4, 5, 4, 1, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[0], 1, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 2060, 0, 0, 0 },
  { "Old Tier Helm", 400001, 0x00080000, 0x00002000, 0x00, 200, 90, 0, 0, 4, 1, 4, 1, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[0], 1, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 1900, 0, 0, 0 },
  { "Plain Ring", 600001, 0x00080000, 0x00002000, 0x00, 219, 90, 0, 0, 4, 11, 4, 0, 1, 0,\
 0.000000f, 0.000000f, &__item_stats_data[0], 1, 0xffff, 0xffffffffffffffff, { 0, 0, 0 },\
 0, 0, 0, 0, 0, 0 },
} };
"""

MID2_SETS = [
    TierSet(name="Primal Attire", option="midnight_season_2", tier="MID2", class_id=8, set_id=2060),
    TierSet(name="Old Attire", option="midnight_season_1", tier="MID1", class_id=8, set_id=1900),
]


def write_items(tmp_path):
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "item_data.inc").write_text(ITEM_DATA_INC, encoding="utf-8")
    return tmp_path


def profile(tmp_path, name, body, *, item_level=None, unvalidated=False):
    path = tmp_path / f"{name}.simc"
    path.write_text(body, encoding="utf-8")
    return SpecProfile(
        path=path,
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent="Spellslinger",
        role="spell",
        talent_hash=None,
        item_level=item_level,
        unvalidated=unvalidated,
    )


FOUR_PIECE = (
    'mage="A"\nspec=arcane\n'
    "head=tier_helm,id=500001,bonus_id=1/2,ilevel=344,gem_id=1,enchant_id=2\n"
    "chest=tier_robe,id=500002,bonus_id=1,ilevel=344\n"
    "hands=tier_helm,id=500001,ilevel=344\n"
    "legs=tier_robe,id=500002,ilevel=334\n"
    "finger1=plain_ring,id=600001,ilevel=334,gem_id=9,enchant_id=7967\n"
)

NO_PIECE = (
    'mage="B"\nspec=arcane\n'
    "head=plain_ring,id=600001,bonus_id=1/2,ilevel=344\n"
    "finger1=plain_ring,id=600001,ilevel=334,gem_id=9,enchant_id=7967\n"
)


# --------------------------------------------------------------------------------
# Reading and re-emitting a kit
# --------------------------------------------------------------------------------


def test_an_untouched_kit_renders_back_byte_identically():
    """A "normalized" kit that is silently a *different* kit is the whole failure mode.

    Measured beside this: re-emitting a shipped profile's sixteen gear lines as
    profileset options returned DPS bit-identical to an inert profileset on the same
    run, so the round trip is exact where it matters as well as in the string.
    """
    source = (
        "head=venomkeepers_horrific_cowl,id=271874,bonus_id=13335/13668,"
        "ilevel=344,gem_id=240967,enchant_id=8017"
    )
    assert gearanchor.parse_gear_lines(source)[0].render() == source


def test_a_line_already_at_the_target_is_rewritten_to_itself():
    line = gearanchor.parse_gear_lines("trinket1=,id=250215,bonus_id=12854,ilevel=334")[0]
    assert line.with_ilevel(334).render() == line.render()


def test_the_item_level_is_written_onto_a_line_that_states_none():
    """simc's disabled profiles state no ``ilevel=`` at all and still wear 289.

    Read back out of simc's own report on 2026-08-23: MID2's Windwalker Monk profile
    states none on any of its fifteen gear lines, simc resolves them to 285-289 from
    the bonus ids, and the anchor moves every one of them to 334. A normalizer that
    only *replaced* an existing value would leave that profile exactly where it was.
    """
    source = "head=night_enders_tusks,id=249952,bonus_id=12806/13335,gem_id=240983"
    line = gearanchor.parse_gear_lines(source)[0]

    assert line.ilevel is None
    moved = line.with_ilevel(334)
    assert moved.ilevel == 334
    # After the bonus ids, where simc's own generator writes it.
    assert moved.render() == (
        "head=night_enders_tusks,id=249952,bonus_id=12806/13335,ilevel=334,gem_id=240983"
    )


def test_gems_enchants_and_crafted_stats_survive_the_normalization():
    """The enchant is worth twelve times the ten-item-level step it sits beside.

    Measured on Arcane Mage, MID2, one target, 1000 deterministic iterations, one
    ring: enchant +1.09%, gem +0.44%, the whole 334->344 step +0.09%. Dropping either
    while moving the item level would measure the wrong thing by an order of magnitude.
    """
    source = (
        "wrists=martyrs_bindings,id=239648,bonus_id=8960,ilevel=331,"
        "gem_id=240900,crafted_stats=32/40"
    )
    moved = gearanchor.parse_gear_lines(source)[0].with_ilevel(334)

    assert moved.render() == (
        "wrists=martyrs_bindings,id=239648,bonus_id=8960,ilevel=334,gem_id=240900,crafted_stats=32/40"
    )


def test_only_live_gear_lines_are_read():
    """A profile's ``# Gear Summary`` and simc's generators both hide gear behind ``#``.

    Reading commented lines would build a kit nobody wears -- and the generator files
    are exactly where the *disabled* profiles live, so this is not hypothetical.
    """
    text = (
        "head=real,id=1,ilevel=334\n"
        "# head=commented,id=2,ilevel=289\n"
        "actions.precombat+=/snapshot_stats\n"
        "# gear_ilvl=337.38\n"
    )
    assert [line.name for line in gearanchor.parse_gear_lines(text)] == ["real"]


# --------------------------------------------------------------------------------
# Deriving the target
# --------------------------------------------------------------------------------


def test_the_item_level_target_is_the_floor_of_the_band(tmp_path):
    """Not the mode, and not the ceiling. Both were rejected by measurement.

    The mode of the item levels MID2's shipped gear lines state is 133 at 344 against
    131 at 334 -- a two-line margin. And the ceiling flatters every computed build
    against a shipped field whose own average item levels run 336.75-339.25.
    """
    profiles = [
        profile(tmp_path, "a", FOUR_PIECE, item_level=344),
        profile(tmp_path, "b", FOUR_PIECE, item_level=344),
        profile(tmp_path, "c", FOUR_PIECE, item_level=334),
    ]
    target = gearanchor.derive_target(profiles, "MID2")

    assert target.band == (334, 344)
    assert target.ilevel == 334


def test_a_tier_stating_no_item_level_anywhere_is_a_refusal(tmp_path):
    """Inventing one would put every computed build at a level nobody chose."""
    profiles = [profile(tmp_path, "a", FOUR_PIECE, item_level=None)]

    with pytest.raises(gearanchor.AnchorError, match="no band to anchor inside"):
        gearanchor.derive_target(profiles, "MID2")


def test_a_set_token_without_the_item_table_is_a_refusal(tmp_path):
    """The token says which set exists, not which state is comparable.

    Those two answers are 13.13% apart on a real MID2 build, so defaulting is the one
    hard-coded number this module exists to avoid.
    """
    profiles = [profile(tmp_path, "a", FOUR_PIECE, item_level=334)]

    with pytest.raises(gearanchor.AnchorError, match="item_sets="):
        gearanchor.derive_target(profiles, "MID2", MID2_SETS)


# --------------------------------------------------------------------------------
# The set state
# --------------------------------------------------------------------------------


def test_set_pieces_are_counted_the_way_simc_counts_them(tmp_path):
    """``dbc_item_data_t::id_set`` against ``item_set_bonus_t::set_id``.

    Which is ``set_bonus_t::initialize``'s own rule. Validated against simc's
    behaviour on 13 of 13 MID2 profiles: every profile this count puts at four or
    more pieces returned DPS **bit-identical** under a forced four-piece, and every
    profile it puts at zero moved.
    """
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    set_ids = gearanchor.set_ids_for(MID2_SETS, "MID2", "Mage")

    assert item_sets[500001] == 2060
    assert 600001 not in item_sets  # id_set 0: simc skips it outright
    four = gearanchor.parse_gear_lines(FOUR_PIECE)
    none = gearanchor.parse_gear_lines(NO_PIECE)
    assert gearanchor.count_set_pieces(four, set_ids, item_sets) == 4
    assert gearanchor.count_set_pieces(none, set_ids, item_sets) == 0


def test_the_set_state_comes_from_the_tier_not_from_the_kit(tmp_path):
    """MID2's four Mage builds wear no set where the other twenty-four wear four.

    Inheriting per kit would hand those two specs a kit measured at **13.13%** behind
    the field. The anchor states what the tier wears, so a kit with none still gets
    the four-piece written on.
    """
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    profiles = [
        profile(tmp_path, "a", FOUR_PIECE, item_level=334),
        profile(tmp_path, "b", FOUR_PIECE, item_level=344),
        profile(tmp_path, "c", NO_PIECE, item_level=344),
    ]
    target = gearanchor.derive_target(profiles, "MID2", MID2_SETS, item_sets, wow_class="Mage")

    assert target.set_pieces == gearanchor.SET_FOUR
    assert dict(target.set_tally) == {gearanchor.SET_NONE: 1, gearanchor.SET_FOUR: 2}

    bare = gearanchor.apply(target, gearanchor.parse_gear_lines(NO_PIECE))
    assert "set_bonus=midnight_season_2_2pc=1" in bare.options()
    assert "set_bonus=midnight_season_2_4pc=1" in bare.options()


def test_a_tier_whose_profiles_wear_no_set_anchors_without_one(tmp_path):
    """The inverse, and the reason the four-piece is not simply hard-coded.

    A tier whose shipped profiles do not wear their set would otherwise put every
    computed build ahead of the whole ranking it is drawn beside.
    """
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    profiles = [
        profile(tmp_path, "a", NO_PIECE, item_level=334),
        profile(tmp_path, "b", NO_PIECE, item_level=344),
    ]
    target = gearanchor.derive_target(profiles, "MID2", MID2_SETS, item_sets, wow_class="Mage")

    assert target.set_pieces == gearanchor.SET_NONE
    assert "set_bonus=midnight_season_2_2pc=0" in target.set_options()
    assert "set_bonus=midnight_season_2_4pc=0" in target.set_options()


def test_a_split_tier_takes_the_lower_state(tmp_path):
    """No comparable answer exists, so take the direction that cannot flatter."""
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    profiles = [
        profile(tmp_path, "a", FOUR_PIECE, item_level=334),
        profile(tmp_path, "b", NO_PIECE, item_level=344),
    ]
    target = gearanchor.derive_target(profiles, "MID2", MID2_SETS, item_sets, wow_class="Mage")

    assert target.set_pieces == gearanchor.SET_NONE


def test_a_disabled_profile_does_not_vote_on_the_set_state(tmp_path):
    """Same exclusion the item-level band makes, for the same reason.

    A profile simc did not ship must not get to say what shipped gear looks like, or
    it drags the anchor toward its own gap and quietly excuses it.
    """
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    profiles = [
        profile(tmp_path, "a", FOUR_PIECE, item_level=334),
        profile(tmp_path, "u1", NO_PIECE, item_level=289, unvalidated=True),
        profile(tmp_path, "u2", NO_PIECE, item_level=289, unvalidated=True),
    ]
    target = gearanchor.derive_target(profiles, "MID2", MID2_SETS, item_sets, wow_class="Mage")

    assert target.set_pieces == gearanchor.SET_FOUR
    assert dict(target.set_tally) == {gearanchor.SET_FOUR: 1}


def test_last_seasons_set_is_written_to_zero(tmp_path):
    """A harvested character may still be wearing it; inheriting that is not a build."""
    item_sets = gearanchor.parse_item_sets(write_items(tmp_path))
    profiles = [profile(tmp_path, "a", FOUR_PIECE, item_level=334)]
    target = gearanchor.derive_target(profiles, "MID2", MID2_SETS, item_sets, wow_class="Mage")

    assert target.zeroed_options == ("midnight_season_1",)
    assert "set_bonus=midnight_season_1_2pc=0" in target.set_options()
    assert "set_bonus=midnight_season_1_4pc=0" in target.set_options()


# --------------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------------


def test_every_slot_ends_at_one_item_level(tmp_path):
    """The within-a-search standard is byte-identical gear, not roughly equal gear."""
    profiles = [profile(tmp_path, "a", FOUR_PIECE, item_level=334)]
    target = gearanchor.derive_target(profiles, "MID2")
    anchor = gearanchor.apply(target, gearanchor.parse_gear_lines(FOUR_PIECE))

    assert {line.ilevel for line in anchor.lines} == {334}
    assert gearanchor.apply(target, gearanchor.parse_gear_lines(FOUR_PIECE)).options() == (
        anchor.options()
    )


def test_the_description_says_what_moved_and_what_did_not(tmp_path):
    """Published beside the build, so a reader can see the anchor rather than trust it."""
    profiles = [profile(tmp_path, "a", FOUR_PIECE, item_level=334)]
    target = gearanchor.derive_target(profiles, "MID2")
    anchor = gearanchor.apply(target, gearanchor.parse_gear_lines(FOUR_PIECE))
    described = anchor.to_json()

    assert described["itemLevel"] == 334
    assert [c["slot"] for c in described["slotsNormalized"]] == ["head", "chest", "hands"]
    assert described["slotsAlreadyAtTarget"] == ["legs", "finger1"]
    assert described["preserved"]["enchants"] == ["head", "finger1"]
    assert "334" in gearanchor.describe(anchor)
