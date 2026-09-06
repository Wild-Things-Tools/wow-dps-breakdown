"""``wowdps build-search`` end to end, with simc stubbed out.

**This file exists because of a defect it would have caught.** A refactor changed what
``_head_to_head`` returns and left the caller unpacking the old shape; every unit test
passed, ``ruff`` passed, and the command died with ``ValueError: too many values to
unpack`` on the first build of a real run -- after two minutes of simulation. Nothing
between the search's pure functions and a live simc was covered, so nothing could see
it.

What is stubbed is exactly one thing: the profileset runner. Everything else is real --
the profile discovery, the trait table parse, the gear anchor, the seeding, the repair,
the climb, the pruning, the document, the writer and the exit code.

Needs a simc checkout, so it skips without ``WOWDPS_SIMC_DIR``. That is the same bargain
``test_talenttree``'s corpus test makes: the 686 KB trait table is not committed, and a
hermetic version of this test would have to stub the tree as well and would then be
testing the stub.
"""

import dataclasses
import json
import os
import pathlib
import re
import shutil

import pytest

from wowdps import buildsearch, cli, talentedit, talentrepair, unvalidated
from wowdps import talenttree as tt

SIMC_DIR = os.environ.get("WOWDPS_SIMC_DIR")
pytestmark = pytest.mark.skipif(
    not SIMC_DIR, reason="set WOWDPS_SIMC_DIR to a simc checkout to run the end-to-end command"
)


class _StubSimc:
    """Scores a candidate by its key, deterministically. No simc, no subprocess."""

    def __init__(self):
        self.calls = []

    def __call__(self, candidates, iterations):
        self.calls.append((iterations, len(candidates)))
        return {
            c.key: buildsearch.Measurement(
                key=c.key,
                # The seed wins, so a blind run comes out a tie and a normal run
                # presents simc's build -- the states the document has to express.
                dps=200000.0 - 100.0 * len(c.lineage),
                dps_error=0.05,
                iterations=iterations,
            )
            for c in candidates
        }


@pytest.fixture()
def stubbed(monkeypatch):
    stub = _StubSimc()
    monkeypatch.setattr(buildsearch, "simc_runner", lambda *a, **k: stub)
    monkeypatch.setattr(cli.simc_runner, "find_simc", lambda explicit=None: pathlib.Path("/simc"))
    return stub


@pytest.fixture(scope="module")
def profiles_dir(tmp_path_factory):
    """A profiles tree with the disabled builds materialised, like the workflow's.

    Three of these tests drive `--build warrior_arms` and
    `--build paladin_retribution_default`. simc ships neither: both live in
    `profiles/generators/MID2/` commented out, so `--profiles` pointed straight at
    the checkout finds no such build and `cmd_build_search` exits 1 with "no build
    of MID2 matches ...". `build-search.yml` runs `wowdps unvalidated --write`
    before the search; this is that step.

    **Into a copy, never the checkout.** `unvalidated --write` defaults to writing
    into `simc/profiles/<tier>` itself, so a fixture that used the default would
    mutate a shared read-only input -- and these tests would then pass only on a
    machine where they had already been run once. That is in fact how they were
    green when they were written.

    **Deliberately NOT also `wowdps extra-builds`, though the workflow runs it.**
    Measured 2026-08-26: `extra-builds` supersedes `MID2_Warrior_Arms.simc` with an
    already-repaired hash, so the repair the search is supposed to perform becomes
    a no-op. Mirroring the workflow exactly is the wrong instinct here: the repair
    tests are about the repair, and `extra-builds` is what removes the thing to
    repair.

    **The refused hash is constructed, not borrowed from simc.** It used to be
    borrowed: `--build warrior_arms` picked up the disabled generator block, whose
    stored hash simc's own parser refused, and the two repair tests rode on that.
    On 2026-08-28 that stopped being true -- simc now *ships* Arms, Fury and both
    Havoc builds as ordinary profiles whose hashes decode cleanly with no spec-rule
    violation, so `talentrepair` had no subject anywhere in MID2 and both tests went
    red having found nothing wrong with the code (issue #107).

    That is the wrong thing to depend on in either direction: a test of **our**
    repair must not go red because simc fixed **its** profile, and it must not go
    green for that reason either. `refusable_profiles` therefore builds the refusal
    itself -- see its own docstring.

    The shipped profiles are copied in alongside, not replaced -- a
    materialised-only directory has no profile stating an item level, and
    `gearanchor` refuses it ("no shipped profile of MID2 states an item level on
    any gear line, so there is no band to anchor inside").
    """
    simc = pathlib.Path(SIMC_DIR)
    shipped_dir = simc / "profiles" / "MID2"
    root = tmp_path_factory.mktemp("search-profiles") / "profiles"
    shutil.copytree(shipped_dir, root / "MID2")
    shipped = {path.name for path in shipped_dir.glob("*.simc")}
    unvalidated.write_profiles(unvalidated.extract_tier(simc, "MID2"), root / "MID2", shipped)
    return root


