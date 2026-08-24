"""The builds this project supplies for missing (spec, hero tree) cells.

What is pinned here is the honesty machinery, not the numbers: the origin travels
in the written file and back out of ``parse_profile`` into the summary row; a
second-tree cell wears its base's gear byte for byte; a hash the trait table
refuses is never written; and a shipped profile is never overwritten. The hashes
themselves are validated against the real trait table by the committed-data test
at the bottom, which needs a checkout and is env-gated like its siblings.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_herotrees import TRAIT_FILE, TRICKSTER_HASH
from test_talenttree import encode, header_bits
from wowdps import extrabuilds, unvalidated
from wowdps.dataset import SpecResult
from wowdps.extrabuilds import ExtraBuild
from wowdps.profiles import parse_profile

SHIPPED_BODY = (
    'rogue="MID2_Rogue_Outlaw"\n'
    "spec=outlaw\n"
    "role=attack\n"
    f"talents={TRICKSTER_HASH}\n"
    "head=cap,id=1234,ilevel=334\n"
    "finger1=ring,id=5678,ilevel=344,gem_id=240906,enchant_id=7967\n"
)

GENERATOR_BODY = (
    "# MID2\n"
    "\n"
    '# rogue="MID2_Rogue_Subtlety"\n'
    "# spec=subtlety\n"
    "# role=attack\n"
    f"# talents={TRICKSTER_HASH}\n"
    "\n"
    "# head=old_cap,id=999,ilevel=289\n"
    "\n"
    "# save=MID2_Rogue_Subtlety.simc\n"
)


def make_checkout(tmp_path: Path) -> Path:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "trait_data.inc").write_text(TRAIT_FILE, encoding="utf-8")
    tier = tmp_path / "profiles" / "MID2"
    tier.mkdir(parents=True)
    (tier / "MID2_Rogue_Outlaw.simc").write_text(SHIPPED_BODY, encoding="utf-8")
    generators = tmp_path / "profiles" / "generators" / "MID2"
    generators.mkdir(parents=True)
    (generators / "MID2_Generate_Rogue.simc").write_text(GENERATOR_BODY, encoding="utf-8")
    return tmp_path


def cell(**overrides) -> ExtraBuild:
    base = dict(
        profile="MID2_Rogue_Outlaw_Trickster",
        base="MID2_Rogue_Outlaw",
        hero_tree="Trickster",
        origin="harvested",
        talents=TRICKSTER_HASH,
        spec_id=260,
        note="Talents from a real player's ranked kill; the character is the base's.",
    )
    base.update(overrides)
    return ExtraBuild(**base)


# --------------------------------------------------------------------------------
# The origin travels in the file and comes back out everywhere a row is built
# --------------------------------------------------------------------------------


def test_the_origin_marker_round_trips_into_the_parsed_profile(tmp_path):
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    report = extrabuilds.write_cells(simc, "MID2", out, [cell()])
    assert [p.name for p in report.written] == ["MID2_Rogue_Outlaw_Trickster.simc"]

    profile = parse_profile(out / "MID2_Rogue_Outlaw_Trickster.simc", "MID2")
    assert profile.origin == "harvested"
    assert profile.origin_note.startswith("Talents from a real player's")
    # A shipped base is not simc's disabled generator profile, so the cell built
    # from it must not claim that state.
    assert profile.unvalidated is False
    assert profile.id == "rogue_outlaw_trickster"
    assert profile.hero_talent == "Trickster"


def test_the_origin_reaches_the_summary_row_and_only_when_set(tmp_path):
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    extrabuilds.write_cells(simc, "MID2", out, [cell()])
    materialised = parse_profile(out / "MID2_Rogue_Outlaw_Trickster.simc", "MID2")
    shipped = parse_profile(simc / "profiles" / "MID2" / "MID2_Rogue_Outlaw.simc", "MID2")

    assert SpecResult(profile=materialised).summary()["origin"] == "harvested"
    assert SpecResult(profile=materialised).to_json()["origin"] == "harvested"
    # Emitted only when set: a tier of simc's own profiles produces the bytes it
    # did before this existed.
    assert "origin" not in SpecResult(profile=shipped).summary()
    assert "origin" not in SpecResult(profile=shipped).to_json()


def test_a_cell_on_a_disabled_base_keeps_the_unvalidated_claim(tmp_path):
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    report = extrabuilds.write_cells(
        simc,
        "MID2",
        out,
        [cell(profile="MID2_Rogue_Subtlety_Trickster", base="MID2_Rogue_Subtlety")],
    )
    assert len(report.written) == 1
    text = report.written[0].read_text(encoding="utf-8")
    # First line, because ``parse_profile`` tests `startswith` for the flag.
    assert text.startswith(unvalidated.MARKER)
    profile = parse_profile(report.written[0], "MID2")
    assert profile.unvalidated is True
    assert profile.origin == "harvested"


# --------------------------------------------------------------------------------
# A second-tree cell wears its base's character byte for byte
# --------------------------------------------------------------------------------


def test_a_cell_changes_only_the_player_name_and_the_talents(tmp_path):
    """The whole comparability claim: the materialised body is the sibling's, so
    the two builds differ in ``talents=`` the way simc's own two-build specs do."""
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    other_hash = encode(header_bits(260) + [1, 0] + [1, 1, 0, 0])
    extrabuilds.write_cells(simc, "MID2", out, [cell(talents=other_hash)])

    written = (out / "MID2_Rogue_Outlaw_Trickster.simc").read_text(encoding="utf-8")
    body = [line for line in written.splitlines() if not line.startswith("# wowdps-")]
    base = SHIPPED_BODY.splitlines()
    assert len(body) == len(base)
    changed = [(a, b) for a, b in zip(base, body, strict=True) if a != b]
    assert changed == [
        ('rogue="MID2_Rogue_Outlaw"', 'rogue="MID2_Rogue_Outlaw_Trickster"'),
        (f"talents={TRICKSTER_HASH}", f"talents={other_hash}"),
    ]


