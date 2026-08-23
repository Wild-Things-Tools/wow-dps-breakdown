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


# --- The tier set as the second gear caveat -------------------------------------
#
# MID2 ships two builds wearing no tier set at all -- both Arcane Mage -- beside 26
# wearing four or five pieces, and the ranking drew all 28 as bars with nothing
# saying so. Measured on simc 22b442e, 1000 deterministic iterations, patchwerk one
# target, profileset against profileset: forcing the four-piece on is worth **+13.13%**
# on Spellslinger and **+14.42%** on Sunfury. See ``tier_set_caveat``.

_MAGE_SET_ID = 2060


def _tier_set(class_id=8, tier="MID2", set_id=_MAGE_SET_ID, thresholds=(2, 4)):
    from wowdps.buffsweep import TierSet

    return TierSet(
        name="Primal Leywarden's Attire",
        option="midnight_season_2",
        tier=tier,
        class_id=class_id,
        set_id=set_id,
        thresholds=thresholds,
    )


def _geared(tmp_path, name, item_ids, unvalidated=False, wow_class="Mage", spec="Arcane"):
    """A profile on disk wearing ``item_ids``, so ``read_kit`` has something to read."""
    from wowdps.profiles import SpecProfile

    path = tmp_path / f"MID2_{name}.simc"
    lines = ["# set_bonus=midnight_season_2_2pc=1"]  # simc's generator convention
    slots = ("head", "shoulders", "chest", "hands", "legs", "back", "waist", "feet")
    for slot, item_id in zip(slots, item_ids, strict=False):
        lines.append(f"{slot}=a_thing,id={item_id},ilevel=344")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SpecProfile(
        path=path,
        tier="MID2",
        wow_class=wow_class,
        spec=spec,
        hero_talent="Spellslinger",
        # Unique per fixture: ``SpecProfile.id`` is built from it, and the reference
        # keys its per-build states on that id the way the dataset does.
        name_hero=name,
        role="spell",
        talent_hash=None,
        unvalidated=unvalidated,
        item_level=344,
    )


def _mage_item_sets(worn=(101, 102, 103, 104, 105)):
    """simc's ``item id -> id_set``: the five set pieces, and nothing else."""
    return {item_id: _MAGE_SET_ID for item_id in worn}


def test_the_reference_set_state_is_voted_on_by_the_tier_and_not_written_down(tmp_path):
    """MID2's shipped profiles wear four or five pieces; two Arcane builds wear none.

    Reduced to *states* before the vote, which is the same reduction
    ``gearanchor.derive_set_pieces`` makes and for the same reason: 12 profiles at
    four pieces and 14 at five is a coin flip between two numbers that mean the same
    thing to the simulation, where as states it is 26 to 2.
    """
    from wowdps.dataset import shipped_set_states

    sets = [_tier_set()]
    item_sets = _mage_item_sets()
    profiles = [
        _geared(tmp_path, "four", [101, 102, 103, 104]),
        _geared(tmp_path, "five", [101, 102, 103, 104, 105]),
        _geared(tmp_path, "also_five", [101, 102, 103, 104, 105]),
        _geared(tmp_path, "none", [900, 901, 902, 903]),
    ]
    reference = shipped_set_states(profiles, "MID2", sets, item_sets)

    # Four and five pieces are one state, so the tally is 3 to 1 rather than 1/2/1.
    assert reference.tally == ((0, 1), (4, 3))
    assert reference.state == 4
    assert reference.majority == 3 and reference.voters == 4
    assert reference.states[profiles[0].id] == 4
    assert reference.states[profiles[3].id] == 0


