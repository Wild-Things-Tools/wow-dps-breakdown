def test_simc_metadata_carries_the_game_build_the_reader_needs():
    """ "Is last night's tuning in here?" is answered by the hotfix date on the page.

    The game build is not at the report's top level -- it is under the first actor's
    dbc block, keyed by data source -- so a naive top-level read misses it.
    """
    from wowdps.simc_runner import simc_metadata

    report = {
        "version": "1210-01",
        "ptr_enabled": False,
        "sim": {
            "players": [
                {
                    "dbc": {
                        "Live": {
                            "build_level": 69273,
                            "wow_version": "12.1.0.69273",
                            "hotfix_date": "2026-08-12",
                        }
                    }
                }
            ]
        },
    }
    meta = simc_metadata(report)
    assert meta["wowVersion"] == "12.1.0.69273"
    assert meta["wowBuild"] == 69273
    assert meta["hotfixDate"] == "2026-08-12"


def test_a_report_without_a_dbc_block_leaves_the_game_build_null_not_absent():
    """Null tells a reader "no build in this report"; absent looks like a bug."""
    from wowdps.simc_runner import simc_metadata

    meta = simc_metadata({"version": "1210-01", "sim": {"players": []}})
    assert meta["wowVersion"] is None
    assert meta["hotfixDate"] is None


def test_a_tier_whose_profiles_no_longer_load_does_not_report_as_fully_covered():
    """The MID1 case, which shipped: 26 of 26 specs claimed over a dataset missing ten.

    ``spec_coverage`` answers what simc *ships*, which is the only question a shard
    can answer. Taken as the coverage of the published dataset it is wrong in the
    direction nobody checks -- the panel says "complete" and the ranking has no Mage
    in it, so the one reading available to a reader is "Mages rank nowhere".
    """
    from wowdps.dataset import apply_simulated_coverage

    manifest = {
        "specs": [
            {"class": "Mage", "spec": "Arcane"},
            {"class": "Mage", "spec": "Fire"},
        ],
        "coverage": {
            "damageSpecs": 4,
            "damageSpecsKnown": 4,
            "missing": [],
            "shipped": [
                {"class": "Mage", "spec": "Arcane"},
                {"class": "Mage", "spec": "Fire"},
                {"class": "Warrior", "spec": "Arms"},
                {"class": "Warrior", "spec": "Fury"},
            ],
        },
    }

    coverage = apply_simulated_coverage(manifest)["coverage"]

    assert coverage["simulated"] == 2
    assert coverage["broken"] == [
        {"class": "Warrior", "spec": "Arms"},
        {"class": "Warrior", "spec": "Fury"},
    ]
    # `missing` is untouched -- it answers a different question and the two must not
    # be merged: no profile at all, versus a profile that no longer loads.
    assert coverage["missing"] == []


def test_two_builds_of_one_spec_count_that_spec_once():
    """A spec is simulated if *any* of its hero builds produced results."""
    from wowdps.dataset import apply_simulated_coverage

    manifest = {
        "specs": [
            {"class": "Mage", "spec": "Frost", "heroTalent": "Frostfire"},
            {"class": "Mage", "spec": "Frost", "heroTalent": "Spellslinger"},
        ],
        "coverage": {"shipped": [{"class": "Mage", "spec": "Frost"}]},
    }

    coverage = apply_simulated_coverage(manifest)["coverage"]
    assert coverage["simulated"] == 1
    assert coverage["broken"] == []


def test_a_manifest_predating_shipped_is_left_alone_rather_than_guessed_at():
    """Without knowing what the tier shipped, broken and missing cannot be split.

    Inventing the split would be the same error in the other direction, so an old
    manifest keeps its coverage untouched and the view falls back to the two-state
    reading it was written for.
    """
    from wowdps.dataset import apply_simulated_coverage

    manifest = {"specs": [{"class": "Mage", "spec": "Arcane"}], "coverage": {"damageSpecs": 1}}
    coverage = apply_simulated_coverage(manifest)["coverage"]

    assert "broken" not in coverage
    assert "simulated" not in coverage
