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


# --------------------------------------------------------------------------------
# Per-slot provenance (#95): a document's slots need not come from one run
# --------------------------------------------------------------------------------


def measured_result(
    hero: str, dps_error: float, slot: EquipmentSlot = TRINKET
) -> gearsweep.SpecSlotResult:
    """A minimal measured result whose rows carry a real error field."""
    profile = SpecProfile(
        path=Path("/dev/null"),
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent=hero,
        role="spell",
        talent_hash=None,
        # `SpecProfile.id` is built from `name_hero`, not `hero_talent` -- two
        # results without it would collapse to one id and the coverage count.
        name_hero=hero,
    )
    return gearsweep.SpecSlotResult(
        profile=profile,
        slot=slot,
        primary_stat="intellect",
        targets=[
            gearsweep.TargetResult(
                targets=1,
                empty_dps=1.0,
                baseline=[MPLUS[0], MPLUS[1]],
                baseline_ilevel=334,
                baseline_dps=100.0,
                baseline_dps_error=dps_error,
            )
        ],
    )


def test_write_gear_stamps_provenance_on_the_swept_slots_only(tmp_path):
    """This run's simc and precision describe only the slots this run measured.

    Stamping them on every pool would claim this run's provenance for the numbers
    `merge_gear_shards` folds in from the published document -- the document-level
    version of exactly that is what issue #95 measured.

    Two slots are swept with different populations and errors on purpose: with a
    single swept slot, per-slot and document-wide figures coincide, and a writer
    stamping the document's numbers on every swept slot -- the #95 defect
    re-created one level down -- passes undetected. Verified by mutation before
    the second slot existed.
    """
    import json

    from wowdps import dataset
    from wowdps.equipment import FINGER, NECK

    def pool_for(slot):
        return SlotPool(
            tier="MID2",
            slot=slot,
            items=(MPLUS[0],),
            item_levels=(HEROIC,),
            baseline_source="mythicplus",
            candidate_source="raid",
        )

    path = dataset.write_gear(
        tmp_path,
        [
            measured_result("Sunfury", 0.05),
            measured_result("Spellslinger", 0.07),
            measured_result("Frostfire", 0.2, slot=NECK),
        ],
        {"trinket": POOL, "neck": pool_for(NECK), "finger": pool_for(FINGER)},
        "MID2",
        {"gitRevision": "OLDREV"},
        SimSettings(),
        builds_available=[f"build_{n}" for n in range(26)],
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    by_slot = {slot["id"]: slot for slot in document["slots"]}

    assert by_slot["trinket"]["coverage"] == {"specs": 2, "specsAvailable": 26}
    assert by_slot["trinket"]["simc"] == {"gitRevision": "OLDREV"}
    assert by_slot["trinket"]["settings"]["medianDpsError"] == 0.06
    assert by_slot["neck"]["coverage"] == {"specs": 1, "specsAvailable": 26}
    assert by_slot["neck"]["settings"]["medianDpsError"] == 0.2
    assert "coverage" not in by_slot["finger"]
    assert "simc" not in by_slot["finger"]
    assert "settings" not in by_slot["finger"]


def _provenanced_slot(
    slot_id: str, revision: str, rows: list[dict], error: float, available: int = 26
) -> dict:
    return {
        "id": slot_id,
        "label": slot_id.title(),
        "specs": rows,
        "coverage": {"specs": len(rows), "specsAvailable": available},
        "simc": {"gitRevision": revision},
        "settings": {
            "targetError": 0,
            "maxIterations": 3000,
            "deterministic": True,
            "medianDpsError": error,
        },
    }


def _row(spec_id: str, error: float) -> dict:
    return {
        "id": spec_id,
        "targets": [{"targets": 1, "baseline": {"dpsError": error}, "pool": [], "candidates": []}],
    }


def test_a_single_slot_rerun_keeps_each_slots_own_provenance(tmp_path):
    """The #95 scenario, run through the real merge.

    A trinket-only re-run at a new simc revision must not relabel the finger
    numbers as its own: each slot keeps the provenance of the run that measured
    it, a slot whose union holds rows from two runs gets its count recounted and
    its precision recomputed from the rows themselves, and the document-level
    blocks describe the document rather than whichever run sorted newest.

    Two deliberately awkward numbers in the fixture, both mutation-tested:

    * finger's stamped median is 0.1234 while its rows derive 0.125 -- the merge
      must not re-derive an untouched slot's figure, because the published
      errors are rounded and a re-derivation can move the last digit of a
      number the run never re-measured;
    * the document-level median over every row (0.1, 0.15, 0.2, 0.4 -> 0.175)
      differs from any composition of the slot medians (0.2117/0.2125), so an
      implementation that composes instead of walking the rows fails here.
    """
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-21T00:00:00+00:00",
                "simc": {"gitRevision": "OLDREV"},
                "settings": {
                    "targetError": 0,
                    "maxIterations": 3000,
                    "deterministic": True,
                    "medianDpsError": 0.15,
                },
                "coverage": {"specs": 2, "specsAvailable": 26},
                "slots": [
                    _provenanced_slot(
                        "finger", "OLDREV", [_row("a", 0.1), _row("b", 0.15)], 0.1234
                    ),
                    _provenanced_slot("trinket", "OLDREV", [_row("a", 0.2)], 0.2),
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
                "generatedAt": "2026-08-26T00:00:00+00:00",
                "simc": {"gitRevision": "NEWREV"},
                "settings": {
                    "targetError": 0,
                    "maxIterations": 3000,
                    "deterministic": True,
                    "medianDpsError": 0.4,
                },
                "coverage": {"specs": 1, "specsAvailable": 52},
                "slots": [
                    # The empty placeholder write_gear emits for a pool this run
                    # did not sweep -- deliberately without provenance blocks.
                    {"id": "finger", "label": "Finger", "specs": []},
                    _provenanced_slot("trinket", "NEWREV", [_row("b", 0.4)], 0.4, available=52),
                ],
            }
        ),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    by_slot = {slot["id"]: slot for slot in merged["slots"]}

    # Finger was not re-swept: numbers and provenance both stay the old run's,
    # the stamped-but-not-row-derived median included.
    assert by_slot["finger"]["simc"] == {"gitRevision": "OLDREV"}
    assert by_slot["finger"]["settings"]["medianDpsError"] == 0.1234
    assert by_slot["finger"]["coverage"] == {"specs": 2, "specsAvailable": 26}
    # Trinket was re-swept at NEWREV and its union holds rows from both runs.
    assert by_slot["trinket"]["simc"] == {"gitRevision": "NEWREV"}
    assert by_slot["trinket"]["coverage"] == {"specs": 2, "specsAvailable": 52}
    assert by_slot["trinket"]["settings"]["medianDpsError"] == pytest.approx(0.3)
    # Document level: the union stays the union, and the precision describes
    # every row the document holds.
    assert merged["coverage"] == {"specs": 2, "specsAvailable": 52}
    assert merged["settings"]["medianDpsError"] == pytest.approx(0.175)
    assert merged["simc"] == {"gitRevision": "NEWREV"}


