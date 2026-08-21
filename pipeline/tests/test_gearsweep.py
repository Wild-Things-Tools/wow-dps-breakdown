"""The gear sweep, with simc replaced by a stub.

What these tests pin is the *method*, not the arithmetic — every one of them is a
decision that would still produce a plausible number if it were made differently:

* the baseline pair is the **best measured combination**, not the two items that
  ranked highest alone -- and the additive rule is the fallback for a pool too large
  to enumerate, never the method;
* the ceiling is drawn from the **same enumeration**, over the whole pool rather
  than the farmed half, so it can name a set the baseline-then-one-swap search
  cannot reach;
* a candidate replaces the **second** baseline item, i.e. the weaker of the two,
  because "does this drop beat my worse trinket" is the question a loot council
  actually has;
* the baseline is itself a **profileset**, never the base actor — the base actor runs
  a different iteration count and lands outside its own error;
* a gain smaller than the two runs' combined standard error is a **tie**.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from wowdps import gearsweep, simc_runner
from wowdps.equipment import TRINKET, EquipmentSlot, GearItem, ItemLevel, SlotPool
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


def test_a_pool_with_no_measured_combination_falls_back_to_standalone_value(stub_simc, profile):
    """The additive rule, which is the fallback and no longer the method.

    ``STUB_DPS`` carries no combination keys, so every enumerated combination comes
    back unmeasured and the sweep has nothing to rank -- exactly the state a pool
    over ``MAX_BASELINE_COMBINATIONS`` is in. It must then take the top two by
    standalone value and *say* that is what it did, rather than publish an
    exhaustive-looking answer it did not measure.
    """
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    [target] = result.targets
    assert [item.item_id for item in target.baseline] == [102, 101]
    assert target.empty_dps == 100_000.0
    assert target.baseline_method == gearsweep.ADDITIVE
    assert target.baseline_combinations == 0
    assert target.best_sets == []
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


def test_a_one_socket_sweep_runs_end_to_end(monkeypatch, tmp_path):
    """The seam the unit tests did not cover, and where it actually broke.

    Every variant builder was correct for a neck, and the run still failed on every
    spec with "list index out of range" -- from a *progress log line* that read
    `baseline_items[0] + baseline_items[1]`. So the sweep is exercised here with a
    stubbed runner: no simc, but every line between picking a baseline and returning
    a result gets executed, logging included.
    """
    from wowdps.equipment import NECK, SlotPool
    from wowdps.gearsweep import VariantResult, _sweep_one

    pool = SlotPool(
        tier="MID2",
        slot=NECK,
        items=(*MPLUS, *RAID),
        item_levels=(HEROIC,),
        baseline_source="mythicplus",
        candidate_source="raid",
    )

    # Distinct DPS per variant so a candidate that was never equipped would show up as
    # a gain of exactly zero rather than hiding in the noise.
    def fake_run(simc, profile, targets, settings, variants, timeout):
        out = {}
        for index, variant in enumerate(variants):
            out[variant.key] = VariantResult(
                key=variant.key, dps=100_000 + index * 1_000, dps_error=0.05, iterations=1000
            )
        return out

    monkeypatch.setattr(gearsweep, "_run", fake_run)

    profile_path = tmp_path / "MID2_Mage_Arcane.simc"
    profile_path.write_text(
        "neck=aqirbane,id=268265,ilevel=344,gem_id=240892/240906\n", encoding="utf-8"
    )
    profile = SpecProfile(
        path=profile_path,
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent="Sunfury",
        role="spell",
        talent_hash=None,
        name_hero="Sunfury",
    )

    result = _sweep_one(
        simc=Path("simc"),
        profile=profile,
        pool=pool,
        settings=SimSettings(target_error=0, max_iterations=1000),
        targets=1,
        primary="intellect",
        baseline_pool=list(MPLUS),
        candidates=list(RAID),
        baseline_level=HEROIC,
        timeout=600,
    )

    # One socket, one baseline item -- not two.
    assert len(result.baseline) == 1
    # And the candidates were actually measured, each against that baseline.
    assert len(result.candidates) == len(RAID)
    assert all(entry.gain != 0 for entry in result.candidates)
    # The ceiling has to survive one socket too: for a neck it is the best single
    # item in the whole pool, which is a real answer and not a degenerate one.
    assert [len(entry.worn.offers) for entry in result.best_sets] == [1]


FINGER_SLOT = EquipmentSlot(
    id="finger", label="Ring", sockets=("finger1", "finger2"), inventory_type=11
)
NECK_SLOT = EquipmentSlot(id="neck", label="Neck", sockets=("neck",), inventory_type=2)


def offers(items, level):
    return [gearsweep.Offer(item, level) for item in items]


def test_the_baseline_is_the_best_combination_not_the_best_items():
    """Two items that individually place third and fourth can beat the top two.

    Standalone value is additive to about 3% -- close enough to rank a clear
    winner, not close enough to trust at the cut, where two items overlap (two
    procs competing for the same global cooldowns, two on-use effects that cannot
    both be pressed). This is the case that separates the two methods, and the old
    top-two-standalone rule gets it wrong.
    """
    from wowdps.gearsweep import _combination_variants, _combo_key

    items = [gear(1, "mythicplus"), gear(2, "mythicplus"), gear(3, "mythicplus")]
    worn = offers(items, HEROIC)

    combos = _combination_variants(FINGER_SLOT, worn, [])

    # Every pair, and only pairs -- one per way of filling the two sockets.
    assert len(combos) == 3
    assert set(combos) == {
        _combo_key((worn[0], worn[1])),
        _combo_key((worn[0], worn[2])),
        _combo_key((worn[1], worn[2])),
    }


def test_a_one_socket_slot_yields_one_combination_per_item():
    """For a neck, "the best combination" and "the best item" are the same question."""
    from wowdps.gearsweep import _combination_variants

    items = [gear(i, "mythicplus") for i in (1, 2, 3)]
    assert len(_combination_variants(NECK_SLOT, offers(items, HEROIC), [])) == 3


def test_the_enumeration_covers_the_drops_at_every_item_level():
    """The step the baseline-then-one-swap search cannot take.

    Farmed items are priced at one level, so their pairs are enumerated once. A drop
    is offered at each level it can drop at, and every pair containing one is run at
    that level -- including pairs of two drops, which no swap into the baseline can
    ever produce.
    """
    from wowdps.gearsweep import _combination_variants

    worn = offers([gear(i, "mythicplus") for i in (1, 2, 3)], HEROIC)
    drops = [
        gearsweep.Offer(item, level)
        for item in (gear(201, "raid"), gear(202, "raid"))
        for level in (HEROIC, MYTHIC)
    ]

    combos = _combination_variants(FINGER_SLOT, worn, drops)

    # 3 farmed pairs, plus per level (3x2 farmed-with-drop + 1 drop pair) x 2.
    assert len(combos) == 3 + 2 * 7
    both_drops = [
        combo for combo in combos.values() if all(offer.item.source == "raid" for offer in combo)
    ]
    assert len(both_drops) == 2


def test_one_variant_never_mixes_two_drop_item_levels():
    """A ring at 334 beside a ring at 344 is a third question, and the published
    comparison has one item-level control rather than two."""
    from wowdps.gearsweep import _combination_variants

    drops = [
        gearsweep.Offer(item, level)
        for item in (gear(201, "raid"), gear(202, "raid"))
        for level in (HEROIC, MYTHIC)
    ]
    combos = _combination_variants(FINGER_SLOT, [], drops)

    for combo in combos.values():
        assert len({offer.level.id for offer in combo}) == 1
        # And no set wears the same ring twice, which simc would answer by leaving
        # a socket empty -- a plausible number for a set nobody could wear.
        assert len({offer.item.item_id for offer in combo}) == len(combo)


def test_a_farmed_item_is_priced_at_one_level_and_a_drop_at_every_level():
    """``baseline_ilevel``'s rule, applied per item. Pricing a Mythic+ ring at the
    raid's top level is the flattery that rule exists to prevent."""
    assert POOL.wearable_levels(MPLUS[0]) == (HEROIC,)
    assert POOL.wearable_levels(RAID[0]) == (HEROIC, MYTHIC)