def test_a_build_wearing_no_tier_set_beside_a_tier_that_does_says_so(tmp_path):
    """The published finding, as a sentence. Measured at +13.13% and +14.42%.

    Symmetric, like ``gear_caveat``: the wording is derived from the two states and
    the tally, so a tier wearing nothing would flag the build that wears the set.
    """
    from wowdps.dataset import shipped_set_states, tier_set_caveat

    sets = [_tier_set()]
    item_sets = _mage_item_sets()
    wearing = _geared(tmp_path, "fire", [101, 102, 103, 104])
    bare = _geared(tmp_path, "arcane", [900, 901, 902, 903])
    reference = shipped_set_states(
        [wearing, _geared(tmp_path, "frost", [101, 102, 103, 104]), bare], "MID2", sets, item_sets
    )

    assert tier_set_caveat(wearing, reference) is None
    said = tier_set_caveat(bare, reference)
    assert said is not None
    assert "no set bonus" in said and "the 4-piece bonus" in said
    # The numbers in the sentence are the tally's, not anybody's.
    assert "2 of the 3" in said


def test_a_disabled_profile_does_not_vote_on_what_shipped_gear_looks_like(tmp_path):
    """Same exclusion as the item-level band, and for the same reason.

    MID2's twelve disabled profiles wear no tier set at all. Letting them vote would
    drag the reference toward their own gap and quietly excuse it -- and at MID2's
    real proportions (12 disabled against 28 shipped, 14 bare in total) it would turn
    a 26-to-2 majority into a 26-to-14 one and then, with two more, into none at all.
    They are still flagged, because saying so about them is the point.
    """
    from wowdps.dataset import shipped_set_states, tier_set_caveat

    sets = [_tier_set()]
    item_sets = _mage_item_sets()
    shipped = _geared(tmp_path, "shipped", [101, 102, 103, 104])
    disabled = [_geared(tmp_path, f"disabled{n}", [900, 901], unvalidated=True) for n in range(4)]
    reference = shipped_set_states([shipped, *disabled], "MID2", sets, item_sets)

    assert reference.tally == ((4, 1),), "only the shipped profile voted"
    assert reference.state == 4
    assert tier_set_caveat(shipped, reference) is None
    assert tier_set_caveat(disabled[0], reference) is not None


def test_a_tier_split_down_the_middle_flags_nobody(tmp_path):
    """ "This build differs from what the tier wears" needs a tier that wears one thing.

    A strict majority is what the word means, not a tolerance somebody tuned, and the
    failure direction is the safe one: no majority, no flag, rather than half the tier
    accused of disagreeing with a coin toss.
    """
    from wowdps.dataset import shipped_set_states, tier_set_caveat

    sets = [_tier_set()]
    item_sets = _mage_item_sets()
    profiles = [
        _geared(tmp_path, "a", [101, 102, 103, 104]),
        _geared(tmp_path, "b", [101, 102, 103, 104]),
        _geared(tmp_path, "c", [900, 901]),
        _geared(tmp_path, "d", [900, 901]),
    ]
    reference = shipped_set_states(profiles, "MID2", sets, item_sets)

    assert reference.state is None
    assert reference.tally == ((0, 2), (4, 2))
    assert all(tier_set_caveat(p, reference) is None for p in profiles)


def test_a_class_the_tier_ships_no_set_for_is_not_a_class_wearing_none(tmp_path):
    """A class with no set cannot be accused of not wearing it.

    Neither MID1 nor MID2 has such a class -- both ship one set per class -- but a
    tier with a partial set list would otherwise flag exactly the classes it forgot,
    since ``count_set_pieces`` over an empty id set correctly returns zero.
    """
    from wowdps.dataset import shipped_set_states, tier_set_caveat

    sets = [_tier_set(class_id=8)]  # Mage only
    item_sets = _mage_item_sets()
    mages = [_geared(tmp_path, f"mage{n}", [101, 102, 103, 104]) for n in range(3)]
    rogue = _geared(tmp_path, "rogue", [900, 901], wow_class="Rogue", spec="Outlaw")
    reference = shipped_set_states([*mages, rogue], "MID2", sets, item_sets)

    assert reference.states[rogue.id] is None
    assert reference.voters == 3, "the Rogue did not vote"
    assert tier_set_caveat(rogue, reference) is None