def test_an_old_artifact_republished_later_cannot_regress_the_newer_rows(tmp_path):
    """Re-running an old workflow run's publish job merges a stale shard artifact
    over a newer published document -- one click, artifacts live ~90 days. The
    published document joins the merge at its own timestamp, so the newer rows,
    their provenance and `generatedAt` all survive; forcing it oldest (the
    pre-#95 arrangement) replaced them with the stale run's."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-28T00:00:00+00:00",
                "simc": {"gitRevision": "NEWPUB"},
                "coverage": {"specs": 1, "specsAvailable": 52},
                "slots": [_provenanced_slot("trinket", "NEWPUB", [_row("a", 0.3)], 0.3)],
            }
        ),
        encoding="utf-8",
    )
    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-20T00:00:00+00:00",
                "simc": {"gitRevision": "OLDRUN"},
                "coverage": {"specs": 1, "specsAvailable": 26},
                "slots": [_provenanced_slot("trinket", "OLDRUN", [_row("a", 0.1)], 0.1)],
            }
        ),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))

    assert merged["generatedAt"] == "2026-08-28T00:00:00+00:00"
    assert merged["simc"] == {"gitRevision": "NEWPUB"}
    slot = merged["slots"][0]
    assert slot["simc"] == {"gitRevision": "NEWPUB"}
    assert slot["specs"][0]["targets"][0]["baseline"]["dpsError"] == 0.3


def test_a_first_publish_tolerates_a_placeholder_before_the_rows(tmp_path):
    """On a tier's first publish there is no published document, so the oldest
    document carrying a slot can be the empty placeholder itself -- a shard
    whose specs all failed for one slot of a two-slot dispatch. The base
    selection has to survive `previous is None` with empty rows; collapsing the
    condition to `slot if incoming else previous` crashes exactly here."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    shard_a = tmp_path / "shard-a"
    shard_a.mkdir()
    (shard_a / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-26T00:00:00+00:00",
                "slots": [{"id": "neck", "label": "Neck", "specs": []}],
            }
        ),
        encoding="utf-8",
    )
    shard_b = tmp_path / "shard-b"
    shard_b.mkdir()
    (shard_b / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-26T01:00:00+00:00",
                "slots": [_provenanced_slot("neck", "NEWREV", [_row("a", 0.2)], 0.2)],
            }
        ),
        encoding="utf-8",
    )

    merge_gear_shards([shard_a, shard_b], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    slot = merged["slots"][0]
    assert [spec["id"] for spec in slot["specs"]] == ["a"]
    assert slot["simc"] == {"gitRevision": "NEWREV"}


def test_a_pre_fix_document_gains_no_invented_slot_provenance(tmp_path):
    """A slot last measured before the per-slot blocks existed keeps none.

    The published document cannot say which run measured which of its slots, so
    attaching the document-level blocks to a slot would manufacture provenance.
    Absent stays absent, and the reader falls back to the document level exactly
    as it did before the blocks existed.
    """
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-17T00:00:00+00:00",
                "simc": {"gitRevision": "OLDREV"},
                "coverage": {"specs": 1, "specsAvailable": 26},
                "slots": [{"id": "trinket", "label": "Trinket", "specs": [_row("a", 0.2)]}],
            }
        ),
        encoding="utf-8",
    )
    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-26T00:00:00+00:00",
                "simc": {"gitRevision": "NEWREV"},
                "coverage": {"specs": 1, "specsAvailable": 52},
                "slots": [
                    {"id": "trinket", "label": "Trinket", "specs": []},
                    _provenanced_slot("neck", "NEWREV", [_row("a", 0.3)], 0.3),
                ],
            }
        ),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    by_slot = {slot["id"]: slot for slot in merged["slots"]}

    assert "simc" not in by_slot["trinket"]
    assert "settings" not in by_slot["trinket"]
    assert "coverage" not in by_slot["trinket"]
    assert by_slot["neck"]["simc"] == {"gitRevision": "NEWREV"}