def test_the_exhaustive_search_is_bounded_by_a_stated_ceiling():
    """Trinkets are deliberately outside it, and the numbers say why.

    Counted against MID2's derived pools. The count is the *Mythic+* half for a
    baseline and the whole pool for a ceiling -- not the pool size, which is what an
    earlier version of this test asserted and what CLAUDE.md's table repeated.
    """
    from math import comb

    from wowdps.gearsweep import MAX_BASELINE_COMBINATIONS

    def full(farmed: int, dropped: int, sockets: int, levels: int = 2) -> int:
        """Farmed combinations once, plus every combination with a drop, per level."""
        base = comb(farmed, sockets)
        return base + levels * (comb(farmed + dropped, sockets) - base)

    # neck: 4 Mythic+, 3 raid, one socket.
    assert comb(4, 1) <= MAX_BASELINE_COMBINATIONS
    assert full(4, 3, 1) == 10 <= MAX_BASELINE_COMBINATIONS
    # finger: 8 Mythic+, 3 raid, two sockets.
    assert comb(8, 2) == 28 <= MAX_BASELINE_COMBINATIONS
    assert full(8, 3, 2) == 82 <= MAX_BASELINE_COMBINATIONS
    # trinket: 27 Mythic+, 15 raid. Out of budget on the baseline alone, so neither
    # answer is measured and the method is recorded as additive.
    assert comb(27, 2) == 351 > MAX_BASELINE_COMBINATIONS
    assert full(27, 15, 2) == 1371 > MAX_BASELINE_COMBINATIONS


