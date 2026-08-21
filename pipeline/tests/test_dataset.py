from pathlib import Path


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


def test_an_unvalidated_build_is_labelled_in_both_documents():
    """A number off a disabled profile is a weaker claim and has to say so."""
    from wowdps.dataset import SpecResult
    from wowdps.profiles import SpecProfile

    def result(unvalidated: bool) -> SpecResult:
        return SpecResult(
            profile=SpecProfile(
                path=Path("MID2_Warrior_Fury.simc"),
                tier="MID2",
                wow_class="Warrior",
                spec="Fury",
                hero_talent="Slayer",
                role="attack",
                talent_hash=None,
                unvalidated=unvalidated,
            )
        )

    assert result(True).to_json()["unvalidated"] is True
    assert result(True).summary()["unvalidated"] is True
    # Absent, not false: a tier of shipped profiles produces the bytes it did
    # before this existed, so a quiet night still has nothing to commit.
    assert "unvalidated" not in result(False).to_json()
    assert "unvalidated" not in result(False).summary()


def test_coverage_counts_the_unvalidated_builds_a_run_actually_produced():
    from wowdps.dataset import apply_simulated_coverage

    manifest = {
        "coverage": {"shipped": [{"class": "Mage", "spec": "Arcane"}]},
        "specs": [
            {"class": "Mage", "spec": "Arcane"},
            {"class": "Warrior", "spec": "Fury", "unvalidated": True},
            {"class": "Warrior", "spec": "Arms", "unvalidated": True},
        ],
    }
    coverage = apply_simulated_coverage(manifest)["coverage"]
    assert coverage["simulated"] == 1
    assert coverage["broken"] == []
    assert coverage["unvalidatedSimulated"] == 2


def _profile(item_level, unvalidated=False, name="Fury"):
    from wowdps.profiles import SpecProfile

    return SpecProfile(
        path=Path(f"MID2_Warrior_{name}.simc"),
        tier="MID2",
        wow_class="Warrior",
        spec=name,
        hero_talent="Slayer",
        role="attack",
        talent_hash=None,
        unvalidated=unvalidated,
        item_level=item_level,
    )


def test_the_shipped_band_is_a_range_because_the_tier_wears_one():
    """A single mode flagged seven of MID2's own shipped builds as incomparable.

    Its profiles genuinely sit at both 334 and 344, so the anchor has to be the band
    they span. Disabled profiles are excluded from it so one cannot drag the anchor
    toward itself and excuse its own gap.
    """
    from wowdps.dataset import shipped_item_levels

    tier = [_profile(344), _profile(334), _profile(289, unvalidated=True)]
    assert shipped_item_levels(tier) == (334, 344)
    assert shipped_item_levels([_profile(None)]) is None


def test_a_build_outside_the_band_says_its_place_is_gear():
    """Absolute DPS does not survive an item-level difference, inside a tier either.

    MID2's disabled profiles wear 289 against a shipped 334-344, and all eight
    resulting builds landed below all twenty-eight shipped ones with no overlap --
    the signature of a systematic difference, not of eight bad specs.
    """
    from wowdps.dataset import gear_caveat

    band = (334, 344)
    assert gear_caveat(_profile(344), band) is None
    assert gear_caveat(_profile(334), band) is None

    low = gear_caveat(_profile(289, unvalidated=True), band)
    assert low is not None
    assert "45 below" in low and "334-344" in low

    # Not always downward: one MID2 generator entry carries a War Within item level.
    high = gear_caveat(_profile(723, unvalidated=True), band)
    assert high is not None and "379 above" in high


def test_an_unstated_item_level_is_flagged_only_for_a_profile_simc_did_not_ship():
    """Absence is not comparability -- but eight shipped MID2 profiles omit it too.

    Saying nothing for an unvalidated build would let exactly the ones that cannot be
    checked pass as checked; saying something for a shipped one would flag a third of
    the tier for a convention simc uses everywhere.
    """
    from wowdps.dataset import gear_caveat

    assert gear_caveat(_profile(None), (334, 344)) is None
    unchecked = gear_caveat(_profile(None, unvalidated=True), (334, 344))
    assert unchecked is not None
    assert "could not" in unchecked
