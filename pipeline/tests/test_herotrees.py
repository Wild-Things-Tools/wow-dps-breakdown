"""Every build plays a hero tree, and simc's own data now says which and what it is called.

The decode is exercised in ``test_talenttree``; what is checked here is the join --
profile to sub-tree id to name -- and the three refusals around it.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_talenttree import encode, header_bits
from wowdps.herotrees import load_overrides, resolve_tier, write_overrides
from wowdps.profiles import parse_profile

#: One class-4 hero node and the SELECTION node that names its tree, in the layout
#: ``parse_trait_data`` reads positionally, plus simc's hero tree name table.
TRAIT_FILE = (
    "static constexpr std::array<trait_data_t, 2> __trait_data_data { {\n"
    "  { 3,  4, 117659,  95062, 1,  1, 122671,  439843,      0,      0,  1,  3, 100, "
    '"Unseen Blade", {  260,  261,    0,    0 }, {    0,    0,    0,    0 },  51, 0 },\n'
    "  { 4,  4, 117660,  95063, 1,  1, 122672,  439844,      0,      0,  1,  1, 100, "
    '"0", {  260,  261,    0,    0 }, {    0,    0,    0,    0 },  51, 3 },\n'
    "} };\n"
    "static constexpr std::array<std::tuple<unsigned, const char*, unsigned>, 2> "
    "__trait_sub_tree_data { {\n"
    '  { 51, "Trickster", 4 },\n'
    '  { 52, "Fatebound", 4 },\n'
    "} };\n"
)

#: Both nodes selected and purchased: Outlaw (260), hero tree 51.
TRICKSTER_HASH = encode(header_bits(260) + [1, 1, 0, 0] + [1, 1, 0, 0])


def make_checkout(tmp_path: Path, profiles: dict[str, str]) -> Path:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "trait_data.inc").write_text(TRAIT_FILE, encoding="utf-8")
    tier = tmp_path / "profiles" / "MID2"
    tier.mkdir(parents=True)
    for name, body in profiles.items():
        (tier / f"{name}.simc").write_text(body, encoding="utf-8")
    return tmp_path


def rogue(name: str, talents: str = TRICKSTER_HASH) -> str:
    return f'rogue="{name}"\nspec=outlaw\nrole=attack\ntalents={talents}\n'


def test_a_build_simc_ships_unnamed_is_named_from_its_own_talent_hash(tmp_path):
    """The regression this exists for: simc ships a spec's default build with no hero
    suffix, and it used to surface as a build with no hero tree, which cannot exist.

    Nothing here runs simc. The tree is the one the loadout's SELECTION node names,
    and the name is the one simc's hero tree table gives that id.
    """
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw": rogue("MID2_Rogue_Outlaw")})
    result = resolve_tier(root / "profiles", "MID2", root)
    assert result.resolved == {"MID2_Rogue_Outlaw": "Trickster"}
    assert result.unresolved == []
    assert result.renamed == []


def test_the_table_names_the_tree_even_where_the_profile_name_abbreviates_it(tmp_path):
    """MID2 spells Scalecommander ``SC`` and Soul Harvester ``Soulharvester``. Those
    are file-naming, not the tree's name, and the disagreement is reported."""
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw_Trick": rogue("MID2_Rogue_Outlaw_Trick")})
    result = resolve_tier(root / "profiles", "MID2", root)
    assert result.resolved == {"MID2_Rogue_Outlaw_Trick": "Trickster"}
    assert result.renamed == [("MID2_Rogue_Outlaw_Trick", "Trick", "Trickster")]


def test_a_hash_that_does_not_decode_is_left_unresolved_rather_than_guessed(tmp_path):
    """simc's disabled Havoc profiles are two of these today. The build keeps
    whatever its own profile name said; nothing is invented."""
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw": rogue("MID2_Rogue_Outlaw", talents="!!!")})
    result = resolve_tier(root / "profiles", "MID2", root)
    assert result.resolved == {}
    assert [name for name, _ in result.unresolved] == ["MID2_Rogue_Outlaw"]


def test_a_checkout_with_no_hero_tree_table_resolves_nothing(tmp_path):
    """Reading the table is the whole method, so its absence has to be loud rather
    than a silently empty result that looks like a tier with no default builds."""
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw": rogue("MID2_Rogue_Outlaw")})
    (root / "engine" / "dbc" / "generated" / "trait_data.inc").write_text(
        TRAIT_FILE.split("__trait_sub_tree_data")[0], encoding="utf-8"
    )
    assert resolve_tier(root / "profiles", "MID2", root).resolved == {}


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