def _substitutions(loadout, nodes, spec, *, accept):
    """Every hash reachable by moving ONE single-rank selection onto a node the class
    has and this specialisation does not own. Yields `(node_id, hash)` lazily.

    Shared by the two fixtures below because it is one rule -- *build simc's refusal
    number 5 out of a build that is currently legal* -- and they differ only in which
    substitution they want. Written twice it would drift, and a drifted copy here
    produces a fixture that still constructs *a* hash and no longer constructs the one
    the test names.

    `accept(victim, trait)` is applied **before** the encode, which is the expensive
    step: without that filter this walks every selection against every node of the
    class and re-encodes each pair.
    """
    taken = {selection.node_id for selection in loadout.selections}
    for victim in reversed(loadout.selections):
        if victim.rank != 1 or victim.choice_index is not None:
            continue
        for node_id, entries in nodes.items():
            trait = entries[0]
            if (
                node_id in taken
                or len(entries) != 1
                or not trait.spec_ids
                or spec in trait.spec_ids
                or not accept(victim, trait)
            ):
                continue
            swapped = dataclasses.replace(
                victim,
                node_id=node_id,
                entry_id=trait.entry_id,
                name=trait.name,
                spell_id=trait.spell_id,
                row=trait.row,
                col=trait.col,
                node_type=trait.node_type,
            )
            kept = tuple(one for one in loadout.selections if one is not victim)
            selections = tuple(sorted(kept + (swapped,), key=lambda one: one.node_id))
            try:
                yield (
                    node_id,
                    tt.encode_loadout(
                        dataclasses.replace(
                            loadout, selections=selections, framing=None, spare_bits=0
                        ),
                        nodes,
                    ),
                )
            except Exception:
                continue


def _arms_loadout(profiles_dir):
    """The Arms profile's hash, its spec id, its class node table and its decode."""
    traits = tt.parse_trait_data(pathlib.Path(SIMC_DIR), ptr=True)
    source = profiles_dir / "MID2" / "MID2_Warrior_Arms.simc"
    text = source.read_text()
    match = re.search(r"^talents=(\S+)", text, re.M)
    assert match, "the Arms profile states no talent hash"
    original = match.group(1)
    nodes = tt.nodes_for_class(traits, tt.CLASS_IDS["Warrior"])
    return text, original, tt.read_header(original), nodes, tt.decode_loadout(original, nodes)


def _rewritten(profiles_dir, tmp_path_factory, name, text, original, replacement):
    """A private copy of the profiles tree with one build's hash replaced."""
    root = tmp_path_factory.mktemp(name) / "profiles"
    shutil.copytree(profiles_dir, root)
    target = root / "MID2" / "MID2_Warrior_Arms.simc"
    target.write_text(text.replace(f"talents={original}", f"talents={replacement}"))
    return root