def test_the_json_error_walk_agrees_with_the_dataclass_walk():
    """`_median_gear_error_json` and `_median_gear_error` are deliberately two
    walks over two shapes of the same data; this is the pin that keeps them from
    drifting apart when either shape changes. The zero-error pool entry is in
    the fixture on purpose: both walks must drop it (a zero error is a variant
    that never measured, not perfect precision), and without one the `> 0`
    filter is dead fixture-side and can be deleted from either walk unnoticed."""
    from wowdps.dataset import _median_gear_error, _median_gear_error_json

    result = gearsweep.SpecSlotResult(
        profile=PROFILE,
        slot=TRINKET,
        primary_stat="intellect",
        targets=[
            gearsweep.TargetResult(
                targets=1,
                empty_dps=1.0,
                baseline=[MPLUS[0], MPLUS[1]],
                baseline_ilevel=334,
                baseline_dps=100.0,
                baseline_dps_error=0.05,
                pool=[
                    gearsweep.PoolEntry(
                        item=MPLUS[0], ilevel=334, dps=90.0, dps_error=0.07, standalone_gain=1.0
                    ),
                    gearsweep.PoolEntry(
                        item=MPLUS[2], ilevel=334, dps=0.0, dps_error=0.0, standalone_gain=0.0
                    ),
                ],
                candidates=[
                    gearsweep.CandidateResult(
                        item=RAID[0],
                        item_level=MYTHIC,
                        replaces=MPLUS[1],
                        dps=101.0,
                        dps_error=0.09,
                        priority_dps=None,
                        gain=0.01,
                        gain_error=0.001,
                    )
                ],
            )
        ],
    )
    row = result.to_json()
    assert _median_gear_error([result]) == _median_gear_error_json([row]) == 0.07

    # The JSON walk reads baseline, pool and candidates and nothing else: a
    # `bestSets` block carries its own error fields, and one walk starting to
    # read them while the other does not is exactly the drift this test pins.
    row["targets"][0]["bestSets"] = [{"dpsError": 99.0}]
    assert _median_gear_error_json([row]) == 0.07


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


# --------------------------------------------------------------------------------
# Which item the candidate replaces
# --------------------------------------------------------------------------------
#
# Pool-file order and measured value are different orderings, and for the whole life
# of the exhaustive method the sweep used the first where it documented the second.
# `baseline_items` comes out of `itertools.combinations(worn, ...)`, which follows
# `gear_pools.json`; under the additive rule `entries` was already sorted by
# standalone value, so `[-1]` happened to be the weakest and nothing showed. Under
# the exhaustive rule it is whichever member of the winning pair sorts later in the
# file, and on MID2's published finger sweep that was the *stronger* ring on 10 of 26
# builds.
#
# The table below is built so the two orderings disagree: X is first in the pool and
# weakest, Y is second and strongest, and X + Y is the pair that wins.

RING_X, RING_Y, RING_Z = (gear(301, "mythicplus"), gear(302, "mythicplus"), gear(303, "mythicplus"))
DROP_D = gear(401, "raid")

ORDER_POOL = SlotPool(
    tier="MID2",
    slot=EquipmentSlot(
        id="finger", label="Ring", sockets=("finger1", "finger2"), inventory_type=11
    ),
    items=(RING_X, RING_Y, RING_Z, DROP_D),
    item_levels=(MYTHIC,),
    baseline_source="mythicplus",
    candidate_source="raid",
)

Y = MYTHIC.ilevel
ORDER_DPS: dict[frozenset[tuple[int, int]], float] = {
    frozenset(): 100_000.0,
    # Standalone: X is the weakest of the three and Y the strongest.
    frozenset({(301, Y)}): 105_000.0,
    frozenset({(302, Y)}): 120_000.0,
    frozenset({(303, Y)}): 110_000.0,
    # ...and yet X + Y is the best farmed pair, so the winning set holds both the
    # weakest item and the strongest.
    frozenset({(301, Y), (302, Y)}): 130_000.0,
    frozenset({(301, Y), (303, Y)}): 118_000.0,
    frozenset({(302, Y), (303, Y)}): 125_000.0,
    # The drop beside each farmed ring. Dropping X for it is worth far more than
    # dropping Y for it, which is the whole point of getting the choice right.
    frozenset({(301, Y), (401, Y)}): 121_000.0,
    frozenset({(302, Y), (401, Y)}): 133_000.0,
    frozenset({(303, Y), (401, Y)}): 128_000.0,
}


@pytest.fixture
def order_sweep(monkeypatch, tmp_path):
    def fake_run(simc, profile, targets, settings, variants, timeout):
        out = {}
        for variant in variants:
            worn = frozenset(
                (item.item_id, ilevel)
                for _socket, item, ilevel in variant.equipped.sockets
                if item is not None
            )
            if worn in ORDER_DPS:
                out[variant.key] = gearsweep.VariantResult(
                    key=variant.key, dps=ORDER_DPS[worn], dps_error=0.05, iterations=1000
                )
        return out

    monkeypatch.setattr(gearsweep, "_run", fake_run)
    path = tmp_path / "MID2_Mage_Arcane_Sunfury.simc"
    path.write_text("# gear_intellect=2751\n", encoding="utf-8")
    return gearsweep._sweep_one(
        simc=Path("simc"),
        profile=spec_profile(path),
        pool=ORDER_POOL,
        settings=SimSettings(target_error=0, max_iterations=1000),
        targets=1,
        primary="intellect",
        baseline_pool=[RING_X, RING_Y, RING_Z],
        candidates=[DROP_D],
        baseline_level=MYTHIC,
        timeout=600,
    )


