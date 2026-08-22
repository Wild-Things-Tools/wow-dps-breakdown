from pathlib import Path

import pytest

from wowdps.profiles import CLASS_TOKENS, parse_profile, slugify

PROFILE_TEMPLATE = """{token}="{name}"
source=default
spec={spec}
level=90
race=dwarf
role={role}
talents=ABCDEF

actions.precombat=arcane_intellect
"""


def write_profile(
    tmp_path: Path, filename: str, token: str, name: str, spec: str, role: str = "spell"
) -> Path:
    path = tmp_path / filename
    path.write_text(
        PROFILE_TEMPLATE.format(token=token, name=name, spec=spec, role=role),
        encoding="utf-8",
    )
    return path


def test_parses_class_spec_and_hero_talent(tmp_path):
    path = write_profile(
        tmp_path, "MID2_Mage_Fire_Frostfire.simc", "mage", "MID2_Mage_Fire_Frostfire", "fire"
    )
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert profile.wow_class == "Mage"
    assert profile.spec == "Fire"
    assert profile.hero_talent == "Frostfire"
    assert profile.id == "mage_fire_frostfire"
    assert profile.is_dps


def test_multiword_class_token_is_unhyphenated_in_body(tmp_path):
    """simc writes `deathknight=` in the body but `Death_Knight` in the name."""
    path = write_profile(
        tmp_path,
        "MID2_Death_Knight_Unholy_San'layn.simc",
        "deathknight",
        "MID2_Death_Knight_Unholy_San'layn",
        "unholy",
        role="attack",
    )
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert profile.wow_class == "Death Knight"
    assert profile.spec == "Unholy"
    assert profile.hero_talent == "San'layn"


def test_internal_name_wins_over_filename(tmp_path):
    """MID1_Death_Knight_Unholy.simc really sims the Rider build; trust the body."""
    path = write_profile(
        tmp_path,
        "MID1_Death_Knight_Unholy.simc",
        "deathknight",
        "MID1_Death_Knight_Unholy_Rider",
        "unholy",
        role="attack",
    )
    profile = parse_profile(path, "MID1")

    assert profile is not None
    assert profile.hero_talent == "Rider of the Apocalypse"


def test_profile_without_hero_suffix_is_default(tmp_path):
    path = write_profile(tmp_path, "MID2_Mage_Fire.simc", "mage", "MID2_Mage_Fire", "fire")
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert profile.hero_talent is None
    assert profile.hero_label == "Default"
    assert profile.id == "mage_fire_default"


def test_multiword_spec_token(tmp_path):
    path = write_profile(
        tmp_path,
        "MID2_Hunter_Beast_Mastery_Pack_Leader.simc",
        "hunter",
        "MID2_Hunter_Beast_Mastery_Pack_Leader",
        "beast_mastery",
        role="attack",
    )
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert profile.spec == "Beast Mastery"
    assert profile.hero_talent == "Pack Leader"
    assert profile.spec_id == "hunter_beast_mastery"


def test_tank_role_is_not_dps(tmp_path):
    path = write_profile(
        tmp_path,
        "MID2_Paladin_Protection.simc",
        "paladin",
        "MID2_Paladin_Protection",
        "protection",
        role="tank",
    )
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert not profile.is_dps


def test_non_player_file_returns_none(tmp_path):
    path = tmp_path / "notes.simc"
    path.write_text("# just a comment\nsome_option=1\n", encoding="utf-8")

    assert parse_profile(path, "MID2") is None


@pytest.mark.parametrize("token", sorted(CLASS_TOKENS))
def test_every_class_token_parses(tmp_path, token):
    display, filename_form = CLASS_TOKENS[token]
    path = write_profile(
        tmp_path,
        f"MID2_{filename_form}_Somespec.simc",
        token,
        f"MID2_{filename_form}_Somespec",
        "somespec",
        role="attack",
    )
    profile = parse_profile(path, "MID2")

    assert profile is not None
    assert profile.wow_class == display


def test_slugify_handles_apostrophes():
    assert slugify("Death Knight", "Unholy", "San'layn") == "death_knight_unholy_san_layn"


def test_spec_coverage_derives_the_reference_list_from_the_other_tiers(tmp_path):
    """ "All specs" is what simc shipped elsewhere, not a table written down here.

    A hard-coded list of the game's damage specs would need editing whenever Blizzard
    adds one -- Midnight adds Devourer -- and would go stale in exactly the patch
    where the question matters most.
    """
    from wowdps.profiles import spec_coverage

    old = tmp_path / "MID1"
    new = tmp_path / "MID2"
    old.mkdir()
    new.mkdir()

    def profile(directory, name, token, spec):
        (directory / f"{name}.simc").write_text(
            f'{token}="{name}"\nspec={spec}\nlevel=80\nrole=attack\n', encoding="utf-8"
        )

    profile(old, "MID1_Mage_Arcane", "mage", "arcane")
    profile(old, "MID1_Warrior_Fury", "warrior", "fury")
    profile(new, "MID2_Mage_Arcane", "mage", "arcane")

    coverage = spec_coverage(tmp_path, "MID2")
    assert coverage["damageSpecs"] == 1
    assert coverage["damageSpecsKnown"] == 2
    assert coverage["missing"] == [{"class": "Warrior", "spec": "Fury"}]
    assert coverage["comparedWith"] == ["MID1"]


