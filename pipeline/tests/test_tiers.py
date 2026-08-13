"""The tier axis: labels, the derived tier index, and merging shards per tier."""

from __future__ import annotations

import json

import pytest

from wowdps.cli import _resolve_tier
from wowdps.dataset import merge_shards, write_tier_index
from wowdps.profiles import available_tiers, latest_tier, previous_tier, tier_label


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("MID2", "Midnight Season 2"),
        ("MID1", "Midnight Season 1"),
        ("TWW3", "The War Within Season 3"),
        # Unknown prefixes and non-conforming names are shown verbatim rather than
        # guessed at, so a new expansion needs no code change to appear at all.
        ("XYZ9", "XYZ9"),
        ("PreRaids", "PreRaids"),
    ],
)
def test_tier_labels(tier, expected):
    assert tier_label(tier) == expected


def write_manifest(root, tier, spec_ids, generated_at="2026-08-13T00:00:00+00:00"):
    tier_dir = root / tier
    (tier_dir / "specs").mkdir(parents=True, exist_ok=True)
    (tier_dir / "index.json").write_text(
        json.dumps(
            {
                "tier": tier,
                "generatedAt": generated_at,
                "simc": {"simcVersion": "1210-01"},
                "specs": [{"id": spec_id} for spec_id in spec_ids],
            }
        ),
        encoding="utf-8",
    )
    for spec_id in spec_ids:
        (tier_dir / "specs" / f"{spec_id}.json").write_text(
            json.dumps({"id": spec_id}), encoding="utf-8"
        )
    return tier_dir


def test_tier_index_is_derived_from_what_exists(tmp_path):
    write_manifest(tmp_path, "MID1", ["mage_fire_default"])
    write_manifest(tmp_path, "MID2", ["mage_fire_sunfury", "mage_fire_frostfire"])

    write_tier_index(tmp_path)
    index = json.loads((tmp_path / "tiers.json").read_text())

    assert [t["id"] for t in index["tiers"]] == ["MID1", "MID2"]
    assert index["current"] == "MID2", "the highest-numbered tier present is the current one"
    assert index["tiers"][1]["specCount"] == 2
    assert index["tiers"][0]["label"] == "Midnight Season 1"


def test_deleting_a_tier_removes_it_from_the_index(tmp_path):
    write_manifest(tmp_path, "MID1", ["mage_fire_default"])
    write_manifest(tmp_path, "MID2", ["mage_fire_sunfury"])
    write_tier_index(tmp_path)

    # The index is regenerated, never accumulated: a tier that is gone stays gone.
    (tmp_path / "MID1" / "index.json").unlink()
    write_tier_index(tmp_path)

    index = json.loads((tmp_path / "tiers.json").read_text())
    assert [t["id"] for t in index["tiers"]] == ["MID2"]
    assert index["current"] == "MID2"


def test_tier_index_needs_at_least_one_tier(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_tier_index(tmp_path)


def test_merge_keeps_tiers_apart(tmp_path):
    """A shard may carry several tiers; merging must not mix their spec lists."""
    shard_a = tmp_path / "shard-0"
    shard_b = tmp_path / "shard-1"
    write_manifest(shard_a, "MID2", ["mage_fire_sunfury"], "2026-08-13T01:00:00+00:00")
    write_manifest(shard_b, "MID2", ["mage_fire_frostfire"], "2026-08-13T02:00:00+00:00")
    write_manifest(shard_a, "MID1", ["rogue_subtlety_default"])

    out = tmp_path / "data"
    for tier in ("MID1", "MID2"):
        sources = [shard / tier for shard in (shard_a, shard_b) if (shard / tier).is_dir()]
        merge_shards(sources, out / tier)
    write_tier_index(out)

    mid2 = json.loads((out / "MID2" / "index.json").read_text())
    mid1 = json.loads((out / "MID1" / "index.json").read_text())

    assert [s["id"] for s in mid2["specs"]] == ["mage_fire_frostfire", "mage_fire_sunfury"]
    assert [s["id"] for s in mid1["specs"]] == ["rogue_subtlety_default"]
    assert (out / "MID2" / "specs" / "mage_fire_sunfury.json").is_file()
    assert not (out / "MID1" / "specs" / "mage_fire_sunfury.json").exists()

    index = json.loads((out / "tiers.json").read_text())
    assert [t["id"] for t in index["tiers"]] == ["MID1", "MID2"]


def make_tier_dir(root, tier, count=1):
    tier_dir = root / tier
    tier_dir.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (tier_dir / f"{tier}_Mage_Fire{index}.simc").write_text("mage=x\n", encoding="utf-8")
    return tier_dir


def test_tier_discovery_ignores_non_tier_directories(tmp_path):
    make_tier_dir(tmp_path, "MID1")
    make_tier_dir(tmp_path, "MID2")
    # Neither of these is a tier, and neither may shift what "previous" means.
    (tmp_path / "generators").mkdir()
    (tmp_path / "PreRaids").mkdir()
    (tmp_path / "PreRaids" / "x.simc").write_text("mage=x\n", encoding="utf-8")
    # An empty tier directory is a tier simc has not filled in yet, not the current one.
    (tmp_path / "MID3").mkdir()

    assert available_tiers(tmp_path) == ["MID1", "MID2"]
    assert latest_tier(tmp_path) == "MID2"
    assert previous_tier(tmp_path) == "MID1"


def test_resolve_tier_tokens(tmp_path):
    make_tier_dir(tmp_path, "MID1")
    make_tier_dir(tmp_path, "MID2")

    assert _resolve_tier(tmp_path, "latest") == "MID2"
    assert _resolve_tier(tmp_path, None) == "MID2"
    assert _resolve_tier(tmp_path, "previous") == "MID1"
    # Anything else is taken literally, so a one-off run can name a tier directly.
    assert _resolve_tier(tmp_path, "MID1") == "MID1"


def test_previous_needs_two_tiers(tmp_path):
    make_tier_dir(tmp_path, "MID2")
    with pytest.raises(FileNotFoundError):
        previous_tier(tmp_path)
