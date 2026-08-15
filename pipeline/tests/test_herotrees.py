"""Every spec plays a hero tree, including the ones simc ships unnamed."""

from __future__ import annotations

import json
from pathlib import Path

from wowdps.herotrees import (
    active_ability_slugs,
    detect_hero_tree,
    load_overrides,
    write_overrides,
)
from wowdps.profiles import parse_profile


def report_with(*ability_slugs):
    return {
        "sim": {
            "players": [
                {
                    "buffs": [{"name": slug} for slug in ability_slugs],
                    "stats": [{"name": "auto_attack", "actual_amount": {"mean": 100.0}}],
                }
            ]
        }
    }


def test_the_active_abilities_pick_the_tree_apart():
    frost = report_with("killing_machine", "exterminate", "reapers_mark")
    assert detect_hero_tree(frost, "Death Knight", "Frost") == "Deathbringer"

    mm = report_with("sentinels_mark", "precise_shots")
    assert detect_hero_tree(mm, "Hunter", "Marksmanship") == "Sentinel"


def test_an_ability_that_never_fired_is_not_a_signature():
    """Both trees' actions sit in the APL; only the taken one produces damage or a
    buff. A stat with no damage must not count, or every build looks like both."""
    player = {
        "buffs": [{"name": "Heart of the Pack"}],
        "stats": [
            {"name": "black_arrow", "actual_amount": {"mean": 0.0}},  # in APL, never cast
            {"name": "vicious_hunt", "actual_amount": {"mean": 500.0}},
        ],
    }
    report = {"sim": {"players": [player]}}
    assert "black_arrow" not in active_ability_slugs(report)
    assert detect_hero_tree(report, "Hunter", "Beast Mastery") == "Pack Leader"


def test_an_ambiguous_or_unknown_result_is_not_guessed():
    # Signatures for both trees present -> the table has gone stale, so refuse.
    both = report_with("exterminate", "apocalypse_now")
    assert detect_hero_tree(both, "Death Knight", "Frost") is None
    # A spec with no signature table -> None, and the caller keeps it unnamed.
    assert detect_hero_tree(report_with("anything"), "Mage", "Arcane") is None
    # Nothing matched.
    assert detect_hero_tree(report_with("frostbolt"), "Rogue", "Subtlety") is None


def test_the_resolved_tree_feeds_the_display_and_never_the_id(tmp_path):
    """The id names simc's build slot; the hero tree names what it plays. Resolving
    one must not rename the file every other dataset joins on."""
    profile_text = 'deathknight="MID2_Death_Knight_Frost"\nspec=frost\nrole=attack\ntalents=ABC\n'
    path = tmp_path / "MID2_Death_Knight_Frost.simc"
    path.write_text(profile_text, encoding="utf-8")

    without = parse_profile(path, "MID2")
    assert without is not None
    assert without.id == "death_knight_frost_default"
    assert without.hero_label == "Default"  # not yet resolved

    with_override = parse_profile(path, "MID2", {"MID2_Death_Knight_Frost": "Deathbringer"})
    assert with_override is not None
    assert with_override.id == "death_knight_frost_default"  # UNCHANGED
    assert with_override.hero_talent == "Deathbringer"
    assert with_override.display_name == "Frost Death Knight (Deathbringer)"


def test_overrides_round_trip_per_tier(tmp_path):
    path = tmp_path / "hero_trees.json"
    write_overrides("MID2", {"MID2_Rogue_Subtlety": "Deathstalker"}, path)
    write_overrides("MID1", {"MID1_Rogue_Subtlety": "Trickster"}, path)

    assert load_overrides("MID2", path) == {"MID2_Rogue_Subtlety": "Deathstalker"}
    assert load_overrides("MID1", path) == {"MID1_Rogue_Subtlety": "Trickster"}
    # A tier that was never processed resolves to nothing rather than an error.
    assert load_overrides("MID9", path) == {}
    assert load_overrides("MID2", tmp_path / "absent.json") == {}


def test_the_shipped_mid2_map_covers_every_default_build():
    """The five builds simc ships unnamed in MID2 are all resolved, so the site
    never shows a build without a hero tree."""
    data = json.loads(
        (Path(__file__).parent.parent / "src/wowdps/data/hero_trees.json").read_text()
    )
    resolved = data["tiers"]["MID2"]["resolved"]
    assert resolved == {
        "MID2_Death_Knight_Frost": "Deathbringer",
        "MID2_Hunter_Beast_Mastery": "Pack Leader",
        "MID2_Hunter_Marksmanship": "Sentinel",
        "MID2_Hunter_Survival": "Sentinel",
        "MID2_Rogue_Subtlety": "Deathstalker",
    }