# --------------------------------------------------------------------------------
# The exhaustive search, end to end
# --------------------------------------------------------------------------------
#
# One stub table drives every test below, and it is built so that each of the three
# possible methods gives a *different* answer. Nothing here is arithmetic: a test
# that only checked the numbers add up would pass under all three.
#
#   singles         B 112k > A 110k > C 105k          "the two best alone" says B + A
#   farmed pairs    A + C 122k > B + C 120k > A + B 118k    the measured pair is A + C
#   with a drop     B + R1 135k > A + R1 130k         the ceiling is B + R1
#
# A and B overlap, so the pair of the two best singles is the worst farmed pair --
# the case the additive rule was measured to get wrong on 13 of 26 builds. And B is
# not in the baseline pair, so no swap into {A, C} can ever reach B + R1: the
# two-step search is structurally blind to it, however many item levels it tries.

RING_A, RING_B, RING_C = (gear(101, "mythicplus"), gear(102, "mythicplus"), gear(103, "mythicplus"))
DROP_1, DROP_2 = gear(201, "raid"), gear(202, "raid")

RING_POOL = SlotPool(
    tier="MID2",
    slot=EquipmentSlot(
        id="finger", label="Ring", sockets=("finger1", "finger2"), inventory_type=11
    ),
    items=(RING_A, RING_B, RING_C, DROP_1, DROP_2),
    item_levels=(HEROIC, MYTHIC),
    baseline_source="mythicplus",
    candidate_source="raid",
)

H, M = HEROIC.ilevel, MYTHIC.ilevel
RING_DPS: dict[frozenset[tuple[int, int]], float] = {
    frozenset(): 100_000.0,
    frozenset({(101, H)}): 110_000.0,
    frozenset({(102, H)}): 112_000.0,
    frozenset({(103, H)}): 105_000.0,
    frozenset({(101, H), (102, H)}): 118_000.0,
    frozenset({(101, H), (103, H)}): 122_000.0,
    frozenset({(102, H), (103, H)}): 120_000.0,
    # Heroic drops: none of them beats the farmed pair, so the ceiling there is what
    # the character already wears -- an answer, not a gap.
    frozenset({(101, H), (201, H)}): 121_500.0,
    frozenset({(102, H), (201, H)}): 121_000.0,
    frozenset({(103, H), (201, H)}): 120_000.0,
    frozenset({(101, H), (202, H)}): 119_000.0,
    frozenset({(102, H), (202, H)}): 118_000.0,
    frozenset({(103, H), (202, H)}): 117_000.0,
    frozenset({(201, H), (202, H)}): 116_000.0,
    # Mythic drops: the best set pairs the drop with B, which the baseline never named.
    frozenset({(101, H), (201, M)}): 130_000.0,
    frozenset({(102, H), (201, M)}): 135_000.0,
    frozenset({(103, H), (201, M)}): 128_000.0,
    frozenset({(101, H), (202, M)}): 125_000.0,
    frozenset({(102, H), (202, M)}): 126_000.0,
    frozenset({(103, H), (202, M)}): 124_000.0,
    frozenset({(201, M), (202, M)}): 127_000.0,
}