@pytest.fixture(scope="module")
def refusable_profiles(profiles_dir, tmp_path_factory):
    """`profiles_dir`, with Arms Warrior's hash rewritten so simc would refuse it.

    **Why constructed rather than borrowed.** Until 2026-08-28 the two repair tests
    used simc's own Arms profile, whose stored hash its parser refused. simc has
    since repaired it -- measured on `30555ef`, all four of the specs `talentrepair`
    was written for (Arms, Fury, both Havoc) now ship as ordinary profiles that
    decode with no spec-rule violation. The tests then failed while the repair code
    was untouched and correct, which is a test measuring simc's data rather than our
    behaviour.

    **The refusal is simc's number 5**, the one `spec_rule_violation` predicts
    offline: a non-hero node whose `id_spec` excludes the player's spec. One of the
    build's own single-rank selections is moved onto a node the *class* has and
    *Arms* does not own, and the hash re-encoded. Decoding it back yields simc's
    literal wording, full stop included:

        Selected node 90265 entry 112116 is not available to player's spec.

    Three constraints on which node may be substituted, and each is load-bearing:

    * **same tree and sub-tree, same `max_ranks`** -- otherwise the swap moves a
      point between trees and `talentrepair`'s soundness screen rejects the decode
      outright ("the build reads 37 points in the class tree, above the 36 in MID2
      shipped profiles"). Measured: the first candidate node tried did exactly that.
      `unsound_profiles` below is that failure, kept on purpose.
    * **a single-entry node**, so no choice index is involved and the refusal under
      test is unambiguously the spec rule rather than a choice-bit failure.
    * **the repair must actually succeed** -- the pair is searched until
      `talentrepair.repair` returns `ok`, because a test asserting the repaired
      build reaches the document needs a repairable hash, not merely a refused one.

    Nothing here is hard-coded to a node id: the substitution is searched against
    whatever tree the checkout ships, so this survives simc renumbering its nodes.
    A checkout where no such substitution exists skips rather than passes silently.
    """
    text, original, spec, nodes, loadout = _arms_loadout(profiles_dir)
    budget = talentedit.derive_point_budget([loadout], source="the Arms profile")

    refused = None
    for _, candidate in _substitutions(
        loadout,
        nodes,
        spec,
        accept=lambda victim, trait: (
            trait.tree_index == victim.tree_index
            and trait.sub_tree == victim.sub_tree
            and trait.max_ranks == victim.max_ranks
        ),
    ):
        if talentrepair.repair("warrior_arms", candidate, nodes, budget, (0, 9)).ok:
            refused = candidate
            break

    if refused is None:
        pytest.skip("no repairable spec-rule substitution exists in this simc checkout")

    # Assert the premise rather than trusting it: a fixture that silently produced a
    # LOADABLE hash would make both tests pass for the wrong reason.
    assert tt.spec_rule_violation(tt.decode_loadout(refused, nodes), nodes)

    return _rewritten(profiles_dir, tmp_path_factory, "refusable", text, original, refused)


