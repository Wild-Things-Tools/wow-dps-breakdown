"""The gear sweep, with simc replaced by a stub.

What these tests pin is the *method*, not the arithmetic — every one of them is a
decision that would still produce a plausible number if it were made differently:

* the baseline pair is the two highest **standalone** values, measured against both
  sockets empty rather than against some arbitrary partner;
* a candidate replaces the **second** baseline item, i.e. the weaker of the two,
  because "does this drop beat my worse trinket" is the question a loot council
  actually has;
* the baseline is itself a **profileset**, never the base actor — the base actor runs
  a different iteration count and lands outside its own error;
* a gain smaller than the two runs' combined standard error is a **tie**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wowdps import gearsweep, simc_runner
from wowdps.equipment import TRINKET, GearItem, ItemLevel, SlotPool
from wowdps.gearsweep import Equipped, Variant
from wowdps.profiles import SpecProfile
from wowdps.scenarios import SimSettings

HEROIC = ItemLevel("heroic", "Heroic", 334)
MYTHIC = ItemLevel("mythic", "Mythic", 344)


def gear(item_id: int, source: str, stat: str | None = "intellect") -> GearItem:
    return GearItem(
        item_id=item_id,
        name=f"Item {item_id}",
        slug=f"item_{item_id}",
        primary_stat=stat,
        secondary_stat=None,
        source=source,
        base_ilevel=219,
        base_quality=4,
    )


MPLUS = [gear(101, "mythicplus"), gear(102, "mythicplus"), gear(103, "mythicplus")]
RAID = [gear(201, "raid"), gear(202, "raid")]

POOL = SlotPool(
    tier="MID2",
    slot=TRINKET,
    items=(*MPLUS, *RAID),
    item_levels=(HEROIC, MYTHIC),
    baseline_source="mythicplus",
    candidate_source="raid",
)


def spec_profile(path: Path) -> SpecProfile:
    return SpecProfile(
        path=path,
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent="Sunfury",
        role="spell",
        talent_hash=None,
    )


# The sweep reads the spec's primary stat off the profile itself, so the stub needs
# a real file with a gear summary in it rather than a bare path.
PROFILE = spec_profile(Path("/dev/null"))


@pytest.fixture
def profile(tmp_path) -> SpecProfile:
    path = tmp_path / "MID2_Mage_Arcane_Sunfury.simc"
    path.write_text("mage=X\nspec=arcane\n# gear_intellect=2751\n", encoding="utf-8")
    return spec_profile(path)


# --------------------------------------------------------------------------------
# Option rendering
# --------------------------------------------------------------------------------


def as_options(variants):
    """What the shared profileset runner will render these variants as."""
    from wowdps.simc_runner import Profileset, profileset_options

    return profileset_options(
        [Profileset(key=v.key, options=tuple(v.equipped.simc_options())) for v in variants]
    )


def test_profileset_options_open_with_equals_and_continue_with_plus_equals():
    variant = Variant(
        key="standard",
        equipped=Equipped(sockets=(("trinket1", MPLUS[0], 334), ("trinket2", MPLUS[1], 334))),
    )
    assert as_options([variant]) == [
        "profileset.standard=trinket1=,id=101,ilevel=334",
        "profileset.standard+=trinket2=,id=102,ilevel=334",
    ]


def test_a_solo_variant_leaves_every_other_socket_empty():
    # An empty partner is the neutral partner: it cannot share an on-use window with
    # the item being measured, so the value is the item's own.
    [variant] = gearsweep._solo_variants(TRINKET, [MPLUS[0]], 334, {})
    assert as_options([variant]) == [
        "profileset.solo_item_101=trinket1=,id=101,ilevel=334",
        "profileset.solo_item_101+=trinket2=",
    ]


# --------------------------------------------------------------------------------
# Report parsing
# --------------------------------------------------------------------------------


def report_with(results: list[dict]) -> dict:
    return {"sim": {"profilesets": {"results": results}}}


def test_parsing_reads_the_relative_error_and_the_priority_metric(monkeypatch):
    monkeypatch.setattr(
        simc_runner,
        "run",
        lambda *args, **kwargs: report_with(
            [
                {
                    "name": "solo_item_101",
                    "mean": 200_000.0,
                    "mean_stddev": 200.0,
                    "iterations": 3000,
                    "additional_metrics": [
                        {"metric": "Damage per Second to Priority Target/Boss", "mean": 90_000.0}
                    ],
                }
            ]
        ),
    )
    results = gearsweep._run(Path("simc"), PROFILE, 5, SimSettings(), [], timeout=60)
    entry = results["solo_item_101"]
    assert entry.dps == 200_000.0
    assert entry.dps_error == pytest.approx(0.1)
    assert entry.priority_dps == 90_000.0


def test_parsing_drops_variants_simc_returned_nothing_for(monkeypatch):
    monkeypatch.setattr(
        simc_runner, "run", lambda *args, **kwargs: report_with([{"name": "x", "mean": 0.0}])
    )
    assert gearsweep._run(Path("simc"), PROFILE, 1, SimSettings(), [], timeout=60) == {}


def test_the_run_always_sets_profileset_work_threads(monkeypatch):
    # Without it every variant silently runs at iterations/threads, i.e. at a
    # fraction of the precision the numbers are reported to.
    seen: list[str] = []

    def capture(simc, request, settings, timeout=0, **kwargs):
        seen.extend(settings.as_simc_options())
        return report_with([])

    monkeypatch.setattr(simc_runner, "run", capture)
    gearsweep._run(Path("simc"), PROFILE, 1, SimSettings(), [], timeout=60)
    assert "profileset_work_threads=1" in seen


# --------------------------------------------------------------------------------
# The tie rule
# --------------------------------------------------------------------------------


def candidate(gain: float, gain_error: float) -> gearsweep.CandidateResult:
    return gearsweep.CandidateResult(
        item=RAID[0],
        item_level=MYTHIC,
        replaces=MPLUS[1],
        dps=1.0,
        dps_error=0.0,
        priority_dps=None,
        gain=gain,
        gain_error=gain_error,
    )


@pytest.mark.parametrize(
    ("gain", "error", "tie"),
    [
        (0.010, 0.002, False),
        (0.001, 0.002, True),
        (-0.001, 0.002, True),  # a loss inside the noise is also a tie
        (-0.010, 0.002, False),
        (0.002, 0.002, True),  # exactly at the noise floor is not a lead
    ],
)
def test_a_margin_inside_the_combined_error_is_a_tie(gain, error, tie):
    assert candidate(gain, error).is_tie is tie


# --------------------------------------------------------------------------------
# The whole sweep
# --------------------------------------------------------------------------------

# item 102 is the best standalone, 101 second, 103 worst. Both raid items beat the
# baseline pair, and 201 gains more at the higher item level.
STUB_DPS = {
    "empty": 100_000.0,
    "solo_item_101": 110_000.0,
    "solo_item_102": 112_000.0,
    "solo_item_103": 105_000.0,
    "standard": 122_000.0,
    "cand_item_201_heroic": 123_000.0,
    "cand_item_201_mythic": 125_000.0,
    "cand_item_202_heroic": 121_000.0,
    "cand_item_202_mythic": 122_500.0,
}


@pytest.fixture
def stub_simc(monkeypatch):
    """Replace simc with a lookup table, recording the options each run was given."""
    calls: list[list[str]] = []

    def fake_run(simc, request, settings, timeout=0, **kwargs):
        options = list(settings.as_simc_options())
        calls.append(options)
        names = {
            option.split("=", 1)[0].removeprefix("profileset.").removesuffix("+")
            for option in options
            if option.startswith("profileset.")
        }
        return report_with(
            [
                {"name": name, "mean": STUB_DPS[name], "mean_stddev": 100.0, "iterations": 3000}
                for name in sorted(names)
                if name in STUB_DPS
            ]
        )

    monkeypatch.setattr(simc_runner, "run", fake_run)
    return calls


def test_sweep_picks_the_two_highest_standalone_values(stub_simc, profile):
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    [target] = result.targets
    assert [item.item_id for item in target.baseline] == [102, 101]
    assert target.empty_dps == 100_000.0
    # The runners-up are published too, so a near-tie at the cut is visible.
    assert [entry.item.item_id for entry in target.pool] == [102, 101, 103]
    assert [entry.chosen for entry in target.pool] == [True, True, False]


def test_sweep_prices_the_baseline_at_the_lower_item_level(stub_simc, profile):
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    assert result.targets[0].baseline_ilevel == 334


def test_candidates_replace_the_weaker_baseline_item(stub_simc, profile):
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    [target] = result.targets
    assert {entry.replaces.item_id for entry in target.candidates} == {101}

    # And the options bear that out: the better item stays in socket one.
    candidate_options = [
        option for option in stub_simc[-1] if option.startswith("profileset.cand_item_201_mythic")
    ]
    assert candidate_options == [
        "profileset.cand_item_201_mythic=trinket1=,id=102,ilevel=334",
        "profileset.cand_item_201_mythic+=trinket2=,id=201,ilevel=344",
    ]


def test_gain_is_measured_against_the_baseline_profileset(stub_simc, profile):
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    [target] = result.targets
    assert target.baseline_dps == 122_000.0
    best = target.candidates[0]
    assert best.item.item_id == 201
    assert best.item_level.id == "mythic"
    assert best.gain == pytest.approx(125_000.0 / 122_000.0 - 1)
    # Results are ranked, so the head of the list is the best drop for this spec.
    assert [entry.gain for entry in target.candidates] == sorted(
        (entry.gain for entry in target.candidates), reverse=True
    )


def test_the_baseline_and_the_candidates_share_one_simc_run(stub_simc, profile):
    # They have to: a gain is a difference between two profilesets, and comparing a
    # profileset against the base actor is the one comparison that does not hold.
    gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    last = stub_simc[-1]
    assert any(option.startswith("profileset.standard=") for option in last)
    assert any(option.startswith("profileset.cand_") for option in last)


def test_a_spec_with_too_few_eligible_baseline_items_reports_rather_than_guesses(
    stub_simc, profile
):
    thin = SlotPool(
        tier="MID2",
        slot=TRINKET,
        items=(gear(101, "mythicplus"), *RAID),
        item_levels=(HEROIC, MYTHIC),
        baseline_source="mythicplus",
        candidate_source="raid",
    )
    result = gearsweep.sweep_spec(Path("simc"), profile, thin, SimSettings(), [1], timeout=60)
    assert result.targets == []
    assert "need 2 to form a baseline" in result.errors[0]


def test_one_failing_target_count_does_not_lose_the_others(monkeypatch, stub_simc, profile):
    real = simc_runner.run

    def flaky(simc, request, settings, timeout=0, **kwargs):
        if request.targets == 5:
            raise RuntimeError("simc exploded")
        return real(simc, request, settings, timeout=timeout, **kwargs)

    monkeypatch.setattr(simc_runner, "run", flaky)
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1, 5], timeout=60)
    assert [target.targets for target in result.targets] == [1]
    assert "5T" in result.errors[0]


# --------------------------------------------------------------------------------
# Sharded output
# --------------------------------------------------------------------------------


def gear_document(spec_ids: list[str], generated: str, available: int = 26) -> dict:
    return {
        "schemaVersion": 1,
        "generatedAt": generated,
        "tier": "MID2",
        "coverage": {"specs": len(spec_ids), "specsAvailable": available},
        "slots": [
            {
                "id": "trinket",
                "label": "Trinket",
                "items": [{"id": 270164, "name": "Gebbo's Bottomless Bag"}],
                "specs": [{"id": spec_id, "targets": []} for spec_id in spec_ids],
            }
        ],
    }


def write_shard(root, name: str, document: dict):
    import json

    shard = root / name
    shard.mkdir()
    (shard / "gear.json").write_text(json.dumps(document), encoding="utf-8")
    return shard


def test_gear_shards_merge_into_the_union_of_their_specs(tmp_path):
    import json

    from wowdps import dataset

    shards = [
        write_shard(tmp_path, "a", gear_document(["mage_arcane_sunfury"], "2026-01-01T00:00:00Z")),
        write_shard(tmp_path, "b", gear_document(["priest_shadow_archon"], "2026-01-01T01:00:00Z")),
    ]
    out = tmp_path / "out"
    out.mkdir()
    dataset.merge_gear_shards(shards, out)

    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    assert [spec["id"] for spec in merged["slots"][0]["specs"]] == [
        "mage_arcane_sunfury",
        "priest_shadow_archon",
    ]
    # Coverage is recounted from what arrived: a shard that failed has to shrink the
    # published figure, not be papered over by whatever the last shard claimed.
    assert merged["coverage"] == {"specs": 2, "specsAvailable": 26}


def test_merging_no_gear_shards_is_a_no_op(tmp_path):
    from wowdps import dataset

    empty = tmp_path / "a"
    empty.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    assert dataset.merge_gear_shards([empty], out) is None
    assert not (out / "gear.json").exists()


# --------------------------------------------------------------------------------
# Slots that are not trinkets
# --------------------------------------------------------------------------------


def test_the_baseline_is_one_item_per_socket():
    """Hard-coded to two while trinkets were the only swept slot. Correct for two
    sockets, and silently wrong for a neck, where it would pick two items for one."""
    from wowdps.equipment import FINGER, NECK, TRINKET
    from wowdps.gearsweep import baseline_size

    assert baseline_size(TRINKET) == 2
    assert baseline_size(FINGER) == 2
    assert baseline_size(NECK) == 1


def test_a_candidate_replaces_the_only_item_on_a_one_socket_slot():
    """The candidate list was `[baseline_items[0], item]`, which assumes two sockets.
    With one, zip() truncates to the first entry -- the candidate is silently never
    equipped and the run measures the baseline against itself."""
    from wowdps.equipment import NECK
    from wowdps.gearsweep import _pair_variant

    baseline, candidate = MPLUS[0], MPLUS[1]
    kept = [baseline][:-1]
    variant = _pair_variant("cand", NECK, [*kept, candidate], [334], {})
    options = as_options([variant])
    assert options == ["profileset.cand=neck=,id=102,ilevel=334"]


def test_a_ring_carries_the_profiles_gem_and_enchant_on_both_sides():
    """Measured at +1.55% together against +0.09% for a ten-item-level step, so a
    comparison that drops them measures the wrong thing by an order of magnitude."""
    from wowdps.equipment import FINGER, SlotAdornment
    from wowdps.gearsweep import _pair_variant

    adornments = {
        "finger1": SlotAdornment(gem_ids=(240906,), enchant_id=7967),
        "finger2": SlotAdornment(gem_ids=(240916,), enchant_id=7967),
    }
    variant = _pair_variant("standard", FINGER, [MPLUS[0], MPLUS[1]], [334, 334], adornments)
    assert as_options([variant]) == [
        "profileset.standard=finger1=,id=101,ilevel=334,gem_id=240906,enchant_id=7967",
        "profileset.standard+=finger2=,id=102,ilevel=334,gem_id=240916,enchant_id=7967",
    ]


def test_a_trinket_stays_bare_because_it_has_nowhere_to_put_a_gem():
    """Measured: passing a trinket's own bonus ids alongside an explicit item level
    returned DPS identical to the last digit."""
    from wowdps.equipment import TRINKET, SlotAdornment
    from wowdps.gearsweep import _pair_variant

    variant = _pair_variant(
        "standard", TRINKET, [MPLUS[0], MPLUS[1]], [334, 334], {"trinket1": SlotAdornment()}
    )
    assert as_options([variant]) == [
        "profileset.standard=trinket1=,id=101,ilevel=334",
        "profileset.standard+=trinket2=,id=102,ilevel=334",
    ]


def test_sweeping_one_slot_does_not_delete_the_others(tmp_path):
    """`write_gear` emits an entry for every pool, so a neck run writes a trinket
    slot with an empty specs array. Merging over shards alone would publish that as
    the trinket comparison -- deleting a sweep that costs an hour to reproduce."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-15T12:00:00+00:00",
                "coverage": {"specs": 2, "specsAvailable": 26},
                "slots": [
                    {"id": "trinket", "label": "Trinket", "specs": [{"id": "a"}, {"id": "b"}]}
                ],
            }
        ),
        encoding="utf-8",
    )

    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-15T14:00:00+00:00",
                "coverage": {"specs": 1, "specsAvailable": 26},
                "slots": [
                    {"id": "trinket", "label": "Trinket", "specs": []},
                    {"id": "neck", "label": "Neck", "specs": [{"id": "a"}]},
                ],
            }
        ),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    by_slot = {slot["id"]: slot for slot in merged["slots"]}
    assert [s["id"] for s in by_slot["trinket"]["specs"]] == ["a", "b"]
    assert [s["id"] for s in by_slot["neck"]["specs"]] == ["a"]
