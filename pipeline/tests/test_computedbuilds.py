"""The published document, pinned against the shape the site actually reads.

``computed-builds.json`` has exactly one consumer -- ``dps-computed.ts`` and
``dps-data.models.ts`` in wtt-frontend -- and it refuses a document it cannot read
rather than guessing. So the field names here are a contract, not a preference, and
these tests are the only thing on this side of the wire that can catch a rename.
"""

import json

from wowdps import buildsearch, computedbuilds, gearanchor
from wowdps.buildsearch import Candidate, Measurement
from wowdps.talenttree import Loadout

#: Every key ``dps-computed.ts`` and ``dps-data.models.ts`` read off a document. Listed
#: rather than derived: a test that asked the code what it emits would pass whatever
#: the code emitted, which is the one thing it must not do.
DATASET_KEYS = {
    "schemaVersion",
    "generatedAt",
    "tier",
    "note",
    "settings",
    "coverage",
    "specs",
}
SPEC_KEYS = {
    "id",
    "scenario",
    "targets",
    "searched",
    "simc",
    "best",
    "runnerUp",
    "anchor",
    "caveats",
}
CONTENDER_KEYS = {
    "origin",
    "label",
    "talentHash",
    "heroTalent",
    "dps",
    "dpsError",
    "iterations",
    "priorityDps",
}
ANCHOR_KEYS = {"label", "profile", "itemLevel", "normalised", "preserved", "tierSet"}


def candidate(key="best", origin="search"):
    return Candidate(
        key=key,
        label="a build",
        origin=origin,
        loadout=Loadout(version=2, spec_id=260, selections=(), spare_bits=0),
        talent_hash="CEkAAA",
        parent="s00",
    )


def measurement(dps=100.0, error=0.05):
    return Measurement(key="best", dps=dps, dps_error=error, iterations=3000)


def entry(**kw):
    defaults = dict(
        build_id="rogue_outlaw_default",
        scenario="patchwerk",
        targets=1,
        searched=True,
        simc=computedbuilds.contender_json(candidate("simcbuild", "simc"), measurement()),
        best=computedbuilds.contender_json(candidate(), measurement(103.0)),
        runner_up=None,
        anchor={k: None for k in ANCHOR_KEYS},
        caveats=["a caveat"],
    )
    defaults.update(kw)
    return computedbuilds.SpecEntry(**defaults)


def document(entries=None, calibration=None):
    return computedbuilds.build_document(
        "MID2",
        entries if entries is not None else [entry()],
        iterations=3000,
        deterministic=True,
        builds_available=42,
        calibration=calibration,
    )


# --------------------------------------------------------------------------------
# The wire shape
# --------------------------------------------------------------------------------


def test_the_document_carries_every_key_the_site_reads():
    assert DATASET_KEYS <= set(document())


def test_no_undeclared_field_reaches_a_row_the_site_reads():
    """The other half of the contract, and the half a subset test cannot state.

    Every check above asks whether a declared key is *present*; none of them notices a
    key the site has never heard of. That is the direction this document drifts in --
    a producer grows a field, nothing reads it, and the next person takes it for part
    of the contract. So the rows are pinned to exactly the declared sets.

    Two deliberate exceptions, both at document level and both named here so they stay
    the only two: ``notes`` carries per-run sentences (which seed sources were
    available) and ``calibration`` carries the gate's own table. Neither has a home in
    the declared interface, ``dps-computed.ts`` reads neither, and both are omitted
    entirely when there is nothing to say.
    """
    assert set(document()) == DATASET_KEYS
    doc = computedbuilds.build_document(
        "MID2",
        [entry()],
        iterations=3000,
        deterministic=True,
        builds_available=42,
        calibration=computedbuilds.Calibration(rows=[]),
        notes=["a note"],
    )
    assert set(doc) - DATASET_KEYS == {"notes", "calibration"}
    for row in doc["specs"]:
        assert set(row) == SPEC_KEYS
        assert set(row["anchor"]) == ANCHOR_KEYS
        for side in ("simc", "best", "runnerUp"):
            if row[side] is not None:
                assert set(row[side]) <= CONTENDER_KEYS | {"harvest", "search"}


