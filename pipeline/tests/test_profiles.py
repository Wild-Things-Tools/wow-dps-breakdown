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
