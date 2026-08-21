"""Profiles simc wrote, commented out, and did not ship.

MID2 ships 15 of 26 damage specs and the reason is not stale action lists -- those
live in the class modules. The generator files carry a complete profile for each
missing spec with every line commented out. These tests pin the parse, and in
particular the blank lines that made the first version return nothing.
"""

from __future__ import annotations

from pathlib import Path

from wowdps import unvalidated

GENERATOR = """ptr=1
# MID2

# warrior="MID2_Warrior_Arms"
# level=90
# spec=arms
# talents=CcEAAAAA

# head=night_enders_tusks,id=249952
# main_hand=alahendal,id=249296


# save=MID2_Warrior_Arms.simc

warrior="MID2_Warrior_Live"
spec=fury
save=MID2_Warrior_Live.simc

# warrior="MID2_Warrior_Protection"
# spec=protection
# save=MID2_Warrior_Protection.simc
"""


def write_generator(tmp_path: Path) -> Path:
    path = tmp_path / "MID2_Generate_Warrior.simc"
    path.write_text(GENERATOR, encoding="utf-8")
    return path


def test_blank_lines_inside_a_block_do_not_end_it():
    """The bug that made the first version return zero profiles from a file with two.

    Generators separate the header, the gear and the save line with blank lines, so
    treating a blank as the end of the block found nothing at all -- and "nothing"
    looks exactly like "simc has no disabled profiles", which is the wrong answer
    stated confidently.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = write_generator(Path(raw))
        found = unvalidated.extract(path)

    assert [entry.name for entry in found] == [
        "MID2_Warrior_Arms",
        "MID2_Warrior_Protection",
    ]
    arms = found[0]
    assert arms.filename == "MID2_Warrior_Arms.simc"
    assert arms.spec_line == "arms"
    # The comment marks are gone and the content survives.
    assert 'warrior="MID2_Warrior_Arms"' in arms.body
    assert "talents=CcEAAAAA" in arms.body
    assert "head=night_enders_tusks,id=249952" in arms.body
    assert "#" not in arms.body


def test_a_live_profile_is_not_collected(tmp_path):
    """Only the disabled ones: a shipped profile is the better claim already."""
    found = unvalidated.extract(write_generator(tmp_path))
    assert all(entry.name != "MID2_Warrior_Live" for entry in found)


def test_writing_never_overwrites_a_profile_simc_ships(tmp_path):
    found = unvalidated.extract(write_generator(tmp_path))
    out = tmp_path / "out"

    written = unvalidated.write_profiles(found, out, shipped={"MID2_Warrior_Arms.simc"})

    assert [path.name for path in written] == ["MID2_Warrior_Protection.simc"]
    assert not (out / "MID2_Warrior_Arms.simc").exists()


def test_a_block_with_no_save_line_is_not_a_profile(tmp_path):
    """`save=` is what names the file; without it there is nothing to write."""
    path = tmp_path / "MID2_Generate_X.simc"
    path.write_text('# mage="MID2_Mage_Nothing"\n# spec=arcane\n', encoding="utf-8")
    assert unvalidated.extract(path) == []


def test_a_written_profile_says_what_it_is(tmp_path):
    """The state travels in the file, so no shard can disagree about it.

    A sharded run materialises these separately in every job. A flag on the command
    line would have to be threaded through all of them and could be passed in one
    and forgotten in another, which publishes an unvalidated build as a shipped one
    -- silently, because the number itself looks fine.
    """
    from wowdps.profiles import parse_profile

    found = unvalidated.extract(write_generator(tmp_path))
    out = tmp_path / "MID2"
    unvalidated.write_profiles(found, out)

    profile = parse_profile(out / "MID2_Warrior_Arms.simc", "MID2")
    assert profile is not None
    assert profile.unvalidated is True
    assert profile.spec == "Arms"
    # The marker is a comment, so simc reads the profile exactly as before.
    assert (out / "MID2_Warrior_Arms.simc").read_text().startswith("# wowdps-unvalidated")


def test_a_shipped_profile_is_not_marked(tmp_path):
    from wowdps.profiles import parse_profile

    path = tmp_path / "MID2_Mage_Arcane.simc"
    path.write_text('mage="MID2_Mage_Arcane"\nspec=arcane\nrole=spell\n', encoding="utf-8")
    profile = parse_profile(path, "MID2")
    assert profile is not None
    assert profile.unvalidated is False


def test_coverage_counts_an_unvalidated_spec_as_neither_shipped_nor_missing(tmp_path):
    """The fourth state, and both of the wrong answers it replaces.

    Counting it as shipped publishes somebody's unfinished profile as this season's
    answer; counting it as missing says the data does not exist while it sits in the
    generator.
    """
    from wowdps.profiles import spec_coverage

    old = tmp_path / "MID1"
    new = tmp_path / "MID2"
    old.mkdir()
    new.mkdir()
    (old / "MID1_Warrior_Fury.simc").write_text(
        'warrior="MID1_Warrior_Fury"\nspec=fury\nrole=attack\n', encoding="utf-8"
    )
    (old / "MID1_Mage_Arcane.simc").write_text(
        'mage="MID1_Mage_Arcane"\nspec=arcane\nrole=spell\n', encoding="utf-8"
    )
    (new / "MID2_Mage_Arcane.simc").write_text(
        'mage="MID2_Mage_Arcane"\nspec=arcane\nrole=spell\n', encoding="utf-8"
    )
    (new / "MID2_Warrior_Fury.simc").write_text(
        f"{unvalidated.MARKER} MID2_Generate_Warrior.simc\n"
        'warrior="MID2_Warrior_Fury"\nspec=fury\nrole=attack\n',
        encoding="utf-8",
    )

    coverage = spec_coverage(tmp_path, "MID2")
    assert coverage["damageSpecs"] == 1
    assert coverage["shipped"] == [{"class": "Mage", "spec": "Arcane"}]
    assert coverage["unvalidated"] == [{"class": "Warrior", "spec": "Fury"}]
    assert coverage["missing"] == []