def test_the_candidate_replaces_the_measured_weakest_not_the_last_in_the_pool_file(order_sweep):
    """The defect, stated as the assertion that catches it.

    The winning pair is X + Y in pool order. Taking the last of those names Y, which
    is the *strongest* ring the build wears; the weakest is X. Getting this wrong
    prices every drop against throwing the better ring away, so a real upgrade can
    publish as a downgrade.
    """
    assert [item.item_id for item in order_sweep.baseline] == [301, 302]
    assert order_sweep.replaces is not None
    assert order_sweep.replaces.item_id == 301
    assert {entry.replaces.item_id for entry in order_sweep.candidates} == {301}
    assert order_sweep.to_json()["baseline"]["replaces"] == 301


def test_the_candidate_is_equipped_in_the_socket_it_replaces(order_sweep):
    """Naming the right item is half of it; the run has to equip that way too.

    The surviving item keeps the socket it was measured in, so the baseline and the
    candidate differ in exactly one socket. That matters beyond tidiness: a profile's
    two ring sockets carry different gems, so moving the survivor across would change
    two things at once and call the difference the drop's.
    """
    [candidate] = order_sweep.candidates
    # 133,000 is the drop *beside Y*, i.e. X swapped out. 121,000 would be Y
    # swapped out -- the old answer, and a 9.9% different number.
    assert candidate.dps == 133_000.0
    assert candidate.gain == pytest.approx(133_000.0 / 130_000.0 - 1)


def test_replacing_the_stronger_item_would_publish_an_upgrade_as_a_downgrade(order_sweep):
    """Why this is severe rather than untidy.

    Against the correct baseline the drop gains 2.3%. Priced against dropping Y
    instead it *loses* 6.9% -- so the same drop, the same build and the same run
    publish opposite advice depending on which item the sweep decided to throw away.
    """
    [candidate] = order_sweep.candidates
    wrong_way = 121_000.0 / 130_000.0 - 1
    assert candidate.gain > 0
    assert wrong_way < 0


def test_a_duplicated_pool_id_never_becomes_a_one_ring_baseline():
    """`gearpool.py` builds the pool from the journal's loot tables, and an item two
    encounters both drop is an ordinary thing there. Repeated in the farmed half it
    would be paired with itself, `_pair_variant` would emit the same id for both
    sockets, and simc answers that by leaving one empty -- a plausible number for a
    set nobody can wear, which could then win and be published as "Standard".

    The guard was on the drop-bearing loop only, where the disjoint sources and the
    one-level-at-a-time enumeration already made it unreachable. It was missing from
    the loop where a duplicate can actually arrive.
    """
    from wowdps.gearsweep import _combination_variants

    duplicated = gear(101, "mythicplus")
    worn = offers([duplicated, duplicated, gear(102, "mythicplus")], HEROIC)

    combos = _combination_variants(FINGER_SLOT, worn, [])

    for combo in combos.values():
        assert len({offer.item.item_id for offer in combo}) == len(combo)
    # 101+101 is gone; 101+102 survives once rather than twice, because two identical
    # offers produce the same variant key.
    assert len(combos) == 1


# --------------------------------------------------------------------------------
# Two invocations, two references
# --------------------------------------------------------------------------------


def test_the_runner_up_gap_is_taken_inside_one_invocation(ring_sweep):
    """Both sides of a gap have to come from the same run.

    The baseline's runner-up used to be compared against `baseline_dps`, which the
    *candidate* invocation measured, while the runner-up itself came from the
    *combination* invocation. Under `--target-error` the two converge separately, so
    the gap was part real and part the difference between two runs -- and it could
    come out negative, at which point `abs(gap)` still called it a lead and the view
    printed the figure with a minus in front of a minus.
    """
    payload = ring_sweep.to_json()["baseline"]["runnerUp"]
    assert ring_sweep.baseline_set is not None
    assert payload["gap"] >= 0
    assert payload["gap"] == pytest.approx(
        ring_sweep.baseline_set.dps / ring_sweep.baseline_runner_up.dps - 1, abs=1e-5
    )


def test_a_gap_needs_two_whole_sets_and_refuses_a_missing_one():
    """The helper used to take the winner as loose floats, which is what let a
    fabricated set with no items into it. Passing nothing is now an absent gap."""
    assert gearsweep._runner_up_json(None, worn_set(120_000.0)) is None
    assert gearsweep._runner_up_json(worn_set(122_000.0), None) is None


def test_the_ceiling_publishes_the_denominator_it_used(ring_sweep):
    """`gain` is this set over the *combination* run's baseline, and `baseline.dps`
    is the *candidate* run's. A consumer dividing the two published numbers gets a
    third answer, so the reference is published beside the gain rather than implied."""
    payload = ring_sweep.to_json()
    for entry in payload["bestSets"]:
        assert entry["baselineDps"] == pytest.approx(ring_sweep.baseline_set.dps, abs=0.1)
        assert entry["gain"] == pytest.approx(entry["dps"] / entry["baselineDps"] - 1, abs=1e-5)


def test_the_drift_between_the_two_baseline_runs_is_published(ring_sweep):
    """Deterministic runs make it exactly zero -- profilesets with identical gear
    repeat bit-identically -- and that is the point: a reader can see it is zero
    rather than being asked to assume it. Under `--target-error` it is not, and the
    ceiling and candidate columns on one page are then that far apart."""
    payload = ring_sweep.to_json()["baseline"]
    assert payload["drift"] == 0.0
    assert payload["driftError"] > 0