@pytest.fixture
def ring_sweep(monkeypatch, tmp_path):
    """``_sweep_one`` with simc replaced by a lookup on what each variant wears.

    Keyed on the equipped items rather than on the variant name: a test that spelled
    out profileset keys would pass while the sweep equipped something else entirely,
    which is the failure mode a one-socket run already shipped once.
    """

    def fake_run(simc, profile, targets, settings, variants, timeout):
        out = {}
        for variant in variants:
            worn = frozenset(
                (item.item_id, ilevel)
                for _socket, item, ilevel in variant.equipped.sockets
                if item is not None
            )
            if worn in RING_DPS:
                out[variant.key] = gearsweep.VariantResult(
                    key=variant.key, dps=RING_DPS[worn], dps_error=0.05, iterations=1000
                )
        return out

    monkeypatch.setattr(gearsweep, "_run", fake_run)

    path = tmp_path / "MID2_Mage_Arcane_Sunfury.simc"
    path.write_text(
        "# gear_intellect=2751\nfinger1=,id=1,ilevel=334\nfinger2=,id=2,ilevel=334\n",
        encoding="utf-8",
    )
    return gearsweep._sweep_one(
        simc=Path("simc"),
        profile=spec_profile(path),
        pool=RING_POOL,
        settings=SimSettings(target_error=0, max_iterations=1000),
        targets=1,
        primary="intellect",
        baseline_pool=[RING_A, RING_B, RING_C],
        candidates=[DROP_1, DROP_2],
        baseline_level=HEROIC,
        timeout=600,
    )


def test_the_baseline_is_the_measured_pair_not_the_two_best_singles(ring_sweep):
    """The defect this replaced, stated as the assertion that catches it.

    B is the best ring measured alone and A the second, so ranking singles picks
    B + A -- which is the *worst* of the three pairs here. Only running the pairs
    finds A + C. If this ever goes back to sorting standalone values, this fails.
    """
    assert [item.item_id for item in ring_sweep.baseline] == [101, 103]
    assert ring_sweep.baseline_method == gearsweep.EXHAUSTIVE
    assert ring_sweep.baseline_combinations == 3
    assert ring_sweep.baseline_dps == 122_000.0

    # And the runner-up pair is published, because two pairs a tenth of a percent
    # apart look like a settled answer from the per-item numbers alone.
    assert ring_sweep.baseline_runner_up is not None
    assert ring_sweep.baseline_runner_up.item_ids == (102, 103)


def test_the_ceiling_names_a_set_the_two_step_search_cannot_reach(ring_sweep):
    """Best Mythic+ pair, then one drop into it, is a two-step search over a space
    where the optimum is not reachable in two steps. B is not in the baseline pair,
    so no swap into {A, C} produces B + R1 -- the best set here by 3.8%."""
    mythic = {entry.level.id: entry for entry in ring_sweep.best_sets}["mythic"]

    assert mythic.worn.item_ids == (102, 201)
    assert mythic.worn.dps == 135_000.0
    assert mythic.is_baseline is False
    assert mythic.gain == pytest.approx(135_000.0 / 122_000.0 - 1)
    assert mythic.is_tie is False

    # The two-step answer, for comparison: the best single drop into the baseline is
    # A + R1, and it is worth 3.8% less than the set nobody would have proposed.
    best_candidate = ring_sweep.candidates[0]
    assert (best_candidate.item.item_id, best_candidate.item_level.id) == (201, "mythic")
    assert best_candidate.dps == 130_000.0
    assert mythic.worn.dps / best_candidate.dps - 1 == pytest.approx(0.0385, abs=1e-4)


def test_the_ceiling_can_be_what_the_character_already_wears(ring_sweep):
    """A real answer -- "no drop at this item level belongs in this slot" -- and it
    has to be distinguishable from the ceiling not having been measured."""
    heroic = {entry.level.id: entry for entry in ring_sweep.best_sets}["heroic"]

    assert heroic.worn.item_ids == (101, 103)
    assert heroic.is_baseline is True
    assert heroic.gain == 0.0
    assert heroic.to_json()["isBaseline"] is True
    # The ceiling's reference and the candidates' reference are the same gear run in
    # two different invocations, which is only sound because profilesets with
    # identical gear repeat exactly. Pinned here so the two halves of the file are
    # known to be on one scale.
    assert heroic.baseline.dps == ring_sweep.baseline_dps