def test_the_schema_version_is_the_one_the_site_supports():
    """``isReadableDataset`` refuses anything above ``SUPPORTED_COMPUTED_SCHEMA``, so a
    bump here renders nothing on the site until the frontend is updated -- on purpose,
    and worth pinning so the bump is deliberate."""
    assert document()["schemaVersion"] == 1


def test_each_row_carries_the_whole_join_key():
    """``findComputedSpec`` joins on ``(id, scenario, targets)``. Joining on the id
    alone would hand a ten-target reader a one-target verdict, and would make every row
    after the first unreachable when one build is computed at two target counts."""
    row = document()["specs"][0]
    assert SPEC_KEYS <= set(row)
    assert row["id"] and row["scenario"] and isinstance(row["targets"], int)


def test_two_rows_for_one_build_at_two_target_counts_are_both_reachable():
    doc = document([entry(targets=1), entry(targets=5)])
    keys = {(r["id"], r["scenario"], r["targets"]) for r in doc["specs"]}
    assert len(keys) == 2


def test_a_contender_always_carries_its_error():
    """``usable()`` on the site refuses a contender whose error cannot be read, because
    ``combinedNoise(undefined, x)`` is NaN and a candidate three percent ahead then
    classifies as a *tie*. An omitted error reverses the verdict rather than blurring
    it."""
    side = computedbuilds.contender_json(candidate(), measurement())
    assert CONTENDER_KEYS <= set(side)
    assert isinstance(side["dpsError"], float)
    assert side["dpsError"] >= 0


def test_the_origin_is_one_the_site_knows():
    assert computedbuilds.contender_json(candidate(), measurement())["origin"] in (
        "simc",
        "harvest",
        "search",
    )


def test_a_search_contender_carries_the_evidence_that_makes_it_reproducible():
    outcome = buildsearch.SearchOutcome(spec_id="s", build_id="b", seed_value=7)
    outcome.seeds["simc"] = 1
    side = computedbuilds.contender_json(candidate(), measurement(), outcome=outcome)
    assert {"method", "description", "seed", "variantsEvaluated", "startedFrom"} <= set(
        side["search"]
    )
    assert side["search"]["seed"] == 7
    assert side["search"]["startedFrom"] == "s00"


def test_coverage_is_carried_rather_than_left_to_an_array_length():
    """The site's own field comment states the project rule. The two numbers differ the
    moment a shard stops early, which is exactly when somebody is reading them."""
    doc = document([entry(), entry(targets=5)])
    assert doc["coverage"] == {"specs": 2, "specsAvailable": 42}


def test_an_unsearched_row_says_so_without_claiming_nothing_was_found():
    """``searched: false`` and ``searched: true`` with ``best: null`` are two different
    sentences on the site -- "nobody has looked" against "somebody looked and found
    nothing"."""
    row = entry(searched=False, simc=None, best=None).to_json()
    assert row["searched"] is False
    assert row["best"] is None


# --------------------------------------------------------------------------------
# The gear anchor's two shapes
# --------------------------------------------------------------------------------


def anchor_for(pieces=4):
    target = gearanchor.AnchorTarget(
        tier="MID2",
        ilevel=334,
        band=(334, 344),
        set_option="midnight_season_2",
        set_name="Season 2",
        set_pieces=pieces,
        set_tally=((4, 20), (0, 8)),
    )
    kit = gearanchor.parse_gear_lines(
        "head=a,id=1,ilevel=289\n"
        "finger1=b,id=2,ilevel=289,gem_id=7,enchant_id=9\n"
        "neck=c,id=3,ilevel=334\n"
    )
    return gearanchor.apply(target, kit)