def test_the_resolved_name_wins_over_the_one_the_filename_carried(tmp_path):
    """``evoker_devastation_sc`` keeps its id -- the joins depend on it -- while the
    build is drawn as Scalecommander, which is what simc's table calls that tree."""
    path = tmp_path / "MID2_Evoker_Devastation_SC.simc"
    path.write_text(
        'evoker="MID2_Evoker_Devastation_SC"\nspec=devastation\nrole=spell\ntalents=ABC\n',
        encoding="utf-8",
    )
    profile = parse_profile(path, "MID2", {"MID2_Evoker_Devastation_SC": "Scalecommander"})
    assert profile is not None
    assert profile.id == "evoker_devastation_sc"
    assert profile.hero_talent == "Scalecommander"


def test_overrides_round_trip_per_tier(tmp_path):
    path = tmp_path / "hero_trees.json"
    write_overrides("MID2", {"MID2_Rogue_Subtlety": "Deathstalker"}, path)
    write_overrides("MID1", {"MID1_Rogue_Subtlety": "Trickster"}, path)

    assert load_overrides("MID2", path) == {"MID2_Rogue_Subtlety": "Deathstalker"}
    assert load_overrides("MID1", path) == {"MID1_Rogue_Subtlety": "Trickster"}
    # A tier that was never processed resolves to nothing rather than an error.
    assert load_overrides("MID9", path) == {}
    assert load_overrides("MID2", tmp_path / "absent.json") == {}


def test_a_rerun_that_changes_nothing_leaves_the_file_alone(tmp_path):
    """Same rule as the datasets: a quiet night must leave nothing to commit."""
    path = tmp_path / "hero_trees.json"
    write_overrides("MID2", {"MID2_Rogue_Outlaw": "Trickster"}, path)
    before = path.stat().st_mtime_ns
    write_overrides("MID2", {"MID2_Rogue_Outlaw": "Trickster"}, path)
    assert path.stat().st_mtime_ns == before


def test_the_shipped_map_names_every_mid2_build_that_used_to_read_default():
    """The three builds the site drew as "Default" on 2026-08-22. They are simc's
    unnamed default builds for specs the old ability-signature detector had no table
    for, which is why nothing could resolve them."""
    data = json.loads(
        (Path(__file__).parent.parent / "src/wowdps/data/hero_trees.json").read_text()
    )
    resolved = data["tiers"]["MID2"]["resolved"]
    assert resolved["MID2_Druid_Balance"] == "Elune's Chosen"
    assert resolved["MID2_Monk_Windwalker"] == "Shado-Pan"
    assert resolved["MID2_Rogue_Outlaw"] == "Trickster"
    # And the five the previous detector had resolved, unchanged by the new method.
    assert resolved["MID2_Death_Knight_Frost"] == "Deathbringer"
    assert resolved["MID2_Hunter_Beast_Mastery"] == "Pack Leader"
    assert resolved["MID2_Hunter_Marksmanship"] == "Sentinel"
    assert resolved["MID2_Hunter_Survival"] == "Sentinel"
    assert resolved["MID2_Rogue_Subtlety"] == "Deathstalker"


# --------------------------------------------------------------------------------
# The command's exit code, which decides whether a nightly survives
# --------------------------------------------------------------------------------


def run_cli(*argv: str) -> int:
    from wowdps.cli import main

    return main(["hero-trees", *argv])


def test_resolving_nothing_is_reported_and_does_not_fail_the_run(tmp_path):
    """This runs inside a `for tier in ...; done` loop in the publish job under
    `bash -e`, so a non-zero exit aborts that job **before the commit step** and
    discards a whole night's simulations -- over a data file whose absence costs only
    the canonical name, since every build keeps whatever its own profile said."""
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw": rogue("MID2_Rogue_Outlaw")})
    (root / "engine" / "dbc" / "generated" / "trait_data.inc").write_text(
        "// an older checkout\n", encoding="utf-8"
    )
    assert run_cli("--tier", "MID2", "--simc-source", str(root), "--no-ptr") == 0
    # `--strict` is the gate for anyone who wants one.
    assert run_cli("--tier", "MID2", "--simc-source", str(root), "--no-ptr", "--strict") == 1


def test_a_tier_simc_no_longer_ships_is_skipped_rather_than_raising(tmp_path):
    """`tiers.json` outlives simc's profile directories: the publish job loops over
    *published* tiers and simc deletes an old tier's profiles eventually."""
    root = make_checkout(tmp_path, {"MID2_Rogue_Outlaw": rogue("MID2_Rogue_Outlaw")})
    assert run_cli("--tier", "TWW3", "--simc-source", str(root), "--no-ptr") == 0