def test_the_reference_is_shard_safe_because_every_input_is_what_simc_ships(tmp_path):
    """Twelve shards must compute one reference, or the merge keeps a wrong manifest.

    Driven as the property rather than asserted in prose: the same tier sliced three
    ways -- which is what ``_select`` hands a shard -- returns the same state, the
    same tally and the same verdict for every build. It holds because the caller
    passes every profile of the tier; a shard passing its own slice is exactly the
    silent failure this pins.
    """
    from wowdps.dataset import shipped_set_states, tier_set_caveat

    sets = [_tier_set()]
    item_sets = _mage_item_sets()
    tier = [
        _geared(tmp_path, "a", [101, 102, 103, 104]),
        _geared(tmp_path, "b", [101, 102, 103, 104, 105]),
        _geared(tmp_path, "c", [101, 102, 103, 104]),
        _geared(tmp_path, "d", [900, 901, 902]),
    ]
    whole = shipped_set_states(tier, "MID2", sets, item_sets)

    for shard in ([tier[0]], [tier[1], tier[3]], tier[2:]):
        # Every shard is handed the whole tier and simulates its own slice.
        theirs = shipped_set_states(tier, "MID2", sets, item_sets)
        assert (theirs.state, theirs.tally) == (whole.state, whole.tally)
        for profile in shard:
            assert (tier_set_caveat(profile, theirs) is None) == (
                tier_set_caveat(profile, whole) is None
            )

    # And the failure it prevents: a shard voting on its own slice gets a different
    # answer -- the bare build's shard has no majority at all.
    alone = shipped_set_states([tier[3]], "MID2", sets, item_sets)
    assert alone.state == 0 and tier_set_caveat(tier[3], alone) is None


def test_the_flag_is_emitted_only_when_false_so_a_quiet_tier_commits_nothing():
    """MID1's 41 shipped profiles all wear four pieces, so that tier gains no key.

    Same rule as ``gearComparable`` and ``unvalidated``. A tier where every build
    wears the same set has to produce the bytes it did before this existed, or a
    deterministic run stops meaning "any diff is the game moving".
    """
    from wowdps.dataset import SpecResult

    comparable = SpecResult(profile=_profile(344))
    assert "tierSetComparable" not in comparable.summary()

    flagged = SpecResult(profile=_profile(344), tier_set_comparable=False)
    assert flagged.summary()["tierSetComparable"] is False


def test_an_item_level_gap_and_a_set_gap_are_two_flags_because_a_build_can_have_one():
    """MID2's Arcane builds sit inside the item-level band and wear no set.

    One boolean would leave the sentence beside it guessing which claim it carried,
    and the disabled profiles -- which carry both gaps -- would make the two look
    interchangeable. They are not: 45 item levels and a tier set are different
    differences, and closing one leaves the other looking fixed.
    """
    from wowdps.dataset import SpecResult

    arcane = SpecResult(profile=_profile(344), tier_set_comparable=False)
    summary = arcane.summary()
    assert summary["tierSetComparable"] is False
    assert "gearComparable" not in summary

    disabled = SpecResult(
        profile=_profile(289, unvalidated=True), gear_comparable=False, tier_set_comparable=False
    )
    both = disabled.summary()
    assert both["gearComparable"] is False and both["tierSetComparable"] is False


# --- The reference has to survive the trip into CI ------------------------------
#
# It did not. `tierSetComparable` worked from a full simc checkout and could not work
# in the nightly, for two independent reasons, and neither raised: the shard bundle
# carried none of the tables the check reads, and the live/PTR choice was read out of
# a manifest under `--out`, which a sharded run points at a fresh empty directory.
# Measured against the published MID2 dataset before the fix: the flag appears on
# **0 of 36** rows, while a local run over the same profiles flags two.