@pytest.fixture
def drifting_sweep(monkeypatch, tmp_path):
    """The same sweep with the two invocations disagreeing about identical gear.

    Deterministic runs make that impossible -- profilesets with identical gear repeat
    bit-identically -- so every other fixture here returns one number per gear set and
    cannot see a cross-invocation mistake at all. Under ``--target-error`` the two
    invocations converge separately and this is the real shape. 2% is far larger than
    a real run would drift; it is chosen so the consequence is unambiguous rather
    than plausible.
    """
    calls: list[int] = []

    def fake_run(simc, profile, targets, settings, variants, timeout):
        calls.append(1)
        scale = 1.0 if len(calls) == 1 else 0.98
        out = {}
        for variant in variants:
            worn = frozenset(
                (item.item_id, ilevel)
                for _socket, item, ilevel in variant.equipped.sockets
                if item is not None
            )
            if worn in RING_DPS:
                out[variant.key] = gearsweep.VariantResult(
                    key=variant.key,
                    dps=RING_DPS[worn] * scale,
                    dps_error=0.05,
                    iterations=1000,
                )
        return out

    monkeypatch.setattr(gearsweep, "_run", fake_run)
    path = tmp_path / "MID2_Mage_Arcane_Sunfury.simc"
    path.write_text("# gear_intellect=2751\n", encoding="utf-8")
    return gearsweep._sweep_one(
        simc=Path("simc"),
        profile=spec_profile(path),
        pool=RING_POOL,
        settings=SimSettings(target_error=0.3),
        targets=1,
        primary="intellect",
        baseline_pool=[RING_A, RING_B, RING_C],
        candidates=[DROP_1, DROP_2],
        baseline_level=HEROIC,
        timeout=600,
    )


def test_a_drifting_baseline_never_produces_a_negative_runner_up_gap(drifting_sweep):
    """The visible half of the cross-invocation bug.

    The winning pair measures 122,000 in the combination run and its runner-up
    120,000, so the lead is 1.7%. Take the winner from the *candidate* run instead --
    119,560 after the drift -- and the runner-up is suddenly ahead: `gap` goes
    negative, `abs(gap)` still calls it outside the band, and the view renders it as a
    minus in front of a minus. Both sides now come from the combination run, so the
    sign cannot invert.
    """
    payload = drifting_sweep.to_json()["baseline"]
    assert payload["runnerUp"]["gap"] == pytest.approx(122_000.0 / 120_000.0 - 1, abs=1e-5)
    assert payload["runnerUp"]["gap"] > 0
    assert payload["runnerUp"]["tie"] is False


def test_a_drifting_baseline_is_published_rather_than_only_logged(drifting_sweep):
    """The invisible half. The ceiling gains and the candidate gains on one page are
    then measured from references 2% apart, and until this was published nothing in
    the file said so -- the guard only wrote a line to a log nobody reads later."""
    payload = drifting_sweep.to_json()["baseline"]
    assert payload["drift"] == pytest.approx(-0.02, abs=1e-4)
    assert abs(payload["drift"]) > payload["driftError"]


def test_the_published_pool_answers_both_rankings_not_only_the_winning_one():
    """The rows carry two columns -- worth in company, worth alone -- and used to be
    cut by one ranking. On MID2's real finger sweep the two heads disagree on four
    builds of six, and on one neither best-alone item survived the cut, so a column
    labelled "alone" listed items chosen by a different figure."""
    from wowdps.gearsweep import _published_pool

    def entry(item_id: int, alone: float, in_company: float) -> gearsweep.PoolEntry:
        return gearsweep.PoolEntry(
            item=gear(item_id, "mythicplus"),
            ilevel=334,
            dps=100_000 + alone,
            dps_error=0.05,
            standalone_gain=alone,
            best_combination_dps=in_company,
        )

    # Ranked by company (the order `entries` arrives in); 104 is the best alone and
    # the worst in company, so a cut at two would drop it entirely.
    entries = [
        entry(101, alone=5_000, in_company=130_000),
        entry(102, alone=4_000, in_company=129_000),
        entry(103, alone=3_000, in_company=128_000),
        entry(104, alone=9_000, in_company=110_000),
    ]

    kept = [e.item.item_id for e in _published_pool(entries, 2)]
    assert kept == [101, 102, 104]  # both heads, in the ranking's own order


# --------------------------------------------------------------------------------
# When the dataset reaches disk
# --------------------------------------------------------------------------------


def test_the_sweep_publishes_after_every_spec_and_not_once_more_at_the_end(monkeypatch, tmp_path):
    """Two properties in one count, and both were wrong in opposite directions.

    `write_gear` used to be called once, after the loop, so a sweep interrupted at
    spec 9 of 26 left nothing -- while CLAUDE.md described it leaving a smaller,
    honestly-counted dataset, which is what the view's coverage line reports. Moving
    it into the loop then left a redundant trailing call that reserialised an
    unchanged document and restamped `generatedAt`.

    So: one write per spec, and no more. Three specs, three writes.
    """
    from wowdps import cli, dataset, equipment, profiles, simc_runner
    from wowdps import gearsweep as sweep

    written: list[int] = []

    def fake_write_gear(out_dir, results, pools, tier, simc_meta, settings, builds_available):
        written.append(len(results))
        path = Path(out_dir) / "gear.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def fake_discover(profiles_dir, tier, dps_only=True):
        return [
            SpecProfile(
                path=tmp_path / f"MID2_Mage_Arcane_{name}.simc",
                tier="MID2",
                wow_class="Mage",
                spec="Arcane",
                hero_talent=name,
                role="spell",
                talent_hash=None,
            )
            for name in ("Sunfury", "Spellslinger", "Frostfire")
        ]

    monkeypatch.setattr(dataset, "write_gear", fake_write_gear)
    monkeypatch.setattr(profiles, "discover", fake_discover)
    monkeypatch.setattr(simc_runner, "find_simc", lambda *a, **k: Path("simc"))
    monkeypatch.setattr(cli, "_resolve_tier", lambda *a, **k: "MID2")
    monkeypatch.setattr(cli, "_probe_simc_metadata", lambda *a, **k: {"version": "stub"})
    monkeypatch.setattr(
        equipment,
        "load_pools",
        lambda *a, **k: equipment.GearPools(tier="MID2", slots={"finger": POOL}),
    )
    monkeypatch.setattr(
        sweep,
        "sweep_spec",
        lambda *a, **k: sweep.SpecSlotResult(
            profile=fake_discover(None, None)[0],
            slot=POOL.slot,
            primary_stat="intellect",
            targets=[
                sweep.TargetResult(
                    targets=1,
                    empty_dps=1.0,
                    baseline=[MPLUS[0]],
                    baseline_ilevel=334,
                    baseline_dps=1.0,
                    baseline_dps_error=0.05,
                )
            ],
        ),
    )

    args = cli.build_parser().parse_args(
        ["gear", "--profiles", str(tmp_path), "--tier", "MID2", "--out", str(tmp_path / "out")]
    )
    assert cli.cmd_gear(args) == 0

    # One write per spec, each carrying everything swept so far -- never a final
    # duplicate of the last one.
    assert written == [1, 2, 3]