def test_a_tier_that_ships_everything_reports_nothing_missing(tmp_path):
    from wowdps.profiles import spec_coverage

    for tier in ("MID1", "MID2"):
        directory = tmp_path / tier
        directory.mkdir()
        (directory / f"{tier}_Mage_Arcane.simc").write_text(
            f'mage="{tier}_Mage_Arcane"\nspec=arcane\nlevel=80\nrole=attack\n', encoding="utf-8"
        )

    coverage = spec_coverage(tmp_path, "MID2")
    assert coverage["missing"] == []
    assert coverage["damageSpecs"] == coverage["damageSpecsKnown"] == 1


def test_coverage_publishes_what_the_tier_ships_so_broken_can_be_told_from_missing(tmp_path):
    """``shipped`` is what makes the third coverage state computable later.

    Without it, "simc ships no profile" and "the profile no longer loads" collapse
    into one number, and an old tier reports as fully covered while its dataset is
    missing whole classes.
    """
    from wowdps.profiles import spec_coverage

    for tier, specs in (("MID1", ("arcane", "fury")), ("MID2", ("arcane",))):
        directory = tmp_path / tier
        directory.mkdir()
        for spec in specs:
            token = "mage" if spec == "arcane" else "warrior"
            (directory / f"{tier}_{spec}.simc").write_text(
                f'{token}="{tier}_{spec}"\nspec={spec}\nlevel=80\nrole=attack\n',
                encoding="utf-8",
            )

    coverage = spec_coverage(tmp_path, "MID1")

    assert coverage["shipped"] == [
        {"class": "Mage", "spec": "Arcane"},
        {"class": "Warrior", "spec": "Fury"},
    ]
    assert len(coverage["shipped"]) == coverage["damageSpecs"]
    # Disjoint by construction: a spec cannot be both shipped for this tier and
    # absent from it.
    shipped = {(e["class"], e["spec"]) for e in coverage["shipped"]}
    absent = {(e["class"], e["spec"]) for e in coverage["missing"]}
    assert not shipped & absent


def test_the_spec_list_covers_every_class_and_names_the_new_spec(tmp_path):
    """Derived rather than typed, which is the whole argument in one line.

    Midnight adds Demon Hunter Devourer (1480). A hand-written table would have to
    be edited for it; reading simc's own `sc_spec_list.inc` picks it up for free,
    and the row-count assertion means a class added to the game fails loudly here
    instead of silently shifting every class by one.
    """
    from wowdps.specindex import _CLASS_ORDER, parse_spec_enum, parse_spec_list

    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        "enum specialization_e {\n  SPEC_NONE = 0,\n  WARRIOR_ARMS = 71,\n", encoding="utf-8"
    )
    rows = "\n".join(
        "  {\n" + "".join(f"    SPEC_{i}_{j},\n" for j in range(3)) + "  },"
        for i in range(len(_CLASS_ORDER))
    )
    (generated / "sc_spec_list.inc").write_text(
        "static constexpr specialization_e __class_spec_id[14][4] =\n{\n" + rows + "\n};\n",
        encoding="utf-8",
    )

    assert parse_spec_enum(tmp_path)["WARRIOR_ARMS"] == 71
    groups = parse_spec_list(tmp_path)
    assert len(groups) == len(_CLASS_ORDER)


def test_a_changed_class_count_is_an_error_not_a_silent_shift(tmp_path):
    from wowdps.specindex import parse_spec_list

    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_spec_list.inc").write_text(
        "static constexpr specialization_e __class_spec_id[2][4] =\n{\n  {\n    A,\n  },\n};\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="class rows"):
        parse_spec_list(tmp_path)


def test_a_hero_tree_is_named_only_when_some_build_plays_it():
    """simc ships no tree names at all -- the SELECTION rows carry the string "0"."""
    from wowdps.specindex import tree_names_from_talents

    names = tree_names_from_talents(
        {
            "builds": [
                {"tree": "251-33", "heroTalent": "Deathbringer"},
                {"tree": "62-27", "heroTalent": "Sunfury"},
                # A build simc ships unnamed contributes nothing rather than
                # naming a tree "Default".
                {"tree": "72-60", "heroTalent": "Default"},
                {"tree": "broken", "heroTalent": "Nope"},
            ]
        }
    )
    assert names == {33: "Deathbringer", 27: "Sunfury"}