@pytest.fixture(scope="module")
def unsound_profiles(profiles_dir, tmp_path_factory):
    """`profiles_dir`, with Arms Warrior's hash rewritten so its decode is refused by
    `talentrepair`'s soundness screen rather than repaired.

    **Why constructed rather than borrowed, again.** The test below used to drive
    `--build paladin_retribution_default`: simc shipped Retribution only as a
    switched-off generator block whose stored hash read 37 points in the
    specialisation tree against a budget of 34, so the screen refused it. Measured on
    `ce0f194`, **2026-09-06**, that is no longer true -- simc ships Retribution as two
    ordinary profiles (`..._herald_of_the_sun` and `..._templar`), both decoding
    soundly, so `wowdps unvalidated --write` materialises nothing for it and
    `cmd_build_search` exits 1 with *"no build of MID2 matches
    'paladin_retribution_default'"*. That failure is on `main`, not in the change that
    found it.

    That is the **third** time a test here has gone red because simc repaired its own
    profile -- Arms, Fury and both Havoc builds on 2026-08-28 (issue #107), and now
    Retribution -- and `refusable_profiles` above already carries the lesson: *a test
    of our behaviour must not go red because simc fixed its data, and must not go
    green for that reason either.*

    **The construction is the one `refusable_profiles` refuses.** Its docstring names
    the constraint that keeps a substitution inside one tree, and the measured reason:
    a cross-tree swap moves a point and the soundness screen rejects the whole decode.
    So this fixture inverts exactly that one constraint and keeps every other. On
    `ce0f194` the Arms profile spends 36/34/14 against a budget of 37/34/14, so moving
    a class point into the specialisation tree overruns it by one and the screen says
    so in its own words:

        the build reads 35 points in the specialisation tree, above the 34 in MID2
        shipped profiles

    **Both halves are required and neither implies the other.** `prepare` reaches its
    blocked state through the spec rule *and then* a refused repair, so a substitution
    that merely overruns the budget would decode, break no rule simc checks, and be
    searched normally. The loop asserts both.

    **Padding the string was tried first and cannot work.** The talent hash has no
    length field, so appending characters of zeros leaves the node stream untouched
    and only moves `spare_bits` out of the corpus range -- which the screen does
    reject. But `decode_loadout` still *succeeds* on such a hash and it breaks no spec
    rule, so `prepare` never consults `talentrepair` at all and the build is searched.
    A screen the run never reaches is not the state this test is about.

    **The corpus cannot be widened by any of this.** `talentrepair.corpus_from` reads
    the real simc checkout rather than `--profiles`, so the rewritten copy in this
    temporary tree is screened against the shipped population and can never enter it.
    """
    text, original, spec, nodes, loadout = _arms_loadout(profiles_dir)
    corpus, _ = talentrepair.corpus_from(pathlib.Path(SIMC_DIR), ("MID2",), ptr=True)
    if not corpus:
        pytest.skip("no shipped MID2 profile decodes in this checkout")
    framing = talentrepair.observed_framing(corpus)
    budget = talentedit.derive_point_budget(corpus, source="MID2 shipped profiles")

    unsound = None
    for _, candidate in _substitutions(
        loadout,
        nodes,
        spec,
        accept=lambda victim, trait: (
            trait.tree_index != victim.tree_index and trait.max_ranks == victim.max_ranks
        ),
    ):
        try:
            decoded = tt.decode_loadout(candidate, nodes)
        except tt.TalentDecodeError:
            continue
        repair = talentrepair.repair("warrior_arms", candidate, nodes, budget, framing)
        # Assert the premise rather than trusting it, as above: a fixture whose hash
        # was repairable, or broke no rule simc checks, would make this test pass
        # while the run took an entirely different path through `prepare`.
        if not repair.ok and not repair.soundness.ok and tt.spec_rule_violation(decoded, nodes):
            unsound = candidate
            break

    if unsound is None:
        pytest.skip("no unsound spec-rule substitution exists in this simc checkout")

    return _rewritten(profiles_dir, tmp_path_factory, "unsound", text, original, unsound)


def _args(tmp_path, profiles_dir, **kw):
    base = dict(
        tier="MID2",
        profiles=str(profiles_dir),
        simc_source=SIMC_DIR,
        simc=None,
        out=str(tmp_path),
        build="mage_arcane_sunfury",
        targets=1,
        iterations=300,
        breadth=4,
        climb_steps=1,
        seed=0,
        threads=2,
        timeout=60,
        rounds=1,
        calibrate=False,
        write_calibration=False,
        # Mirrors the real parser. The stubbed runner answers the shipped-gear
        # head-to-head like any other, so leaving it on is what exercises the path.
        shipped_gear=True,
        harvest=None,
        ptr=True,
        plan=False,
    )
    base.update(kw)
    import argparse

    return argparse.Namespace(**base)


def test_the_command_runs_end_to_end_and_writes_a_readable_document(
    tmp_path, profiles_dir, stubbed
):
    assert cli.cmd_build_search(_args(tmp_path, profiles_dir)) == 0
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert document["schemaVersion"] == 1
    row = document["specs"][0]
    assert row["id"] == "mage_arcane_sunfury"
    assert row["scenario"] == "patchwerk"
    assert row["targets"] == 1
    assert row["searched"] is True
    # Both sides present, both with an error, and the anchor beside them.
    assert row["simc"]["dpsError"] >= 0
    assert row["best"]["dpsError"] >= 0
    assert row["anchor"]["itemLevel"]
    assert row["caveats"]
    # The shipped-gear head-to-head really ran and reached the document (#72). This is
    # the only place that is provable end to end: the block is built three call frames
    # from `_publish`, and every unit test above it works on a hand-made entry. The
    # arity change that produced it was exactly the defect this file exists for, and it
    # went unnoticed by 925 unit tests.
    assert set(row["shipped"]) == {"simcDps", "bestDps", "margin", "tieBand", "separates"}
    assert row["shipped"]["simcDps"] > 0