# --------------------------------------------------------------------------------
# A profile the sweep cannot read costs its row, never the shard
# --------------------------------------------------------------------------------


def test_a_profile_without_a_gear_summary_costs_its_row_and_not_the_sweep(tmp_path):
    """The 2026-08-30 shard deaths, pinned at the layer that raised.

    Six materialised MID2 profiles carry no ``# gear_<stat>=`` summary line, so
    ``primary_stat`` raised and the ValueError escaped ``sweep_spec`` -- it sat
    before the per-target try -- and killed every shard of gear runs #8 and #9 at
    its first such profile. The sweep published 29 of 52 builds and reported exit 1.

    The contract is the one CLAUDE.md already states for buffs: a refused profile
    costs a row's numbers and not a shard. Reverting the ``primary_stat`` guard in
    ``sweep_spec`` turns this red with the original ValueError.

    Since #118 the profile's silence is no longer the end of it -- simc is asked to
    regenerate the block (the test below) -- so what this pins is the case where
    *both* routes fail. There is no simc binary at ``Path("simc")`` here, which is
    the honest version of that: the derivation raises, and the error names both
    halves so a reader can tell "the profile says nothing" from "and simc would not
    say either".
    """
    path = tmp_path / "MID2_Evoker_Devastation_FS.simc"
    # A materialised profile's shape: player line and options, no gear summary block.
    path.write_text("evoker=MID2_Evoker_Devastation_FS\nspec=devastation\n", encoding="utf-8")
    profile = SpecProfile(
        path=path,
        tier="MID2",
        wow_class="Evoker",
        spec="Devastation",
        hero_talent="Flameshaper",
        role="spell",
        talent_hash=None,
    )

    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1])

    assert result.targets == []
    assert result.errors, "the refusal must be recorded, not silent"
    assert "gear_<stat>" in result.errors[0]
    # 'unknown' is the stat the pool machinery already refuses to match items to
    # (test_unknown_primary_stat_excludes_everything_rather_than_guessing), so even
    # a caller that ignores the early return cannot equip wrong-stat items.
    assert result.primary_stat == "unknown"
    assert "simc could not regenerate it either" in result.errors[0]


def test_a_profile_without_a_gear_summary_is_recovered_by_asking_simc(monkeypatch, tmp_path):
    """The recovery, and the reason it is simc rather than a table.

    Eight MID2 builds -- Balance and Feral Druid, Devastation Evoker and Retribution
    Paladin, both hero builds each -- are materialised from simc's disabled generator
    blocks, which carry no ``# gear_<stat>=`` line. Skipping them costs the sweep 8 of
    51 rows, and the primary stat is not a thing to look up: simc's own ``save=``
    export regenerates the block with the code that wrote every shipped profile's.
    Measured 2026-08-30: 10 of 11 such profiles derive, at 0.112 s each, and
    Retribution correctly reads ``strength`` where a class table keyed on "Paladin"
    would have to know which spec.

    Deleting the ``derive_primary_stat`` fallback turns this red: the row comes back
    with no targets and ``primary_stat == "unknown"``.
    """
    path = tmp_path / "MID2_Paladin_Retribution_Templar.simc"
    path.write_text("paladin=MID2_Paladin_Retribution_Templar\nspec=retribution\n", "utf-8")
    profile = spec_profile(path)

    # A strength pool, because the point of deriving rather than tabulating is that
    # the answer is per *build*: Retribution is the strength spec of a class whose
    # other two profiled specs are not, so a table keyed on "Paladin" cannot answer.
    strength_pool = SlotPool(
        tier="MID2",
        slot=TRINKET,
        items=(
            gear(301, "mythicplus", "strength"),
            gear(302, "mythicplus", "strength"),
            gear(303, "raid", "strength"),
        ),
        item_levels=(HEROIC, MYTHIC),
        baseline_source="mythicplus",
        candidate_source="raid",
    )
    asked: list[Path] = []

    def derive(simc, profile_path, timeout=120):
        asked.append(profile_path)
        return "strength"

    swept_against: list[str] = []

    def fake_sweep_one(simc, profile, pool, settings, count, primary, *rest):
        # Record the stat the sweep was *run with*, not only the one it reports:
        # `pool.candidates(primary)` is what a wrong stat would silently empty, and
        # the two are separate arguments in `sweep_spec`.
        swept_against.append(primary)
        return gearsweep.TargetResult(
            targets=count,
            empty_dps=1.0,
            baseline=[MPLUS[0]],
            baseline_ilevel=334,
            baseline_dps=1.0,
            baseline_dps_error=0.05,
        )

    monkeypatch.setattr(gearsweep, "derive_primary_stat", derive)
    monkeypatch.setattr(gearsweep, "_sweep_one", fake_sweep_one)

    result = gearsweep.sweep_spec(Path("simc"), profile, strength_pool, SimSettings(), [1])

    assert asked == [path], "the profile's own path is what simc is handed"
    assert result.errors == []
    assert result.primary_stat == "strength"
    assert swept_against == ["strength"]