# --------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------


def test_a_hash_stating_another_spec_is_refused(tmp_path):
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    foreign = encode(header_bits(261) + [1, 1, 0, 0] + [1, 1, 0, 0])
    report = extrabuilds.write_cells(simc, "MID2", out, [cell(talents=foreign)])
    assert report.written == []
    assert len(report.skipped) == 1
    assert "states spec 261" in report.skipped[0][1]
    assert not (out / "MID2_Rogue_Outlaw_Trickster.simc").exists()


def test_a_hash_that_does_not_decode_is_refused(tmp_path):
    simc = make_checkout(tmp_path)
    report = extrabuilds.write_cells(simc, "MID2", tmp_path / "out", [cell(talents="!!!")])
    assert report.written == []
    assert "does not decode" in report.skipped[0][1]


def test_a_hash_on_the_wrong_hero_tree_is_refused(tmp_path):
    """A row labelled with a tree it does not play is the quiet failure this check
    exists for -- the number would look fine and describe the wrong build."""
    simc = make_checkout(tmp_path)
    report = extrabuilds.write_cells(simc, "MID2", tmp_path / "out", [cell(hero_tree="Fatebound")])
    assert report.written == []
    assert "plays 'Trickster'" in report.skipped[0][1]


def test_a_shipped_profile_is_never_overwritten(tmp_path):
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "MID2_Rogue_Outlaw_Trickster.simc").write_text(
        'rogue="Somebody_Simc_Ships"\n', encoding="utf-8"
    )
    report = extrabuilds.write_cells(simc, "MID2", out, [cell()])
    assert report.written == []
    assert "shipped profile" in report.skipped[0][1]
    assert (out / "MID2_Rogue_Outlaw_Trickster.simc").read_text(
        encoding="utf-8"
    ) == 'rogue="Somebody_Simc_Ships"\n'


def test_a_previously_materialised_file_is_superseded(tmp_path):
    """The unvalidated step writes the refused generator profile first; a cell of
    the same filename replaces it, or the tier directory holds two profiles with
    one build id -- one refused, one working."""
    simc = make_checkout(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    stale = f'{unvalidated.MARKER} MID2_Generate_Rogue.simc\nrogue="Old_Refused"\n'
    (out / "MID2_Rogue_Outlaw_Trickster.simc").write_text(stale, encoding="utf-8")
    report = extrabuilds.write_cells(simc, "MID2", out, [cell()])
    assert len(report.written) == 1
    assert "Old_Refused" not in report.written[0].read_text(encoding="utf-8")


def test_an_unknown_base_is_reported_not_invented(tmp_path):
    simc = make_checkout(tmp_path)
    report = extrabuilds.write_cells(
        simc, "MID2", tmp_path / "out", [cell(base="MID2_Rogue_Nobody")]
    )
    assert report.written == []
    assert "MID2_Rogue_Nobody" in report.skipped[0][1]


def test_a_missing_trait_table_writes_unchecked_rather_than_dropping_the_night(tmp_path):
    simc = make_checkout(tmp_path)
    (simc / "engine" / "dbc" / "generated" / "trait_data.inc").unlink()
    report = extrabuilds.write_cells(simc, "MID2", tmp_path / "out", [cell()])
    assert report.unchecked is True
    assert len(report.written) == 1


# --------------------------------------------------------------------------------
# The committed data file
# --------------------------------------------------------------------------------


def test_the_committed_cells_load_and_are_distinct():
    cells = extrabuilds.load_cells("MID2")
    assert len(cells) == 16
    assert len({c.profile for c in cells}) == 16
    assert len({c.filename for c in cells}) == 16
    for c in cells:
        assert c.origin in extrabuilds.ORIGINS
        assert c.talents
        assert c.note
    # The owner's requirement, counted rather than eyeballed: with these cells the
    # tier's build list covers every one of the four absent damage specs twice and
    # gives every single-tree spec its second tree. The (spec key, tree) pairs are
    # pinned so a hand edit that quietly drops a cell fails here rather than on
    # the published site.
    pairs = {(c.profile, c.hero_tree) for c in cells}
    assert ("MID2_Paladin_Retribution_Templar", "Templar") in pairs
    assert ("MID2_Hunter_Beast_Mastery_Dark_Ranger", "Dark Ranger") in pairs
    assert ("MID2_Warlock_Demonology_Diabolist", "Diabolist") in pairs


def test_an_unknown_origin_in_the_data_file_is_refused(tmp_path):
    bad = tmp_path / "extra.json"
    bad.write_text(
        json.dumps(
            {
                "tiers": {
                    "MID2": [
                        {
                            "profile": "X",
                            "base": "Y",
                            "heroTree": "Z",
                            "origin": "guessed",
                            "talents": "A",
                            "specId": 1,
                            "note": "n",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        extrabuilds.load_cells("MID2", path=bad)
    except extrabuilds.ExtraBuildError as error:
        assert "guessed" in str(error)
    else:
        raise AssertionError("an origin nobody defined must not load")


def test_a_tier_without_cells_is_empty_not_an_error():
    assert extrabuilds.load_cells("MID1") == []