def test_a_blind_run_measures_no_shipped_gear_margin(tmp_path, profiles_dir, stubbed):
    """`--calibrate` publishes nothing and its "simc" candidate is the SCRAMBLED build,
    so a shipped-gear margin taken there would answer a question nobody asked -- and
    would be published under simc's name if `--write-calibration` were given."""
    cli.cmd_build_search(_args(tmp_path, profiles_dir, calibrate=True, write_calibration=True))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert all("shipped" not in row for row in document["specs"])


def test_the_flag_turns_the_shipped_gear_measurement_off(tmp_path, profiles_dir, stubbed):
    """A run that skipped it must publish no block rather than an empty one: absent is
    the state the site falls back to the projection for, and a null would be read as a
    margin somebody measured as zero."""
    cli.cmd_build_search(_args(tmp_path, profiles_dir, shipped_gear=False))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert all("shipped" not in row for row in document["specs"])


def test_a_normal_run_publishes_no_calibration_block(tmp_path, profiles_dir, stubbed):
    """A head-to-head runs on every build; it is only *calibration* when blind. Naming a
    non-blind head-to-head "calibration" would be a gate grading a paper it had read."""
    cli.cmd_build_search(_args(tmp_path, profiles_dir))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert "calibration" not in document


def test_a_blind_run_publishes_the_gate_and_returns_its_verdict_as_the_exit_code(
    tmp_path, profiles_dir, stubbed
):
    """The workflow reads the exit code to decide whether to commit, so it is part of
    the contract rather than a convenience."""
    code = cli.cmd_build_search(
        _args(tmp_path, profiles_dir, calibrate=True, write_calibration=True)
    )
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    gate = document["calibration"]
    assert "fixed in advance" in gate["criterion"]
    assert code == (0 if gate["passed"] else 2)


def test_a_blind_run_publishes_nothing_unless_asked(tmp_path, profiles_dir, stubbed):
    cli.cmd_build_search(_args(tmp_path, profiles_dir, calibrate=True))
    assert not (tmp_path / "MID2" / "computed-builds.json").exists()


def test_the_document_is_rewritten_after_every_build(tmp_path, profiles_dir, stubbed):
    """A search costs CPU-hours, so being interrupted is the expected case. CLAUDE.md
    records the gear sweep claiming a per-spec write while calling the writer once after
    the loop, which left an interrupted sweep with nothing."""
    written: list[int] = []
    real = cli.cmd_build_search.__globals__["_publish"]

    def counting(*a, **kw):
        result = real(*a, **kw)
        if result is not None:
            written.append(len(json.loads(result.read_text())["specs"]))
        return result

    cli.cmd_build_search.__globals__["_publish"] = counting
    try:
        # Two builds, so a per-build write is distinguishable from a write at the end.
        cli.cmd_build_search(_args(tmp_path, profiles_dir, build="mage_arcane"))
    finally:
        cli.cmd_build_search.__globals__["_publish"] = real
    assert written == [1, 2, 2], written


def test_plan_reports_without_running_anything(tmp_path, profiles_dir, stubbed):
    assert cli.cmd_build_search(_args(tmp_path, profiles_dir, plan=True, build="")) == 0
    assert stubbed.calls == []
    assert not (tmp_path / "MID2").exists()


def test_a_build_simc_refuses_is_repaired_and_searched(tmp_path, refusable_profiles, stubbed):
    """A build whose hash simc refuses has no number anywhere on the site. The
    repaired build has to reach the document, carrying the repair's caveats.

    Driven off `refusable_profiles`, which constructs the refusal, rather than off a
    profile simc happens to ship broken -- see that fixture for why.
    """
    assert cli.cmd_build_search(_args(tmp_path, refusable_profiles, build="warrior_arms")) == 0
    row = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())["specs"][0]
    assert row["searched"] is True
    assert row["simc"]["talentHash"]
    text = " ".join(row["caveats"])
    assert "Repaired talent hash, not an optimised build" in text