def test_the_anchor_projects_onto_the_shape_the_site_reads():
    """``GearAnchor.to_json`` is the record and ``DpsGearAnchor`` is the reading; only
    ``itemLevel`` is common to both. wtt-frontend#130 reported the mismatch and left it
    for whoever wired the producer -- it is settled on this side, and this pins it."""
    shown = gearanchor.display_json(anchor_for(), profile="mage_arcane_sunfury")
    assert set(shown) == ANCHOR_KEYS
    assert shown["itemLevel"] == 334
    assert shown["profile"] == "mage_arcane_sunfury"
    assert isinstance(shown["normalised"], list)
    assert isinstance(shown["preserved"], list)
    assert isinstance(shown["tierSet"], str)


def test_the_anchor_names_what_it_left_alone():
    """Gem and enchant are worth an order of magnitude more than a ten-item-level step,
    so "preserved" is a claim rather than a nicety and has to be visible."""
    shown = gearanchor.display_json(anchor_for())
    left = " ".join(shown["preserved"])
    assert "gem_id" in left
    assert "enchant_id" in left


def test_the_anchor_states_where_its_item_level_came_from():
    shown = gearanchor.display_json(anchor_for())
    assert any("band" in line for line in shown["normalised"])


def test_a_tier_with_no_set_says_that_rather_than_reporting_none_worn():
    target = gearanchor.AnchorTarget(tier="MID2", ilevel=334, band=(334, 334), set_option=None)
    shown = gearanchor.display_json(gearanchor.apply(target, []))
    assert "ships no set bonus" in shown["tierSet"]


# --------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------


def test_a_quiet_re_run_leaves_the_published_stamp_alone(tmp_path):
    """The sims are deterministic, so a re-run that found the same answer should leave
    nothing to commit -- which is what makes a diff in the history mean something moved.

    The stamps are written to the second, so a test that ran twice inside one second
    would pass whatever the settle rule did. Both stamps are set explicitly instead.
    """
    first = dict(document(), generatedAt="2026-01-01T00:00:00+00:00")
    computedbuilds.write_computed_builds(tmp_path, first)

    second = dict(document(), generatedAt="2026-06-06T12:00:00+00:00")
    computedbuilds.write_computed_builds(tmp_path, second)
    again = json.loads((tmp_path / "computed-builds.json").read_text())
    assert again["generatedAt"] == "2026-01-01T00:00:00+00:00"


def test_a_run_that_found_something_different_does_move_the_stamp(tmp_path):
    """The other half, and the one that matters: settling must not swallow a real change."""
    computedbuilds.write_computed_builds(
        tmp_path, dict(document(), generatedAt="2026-01-01T00:00:00+00:00")
    )
    moved = dict(
        document([entry(best=computedbuilds.contender_json(candidate(), measurement(500.0)))]),
        generatedAt="2026-06-06T12:00:00+00:00",
    )
    computedbuilds.write_computed_builds(tmp_path, moved)
    second = json.loads((tmp_path / "computed-builds.json").read_text())
    assert second["specs"][0]["best"]["dps"] == 500.0
    assert second["generatedAt"] == "2026-06-06T12:00:00+00:00"


def test_the_calibration_travels_with_the_document(tmp_path):
    """A published search result and the evidence that the search was trusted have to
    arrive together, or a reader has the number and no way to weigh it."""
    row = computedbuilds.CalibrationRow(
        build_id="b",
        label="b",
        simc=measurement(100.0),
        found=measurement(100.0),
        variants_evaluated=20,
    )
    doc = document(calibration=computedbuilds.Calibration(rows=(row,)))
    assert doc["calibration"]["passed"] is True
    assert "fixed in advance" in doc["calibration"]["criterion"]
    assert doc["calibration"]["rows"][0]["verdict"] == "tie"


def test_the_written_file_is_valid_json_the_site_could_parse(tmp_path):
    path = computedbuilds.write_computed_builds(tmp_path, document())
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["specs"][0]["id"] == "rogue_outlaw_default"
    assert path.name == "computed-builds.json"