def test_a_profile_that_states_its_own_stat_is_never_sent_to_simc(monkeypatch, profile):
    """The profile is the first authority and simc is the fallback, not the other way.

    Worth pinning even though the derivation agrees: 43 of 51 builds state the block
    themselves, and a fallback promoted to the default would add a simc invocation per
    (build, slot, target) for an answer already on disk -- and would put simc's reading
    of a *shipped* profile ahead of what that profile says, which is a different claim
    from the one #118 makes.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("simc must not be asked about a profile that states its stat")

    monkeypatch.setattr(gearsweep, "derive_primary_stat", refuse)
    monkeypatch.setattr(
        gearsweep,
        "_sweep_one",
        lambda simc, prof, pool, settings, count, primary, *rest: gearsweep.TargetResult(
            targets=count,
            empty_dps=1.0,
            baseline=[MPLUS[0]],
            baseline_ilevel=334,
            baseline_dps=1.0,
            baseline_dps_error=0.05,
        ),
    )

    result = gearsweep.sweep_spec(Path("simc"), profile, POOL, SimSettings(), [1])
    assert result.primary_stat == "intellect"


def test_a_spec_that_raises_costs_its_row_and_the_rest_still_sweep(monkeypatch, tmp_path):
    """The loop-level guard in ``cmd_gear``, for whatever raises next.

    ``primary_stat`` was the fourth instance of "a new call placed beside a guard
    that already existed rather than inside it" in this repository, so the loop over
    (profile, slot) now guards the whole call: profile 2 of 3 raising must cost its
    row alone, with profiles 1 and 3 swept and published. Reverting the try/except
    around ``gearsweep.sweep_spec`` in ``cmd_gear`` turns this red.
    """
    from wowdps import cli, dataset, equipment, profiles, simc_runner
    from wowdps import gearsweep as sweep

    written: list[int] = []

    def fake_write_gear(out_dir, results, pools, tier, simc_meta, settings, builds_available):
        written.append(len(results))
        path = Path(out_dir) / "gear.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    discovered = [
        SpecProfile(
            path=tmp_path / f"MID2_Mage_Arcane_{name}.simc",
            tier="MID2",
            wow_class="Mage",
            spec="Arcane",
            hero_talent=name,
            role="spell",
            talent_hash=None,
        )
        for name in ("Sunfury", "Spellslinger", "Frostfire")
    ]

    def fake_sweep_spec(simc, profile, pool, settings, targets, timeout):
        if profile.hero_talent == "Spellslinger":
            raise ValueError(f"{profile.path.name} has no '# gear_<stat>=' summary line")
        return sweep.SpecSlotResult(
            profile=profile,
            slot=POOL.slot,
            primary_stat="intellect",
            targets=[
                sweep.TargetResult(
                    targets=1,
                    empty_dps=1.0,
                    baseline=[MPLUS[0]],
                    baseline_ilevel=334,
                    baseline_dps=1.0,
                    baseline_dps_error=0.05,
                )
            ],
        )

    monkeypatch.setattr(dataset, "write_gear", fake_write_gear)
    monkeypatch.setattr(profiles, "discover", lambda *a, **k: discovered)
    monkeypatch.setattr(simc_runner, "find_simc", lambda *a, **k: Path("simc"))
    monkeypatch.setattr(cli, "_resolve_tier", lambda *a, **k: "MID2")
    monkeypatch.setattr(cli, "_probe_simc_metadata", lambda *a, **k: {"version": "stub"})
    monkeypatch.setattr(
        equipment,
        "load_pools",
        lambda *a, **k: equipment.GearPools(tier="MID2", slots={"finger": POOL}),
    )
    monkeypatch.setattr(sweep, "sweep_spec", fake_sweep_spec)

    args = cli.build_parser().parse_args(
        ["gear", "--profiles", str(tmp_path), "--tier", "MID2", "--out", str(tmp_path / "out")]
    )
    assert cli.cmd_gear(args) == 0

    # Profile 2 raised: its row is missing, and the sweep carried on to profile 3.
    # The middle write repeats the unchanged single-result document because
    # `publish()` runs per profile whenever anything has been swept -- the same
    # behaviour a no-targets result already produces, and harmless in a shard,
    # whose output is merged rather than served.
    assert written == [1, 1, 2]


# --------------------------------------------------------------------------------
# Stale rows (#114): the union never retires a row, so name the ones the tier dropped
# --------------------------------------------------------------------------------


def _gear_document(generated_at: str, rows: list[str], available: list[str] | None) -> dict:
    """A gear document with one trinket slot, its own coverage block, and no more."""
    coverage: dict = {"specs": len(rows), "specsAvailable": len(available or rows)}
    if available is not None:
        coverage["buildsAvailable"] = sorted(available)
    return {
        "generatedAt": generated_at,
        "coverage": coverage,
        "slots": [
            {
                "id": "trinket",
                "label": "Trinket",
                "specs": [{"id": row} for row in rows],
                "coverage": {"specs": len(rows), "specsAvailable": len(available or rows)},
            }
        ],
    }


def test_a_row_whose_build_the_tier_no_longer_ships_is_named(tmp_path):
    """The merge unions rows and never retires one, so a build simc stops shipping
    leaves its row behind forever. From two counts nobody can say *which* row --
    `computed-builds.json` carried `demon_hunter_devourer_annihilator` for exactly
    that reason -- so the ids are published and the excess is named."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T12:00:00+00:00", ["a", "gone"], ["a", "b", "gone"])),
        encoding="utf-8",
    )

    shard = tmp_path / "shard-0"
    shard.mkdir()
    # The tier has since dropped `gone`, which this run's profile discovery reports.
    (shard / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T14:00:00+00:00", ["b"], ["a", "b"])),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))

    assert merged["coverage"]["specs"] == 3
    assert merged["coverage"]["specsAvailable"] == 2
    assert merged["coverage"]["staleRows"] == ["gone"]
    # Per slot too, because that is the block a reader with a slot selector shows.
    assert merged["slots"][0]["coverage"]["staleRows"] == ["gone"]
    # And the row itself is kept: naming it is the finding, deleting it is a
    # decision about somebody's data that a merge does not get to make.
    assert [spec["id"] for spec in merged["slots"][0]["specs"]] == ["a", "b", "gone"]


def test_a_healthy_document_gains_no_stale_field(tmp_path):
    """Absent is not equal, and a document with nothing stale must produce the bytes
    it produced before this existed."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T12:00:00+00:00", ["a"], ["a", "b"])),
        encoding="utf-8",
    )
    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T14:00:00+00:00", ["b"], ["a", "b"])),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    assert "staleRows" not in merged["coverage"]
    assert "staleRows" not in merged["slots"][0]["coverage"]
    assert merged["coverage"]["buildsAvailable"] == ["a", "b"]


def test_a_build_that_comes_back_stops_being_stale(tmp_path):
    """A stale mark is a claim about the current rows against the current tier, so it
    goes when the tier ships the build again -- otherwise the field outlives its own
    evidence, which is the failure this whole file is about.

    The slot this run did NOT sweep is the case that reaches it, and the first version
    of this test could not: a swept slot's block comes from the shard, which never
    carried the old mark, so the drop was untested and the canary stayed green. An
    unswept slot keeps the published block verbatim -- which is what the merge is built
    to do -- and that block is where a stale mark outlives its evidence.
    """
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    published = _gear_document("2026-08-15T12:00:00+00:00", ["a"], ["a"])
    published["slots"].append(
        {
            "id": "neck",
            "label": "Neck",
            "specs": [{"id": "a"}, {"id": "back"}],
            "coverage": {"specs": 2, "specsAvailable": 1, "staleRows": ["back"]},
        }
    )
    (out / "gear.json").write_text(json.dumps(published), encoding="utf-8")

    # A trinket-only re-run, whose profile discovery finds `back` shipping again.
    shard = tmp_path / "shard-0"
    shard.mkdir()
    document = _gear_document("2026-08-15T14:00:00+00:00", ["a"], ["a", "back"])
    document["slots"].append({"id": "neck", "label": "Neck", "specs": []})
    (shard / "gear.json").write_text(json.dumps(document), encoding="utf-8")

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    by_slot = {slot["id"]: slot for slot in merged["slots"]}
    assert "staleRows" not in merged["coverage"]
    assert "staleRows" not in by_slot["neck"]["coverage"]
    # The neck rows are untouched -- only the claim about them moved.
    assert [spec["id"] for spec in by_slot["neck"]["specs"]] == ["a", "back"]


def test_a_document_that_states_no_build_ids_claims_nothing(tmp_path):
    """Every document written before #114 carries counts alone. Unknown is not empty:
    an empty set would make every row in the document look stale, which is the
    loudest possible wrong answer."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    (out / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T12:00:00+00:00", ["a", "b"], None)),
        encoding="utf-8",
    )
    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T14:00:00+00:00", ["c"], None)),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    assert "staleRows" not in merged["coverage"]
    assert "buildsAvailable" not in merged["coverage"]