def test_a_repaired_build_hands_simc_a_loadable_base_actor(
    tmp_path, refusable_profiles, stubbed, monkeypatch
):
    """**The defect a real run found and no unit test could.**

    Nothing *reads* the base actor, but simc still **builds** it -- from the profile
    file, before it generates a single profileset. For the four specs this feature
    exists for, the profile's own hash is exactly the one simc refuses, so the whole
    invocation exits 81 and takes every profileset with it:

        Error: Initialization error: Player 'MID2_Demon_Hunter_Havoc_Fel-Scarred':
        Hash '...': Node 91024 is not a choice node but has index selection.

    Verifying a repaired hash by hand as ``simc PROFILE talents=HASH`` proves nothing
    about this, because that overrides the profile and the pipeline reaches simc by a
    different route. So the assertion is on what the runner is *handed*.
    """
    seen: dict = {}
    real = buildsearch.simc_runner

    def capture(*args, **kwargs):
        seen["base_talents"] = kwargs.get("base_talents")
        return stubbed

    monkeypatch.setattr(buildsearch, "simc_runner", capture)
    cli.cmd_build_search(_args(tmp_path, refusable_profiles, build="warrior_arms"))
    del real
    assert seen["base_talents"], "a repaired build must hand simc a loadable base actor"

    seen.clear()
    cli.cmd_build_search(_args(tmp_path, refusable_profiles, build="mage_arcane_sunfury"))
    # A profile simc already accepts needs no override: simc reads it from the file.
    assert seen["base_talents"] is None


def test_a_build_whose_decode_cannot_be_trusted_is_published_as_unsearched(
    tmp_path, unsound_profiles, stubbed
):
    """A hash this reader cannot follow is published as unsearched, with the reason.

    Repairing a decode the soundness screen rejects would produce a valid hash for a
    build nobody wrote, so nothing is searched. The row then says which of the site's
    absence states applies rather than leaving a gap that reads like a bad result.

    The subject is constructed -- see `unsound_profiles` for why it can no longer be
    borrowed from simc, and for why the construction is a cross-tree substitution
    rather than a padded string.
    """
    assert cli.cmd_build_search(_args(tmp_path, unsound_profiles, build="warrior_arms")) == 0
    row = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())["specs"][0]
    assert row["searched"] is False
    assert row["best"] is None
    assert any("No search ran for this build" in c for c in row["caveats"])


def test_a_head_to_head_that_aborts_simc_costs_its_build_and_not_the_pass(
    tmp_path, profiles_dir, monkeypatch
):
    """One build's simc abort must not end the run, and the run must keep what it did.

    Measured on run 32989652220: a ten-target pass died after 2h26m with
    ``simc exited -6 for monk_windwalker_default__patchwerk__t10``. Ten builds had
    already been searched and their rows written -- `_publish` runs after every build
    for exactly this reason -- and the workflow's Commit step is `success()`-gated, so
    all of it was discarded.

    `run_build` has been inside a per-build guard since #38. `_head_to_head` was added
    afterwards (#74) and sat outside it. That is the same shape as `PointBudgetExhausted`
    escaping `_public_first_kills` and `harvest_encounter`, both already in CLAUDE.md:
    a new call placed one line outside a guard that already existed.
    """
    from wowdps import simc_runner

    stub = _StubSimc()
    plain = stub.__call__

    def abort_on_the_head_to_head(candidates, iterations):
        # `_head_to_head` is the only caller that names a candidate `simcbuild`, so
        # this fails precisely that invocation and lets the search itself succeed.
        if any(c.key == "simcbuild" for c in candidates):
            raise simc_runner.SimcError("simc exited -6 for monk_windwalker_default")
        return plain(candidates, iterations)

    monkeypatch.setattr(buildsearch, "simc_runner", lambda *a, **k: abort_on_the_head_to_head)
    monkeypatch.setattr(cli.simc_runner, "find_simc", lambda explicit=None: pathlib.Path("/simc"))

    assert cli.cmd_build_search(_args(tmp_path, profiles_dir)) == 0

    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    row = document["specs"][0]
    # Published as unsearched WITH the reason -- not as a winner with no baseline,
    # which is what a margin measured against nothing would be.
    assert row["searched"] is False
    assert row["simc"] is None
    assert any("simc exited -6" in caveat for caveat in row["caveats"])
