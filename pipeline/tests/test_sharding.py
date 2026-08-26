"""Sharding must cover every profile exactly once, however the matrix is split."""

from __future__ import annotations

from pathlib import Path

import pytest

from wowdps.cli import _parse_shard, _select
from wowdps.profiles import SpecProfile


def make_profile(index: int) -> SpecProfile:
    return SpecProfile(
        path=Path(f"/tmp/p{index}.simc"),
        tier="MID2",
        wow_class=f"Class{index % 4}",
        spec=f"Spec{index}",
        hero_talent=None,
        role="spell",
        talent_hash=None,
    )


@pytest.mark.parametrize("shard_count", [1, 2, 3, 6, 7, 20])
def test_shards_partition_the_profiles(shard_count):
    all_profiles = [make_profile(i) for i in range(17)]

    seen: list[str] = []
    for index in range(shard_count):
        shard = _select(all_profiles, None, None, None, (index, shard_count))
        seen.extend(profile.id for profile in shard)

    assert sorted(seen) == sorted(profile.id for profile in all_profiles)
    assert len(seen) == len(set(seen)), "a profile landed in two shards"


@pytest.mark.parametrize("shard_count", [2, 3, 6])
def test_shards_are_balanced(shard_count):
    all_profiles = [make_profile(i) for i in range(17)]
    sizes = [
        len(_select(all_profiles, None, None, None, (index, shard_count)))
        for index in range(shard_count)
    ]

    assert max(sizes) - min(sizes) <= 1


def test_parse_shard():
    assert _parse_shard("0/6") == (0, 6)
    assert _parse_shard("5/6") == (5, 6)
    assert _parse_shard(None) is None


@pytest.mark.parametrize("bad", ["6/6", "-1/6", "1/0", "abc", "1"])
def test_bad_shard_specs_are_rejected(bad):
    with pytest.raises(SystemExit):
        _parse_shard(bad)


def test_class_filter_is_case_and_separator_insensitive():
    profiles = [make_profile(i) for i in range(4)]
    profiles[0] = SpecProfile(
        path=Path("/tmp/dk.simc"),
        tier="MID2",
        wow_class="Death Knight",
        spec="Frost",
        hero_talent=None,
        role="attack",
        talent_hash=None,
    )

    assert len(_select(profiles, ["death_knight"], None, None)) == 1
    assert len(_select(profiles, ["Death Knight"], None, None)) == 1


def test_buff_shards_merge_into_one_document(tmp_path):
    """A buff sweep shards by spec, so merging is the union of those slices.

    Without this the sweep produced six shard artifacts and nothing published --
    the workflow uploaded them and there was no step that put them together.
    """
    import json

    from wowdps.dataset import merge_buff_shards

    shards = []
    for index, spec_id in enumerate(("mage_fire", "mage_frost")):
        shard = tmp_path / f"s{index}"
        shard.mkdir()
        (shard / "buffs.json").write_text(
            json.dumps(
                {
                    "tier": "MID2",
                    "generatedAt": f"2026-08-17T0{index}:00:00+00:00",
                    "specs": [{"id": spec_id, "baseDps": 100.0 + index}],
                }
            ),
            encoding="utf-8",
        )
        shards.append(shard)

    out = tmp_path / "out"
    out.mkdir()
    path = merge_buff_shards(shards, out)

    assert path is not None
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert [spec["id"] for spec in merged["specs"]] == ["mage_fire", "mage_frost"]
    # The header comes from the newest shard, as it does for the gear merge.
    assert merged["generatedAt"] == "2026-08-17T01:00:00+00:00"


def test_a_run_with_no_buff_shards_merges_nothing_rather_than_writing_an_empty_file(tmp_path):
    from wowdps.dataset import merge_buff_shards

    out = tmp_path / "out"
    out.mkdir()
    assert merge_buff_shards([], out) is None
    assert not (out / "buffs.json").exists()


def test_the_merged_buff_coverage_is_recounted_not_taken_from_one_shard(tmp_path):
    """``merged`` starts as the NEWEST shard's header, whose ``coverage.specs`` is
    that shard's own slice. Publishing it as the run's is the defect CLAUDE.md
    already records twice -- ``medianDpsError`` and ``targetError`` -- a number
    describing a fraction of the run presented as describing all of it."""
    import json

    from wowdps.dataset import merge_buff_shards

    shards = []
    for index, spec_id in enumerate(("mage_fire", "mage_frost")):
        shard = tmp_path / f"s{index}"
        shard.mkdir()
        (shard / "buffs.json").write_text(
            json.dumps(
                {
                    "tier": "MID2",
                    "generatedAt": f"2026-08-17T0{index}:00:00+00:00",
                    # Each shard covered ONE build of a tier holding 52.
                    "coverage": {"specs": 1, "specsAvailable": 52},
                    "specs": [{"id": spec_id, "baseDps": 100.0 + index}],
                }
            ),
            encoding="utf-8",
        )
        shards.append(shard)

    merged = json.loads(merge_buff_shards(shards, tmp_path).read_text(encoding="utf-8"))
    # Two arrived, so the merge covers two -- not the last shard's 1.
    assert merged["coverage"] == {"specs": 2, "specsAvailable": 52}


def test_buff_shards_that_state_no_coverage_produce_no_coverage_block(tmp_path):
    """Absent is not zero. A document written before the block existed must not be
    merged into one claiming the sweep covered nothing."""
    import json

    from wowdps.dataset import merge_buff_shards

    shard = tmp_path / "s0"
    shard.mkdir()
    (shard / "buffs.json").write_text(
        json.dumps(
            {
                "tier": "MID2",
                "generatedAt": "2026-08-17T00:00:00+00:00",
                "specs": [{"id": "mage_fire", "baseDps": 100.0}],
            }
        ),
        encoding="utf-8",
    )
    merged = json.loads(merge_buff_shards([shard], tmp_path).read_text(encoding="utf-8"))
    assert "coverage" not in merged


def test_write_buffs_omits_coverage_when_the_tier_size_is_unknown(tmp_path):
    """A caller that does not know the tier size must not be made to assert one."""
    import json

    from wowdps.buffsweep import write_buffs
    from wowdps.scenarios import SimSettings

    settings = SimSettings(target_error=0.0, max_iterations=300, threads=1)
    path = write_buffs(tmp_path, "MID2", [], settings)
    assert "coverage" not in json.loads(path.read_text(encoding="utf-8"))

    path = write_buffs(tmp_path, "MID2", [], settings, builds_available=52)
    assert json.loads(path.read_text(encoding="utf-8"))["coverage"] == {
        "specs": 0,
        "specsAvailable": 52,
    }