def _bundle_globs():
    """The `engine/dbc/generated` files `sims.yml` actually packages, as globs.

    Read out of the workflow rather than written down again. The point of the test
    below is that the bundle is *sufficient*, and a second copy of the file list
    here would let the two drift in exactly the direction that went unnoticed.
    """
    import re

    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "sims.yml"
    return re.findall(
        r"cp\s+simc/engine/dbc/generated/(\S+)\s+bundle/engine/dbc/generated/",
        workflow.read_text(encoding="utf-8"),
    )


def _simc_checkout(tmp_path, *, ptr_set_id=None, tables=True):
    """A simc source tree: profiles plus the whole of `engine/dbc/generated`.

    ``ptr_set_id`` writes the ``*_ptr.inc`` variants with a different set id, so the
    live and PTR tables give measurably different answers. simc's real ones agree
    today -- 12,260 items carry an ``id_set`` in each and the two maps are equal on
    625a591 -- which is why a fixture has to manufacture the disagreement.
    """
    from test_gearanchor import FOUR_PIECE, ITEM_DATA_INC, NO_PIECE, SET_BONUS_INC

    simc_dir = tmp_path / "simc"
    generated = simc_dir / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "trait_data.inc").write_text("", encoding="utf-8")
    (generated / "trait_data_ptr.inc").write_text("", encoding="utf-8")
    if tables:
        for name, text in (
            ("item_data.inc", ITEM_DATA_INC),
            ("item_data_ptr.inc", ITEM_DATA_INC),
            ("item_set_bonus.inc", SET_BONUS_INC),
            (
                "item_set_bonus_ptr.inc",
                SET_BONUS_INC.replace("2060", str(ptr_set_id)) if ptr_set_id else SET_BONUS_INC,
            ),
        ):
            (generated / name).write_text(text, encoding="utf-8")

    tier_dir = simc_dir / "profiles" / "MID2"
    tier_dir.mkdir(parents=True)
    for name, body in (
        ("MID2_Mage_Arcane_Wearing", FOUR_PIECE),
        ("MID2_Mage_Arcane_Also", FOUR_PIECE),
        ("MID2_Mage_Arcane_Bare", NO_PIECE),
    ):
        (tier_dir / f"{name}.simc").write_text(
            f'mage="{name}"\nspec=arcane\nlevel=80\nrole=spell\n'
            + body.split("\n", 2)[2],  # drop the fixture's own name/spec lines
            encoding="utf-8",
        )
    return simc_dir


def _package_bundle(simc_dir, bundle):
    """Reproduce `sims.yml`'s bundle packaging: profiles plus the copied globs."""
    import shutil

    shutil.copytree(simc_dir / "profiles", bundle / "profiles")
    generated = bundle / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    for glob in _bundle_globs():
        for found in sorted((simc_dir / "engine" / "dbc" / "generated").glob(glob)):
            shutil.copy(found, generated / found.name)
    return bundle


def test_the_nightly_bundle_carries_every_table_the_tier_set_check_reads(tmp_path):
    """The shard's simc directory is the bundle, and the bundle has to be enough.

    `wowdps build --profiles bundle/profiles` makes `_tier_set_reference` look in
    `bundle/engine/dbc/generated`, and `sims.yml` copied only `trait_data*.inc`
    there. So the tables were absent in every shard, the function warned and
    returned ``None``, and no build was flagged in the published dataset -- the
    feature worked locally and could not work in CI.

    Driven through the workflow's own copy list rather than a second copy of it, and
    end to end through the real function, so it fails if either side moves.
    """
    from wowdps import cli

    simc_dir = _simc_checkout(tmp_path)
    bundle = _package_bundle(simc_dir, tmp_path / "bundle")

    reference = cli._tier_set_reference(bundle / "profiles", "MID2")

    assert reference is not None, f"the bundle {_bundle_globs()} is not enough to answer"
    assert reference.state == 4
    assert reference.tally == ((0, 1), (4, 2))