def test_the_ceiling_is_published_per_item_level(ring_sweep):
    assert [entry.level.id for entry in ring_sweep.best_sets] == ["heroic", "mythic"]
    payload = ring_sweep.to_json()
    assert [entry["level"] for entry in payload["bestSets"]] == ["heroic", "mythic"]
    assert payload["baseline"]["method"] == "exhaustive"
    assert payload["baseline"]["combinations"] == 3


def test_the_per_item_comparison_still_answers_should_i_take_this_drop(ring_sweep):
    """The ceiling is an addition, not a replacement. A reader still has to be able
    to price one drop against what is worn today, which is a different question from
    what the slot should eventually hold."""
    assert {entry.replaces.item_id for entry in ring_sweep.candidates} == {103}
    assert len(ring_sweep.candidates) == 4  # two drops at two item levels
    for entry in ring_sweep.candidates:
        assert entry.gain == pytest.approx(entry.dps / 122_000.0 - 1)


# --------------------------------------------------------------------------------
# The tie rule, applied to sets rather than to items
# --------------------------------------------------------------------------------


def worn_set(dps: float, error: float = 0.05, ids=(101, 103)) -> gearsweep.WornSet:
    return gearsweep.WornSet(
        offers=tuple(gearsweep.Offer(gear(i, "mythicplus"), HEROIC) for i in ids),
        dps=dps,
        dps_error=error,
    )


@pytest.mark.parametrize(
    ("winner", "runner", "tie"),
    [
        (122_000.0, 120_000.0, False),  # 1.7%, far outside the noise
        (122_000.0, 121_950.0, True),  # 0.04%, inside hypot(0.05, 0.05)/100
    ],
)
def test_a_winning_set_inside_the_noise_reads_as_a_tie(winner, runner, tie):
    """The project's uncertainty convention, applied where it was missing: the
    published pool entries are per item, so a pair that beat its runner-up by a
    tenth of a percent looked like a settled answer from outside."""
    payload = gearsweep._runner_up_json(worn_set(winner), worn_set(runner))
    assert payload is not None
    assert payload["tie"] is tie


def test_the_tie_band_sits_at_the_two_errors_in_quadrature():
    """Not at a fixed percentage. The whole point of the project's rule is that the
    band tracks the precision the run achieved, so it has to be *this* number --
    written from ``hypot`` rather than from a figure somebody typed, and pinned from
    both sides so a band twice or half as wide fails."""
    floor = math.hypot(0.05, 0.05) / 100
    inside = gearsweep._runner_up_json(
        worn_set(122_000.0), worn_set(122_000.0 / (1 + floor * 0.99))
    )
    outside = gearsweep._runner_up_json(
        worn_set(122_000.0), worn_set(122_000.0 / (1 + floor * 1.01))
    )
    assert inside is not None and outside is not None
    assert inside["tie"] is True
    assert outside["tie"] is False


def test_a_set_with_nothing_behind_it_publishes_no_runner_up():
    assert gearsweep._runner_up_json(worn_set(122_000.0), None) is None


@pytest.mark.parametrize(
    ("ceiling", "tie"),
    [(130_000.0, False), (122_060.0, True)],
)
def test_a_ceiling_inside_the_noise_is_not_an_upgrade_over_the_baseline(ceiling, tie):
    best = gearsweep.BestSet(
        level=MYTHIC,
        worn=worn_set(ceiling, ids=(102, 201)),
        runner_up=None,
        baseline=worn_set(122_000.0),
    )
    assert best.is_tie is tie
    assert best.is_baseline is False


def test_a_ceiling_that_was_never_measured_is_absent_rather_than_equal(stub_simc, profile):
    """ "The baseline is the ceiling" and "nobody measured the ceiling" are different
    claims, and publishing the first for the second would assert that no drop
    improves on what is worn -- which nothing ran."""
    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1], timeout=60)
    assert result.targets[0].best_sets == []
    assert "bestSets" not in result.targets[0].to_json()