def test_the_build_list_is_taken_from_the_newest_document_that_states_one(tmp_path):
    """`merge_gear_shards` folds the published file in at its own age, so the newest
    document is frequently one written before #114. Reading `documents[-1]` alone
    would answer "unknown" for exactly the run after a single-slot sweep."""
    import json

    from wowdps.dataset import merge_gear_shards

    out = tmp_path / "MID2"
    out.mkdir()
    # The published file sorts NEWEST here and states no ids.
    (out / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T18:00:00+00:00", ["a", "gone"], None)),
        encoding="utf-8",
    )
    shard = tmp_path / "shard-0"
    shard.mkdir()
    (shard / "gear.json").write_text(
        json.dumps(_gear_document("2026-08-15T14:00:00+00:00", ["a"], ["a"])),
        encoding="utf-8",
    )

    merge_gear_shards([shard], out)
    merged = json.loads((out / "gear.json").read_text(encoding="utf-8"))
    assert merged["coverage"]["staleRows"] == ["gone"]


def test_write_gear_publishes_the_tiers_build_ids(tmp_path):
    """`specsAvailable` is derived from the list rather than passed beside it, so the
    two cannot disagree -- the shape of error this repo has shipped twice already."""
    import json

    from wowdps import dataset

    path = dataset.write_gear(
        tmp_path,
        [measured_result("Sunfury", 0.1)],
        {"trinket": POOL},
        "MID2",
        {"gitRevision": "abc"},
        SimSettings(target_error=0.0, max_iterations=1000),
        builds_available=["mage_arcane_sunfury", "mage_fire_frostfire"],
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["coverage"]["buildsAvailable"] == [
        "mage_arcane_sunfury",
        "mage_fire_frostfire",
    ]
    assert document["coverage"]["specsAvailable"] == 2
    assert document["slots"][0]["coverage"]["specsAvailable"] == 2