def test_a_bundle_without_the_tables_still_costs_only_the_flag(tmp_path, caplog):
    """A missing table must never fail a night of simulations, and does not.

    This is the behaviour the defect above hid behind, and it is correct: the run
    continues, nothing is flagged, and the dataset is the one it was before the flag
    existed. Kept legible on purpose -- the warning names the path, the errno and
    what is lost -- because that line is how anybody notices.
    """
    import logging

    from wowdps import cli

    simc_dir = _simc_checkout(tmp_path, tables=False)
    bundle = _package_bundle(simc_dir, tmp_path / "bundle")

    with caplog.at_level(logging.WARNING):
        assert cli._tier_set_reference(bundle / "profiles", "MID2") is None
    said = caplog.text
    assert "item_set_bonus" in said and "No such file" in said
    assert "no build will be checked" in said


def test_which_item_table_the_check_reads_is_stated_and_not_found_in_a_directory(tmp_path):
    """Live or PTR is an argument now, because the manifest could not answer it.

    Two failures in one line. The manifest was read from ``<--out>/<tier>/index.json``
    and the nightly passes ``--out shard``, a fresh empty directory, so ``ptr`` was
    never anything but ``False`` -- and the flag it would have read, ``simc.ptr``, is
    ``report["ptr_enabled"]``, i.e. ``SC_USE_PTR``, a compile constant that is 1 for
    every binary this project builds. What the sims actually read is ``Live``.

    So the answer is stated. The fixture manufactures a disagreement between the two
    tables that simc's real ones do not have, or neither branch would prove anything.
    """
    from wowdps import cli

    simc_dir = _simc_checkout(tmp_path, ptr_set_id=9999)
    bundle = _package_bundle(simc_dir, tmp_path / "bundle")
    profiles_dir = bundle / "profiles"

    live = cli._tier_set_reference(profiles_dir, "MID2", ptr=False)
    ptr = cli._tier_set_reference(profiles_dir, "MID2", ptr=True)

    assert live is not None and ptr is not None
    # The PTR table names a set nothing in the item table belongs to, so every build
    # counts zero pieces there and the whole tier reads as wearing nothing.
    assert (live.state, ptr.state) == (4, 0)
    assert live.tally == ((0, 1), (4, 2)) and ptr.tally == ((0, 3),)

    # And it is an argument rather than something discovered: there is no directory
    # left whose contents could change the answer.
    import inspect

    taken = set(inspect.signature(cli._tier_set_reference).parameters)
    assert taken == {"profiles_dir", "tier", "ptr"}


def test_build_command_never_enables_ptr_data():
    """``USES_PTR_DATA`` is the default the tier-set check inherits. Pin it.

    ``ptr=1`` before the profile takes MID2 Arcane from 169,135 to 188,911 DPS at one
    iteration, so this is the difference between two games and not a label. Nothing
    here passes it, simc's own report agrees (``dbc.version_used == "Live"`` on
    625a591), and ``cli._tier_set_reference`` therefore reads the live tables by
    default. If a scenario ever starts asking for PTR data, this test is where the
    tier-set check finds out.
    """
    from wowdps import scenarios, simc_runner
    from wowdps.cli import build_parser
    from wowdps.profiles import SpecProfile
    from wowdps.simc_runner import SimRequest, SimSettings

    profile = SpecProfile(
        path=Path("MID2_Mage_Arcane.simc"),
        tier="MID2",
        wow_class="Mage",
        spec="Arcane",
        hero_talent="Spellslinger",
        name_hero="Spellslinger",
        role="spell",
        talent_hash=None,
    )
    for scenario in scenarios.ALL_SCENARIOS:
        command = simc_runner.build_command(
            Path("simc"),
            SimRequest(profile=profile, scenario=scenario, targets=1),
            SimSettings(target_error=0, max_iterations=100),
            Path("out.json"),
        )
        assert not any(part.startswith("ptr=") for part in command), command

    assert simc_runner.USES_PTR_DATA is False
    # The CLI default follows the constant rather than repeating it.
    parsed = build_parser().parse_args(["build"])
    assert parsed.ptr is simc_runner.USES_PTR_DATA is False
