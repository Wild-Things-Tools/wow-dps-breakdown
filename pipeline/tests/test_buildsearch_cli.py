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

import json
import os
import pathlib

import pytest

from wowdps import buildsearch, cli

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


def _args(tmp_path, **kw):
    base = dict(
        tier="MID2",
        profiles=str(pathlib.Path(SIMC_DIR) / "profiles"),
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


def test_the_command_runs_end_to_end_and_writes_a_readable_document(tmp_path, stubbed):
    assert cli.cmd_build_search(_args(tmp_path)) == 0
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


def test_a_blind_run_measures_no_shipped_gear_margin(tmp_path, stubbed):
    """`--calibrate` publishes nothing and its "simc" candidate is the SCRAMBLED build,
    so a shipped-gear margin taken there would answer a question nobody asked -- and
    would be published under simc's name if `--write-calibration` were given."""
    cli.cmd_build_search(_args(tmp_path, calibrate=True, write_calibration=True))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert all("shipped" not in row for row in document["specs"])


def test_the_flag_turns_the_shipped_gear_measurement_off(tmp_path, stubbed):
    """A run that skipped it must publish no block rather than an empty one: absent is
    the state the site falls back to the projection for, and a null would be read as a
    margin somebody measured as zero."""
    cli.cmd_build_search(_args(tmp_path, shipped_gear=False))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert all("shipped" not in row for row in document["specs"])


def test_a_normal_run_publishes_no_calibration_block(tmp_path, stubbed):
    """A head-to-head runs on every build; it is only *calibration* when blind. Naming a
    non-blind head-to-head "calibration" would be a gate grading a paper it had read."""
    cli.cmd_build_search(_args(tmp_path))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    assert "calibration" not in document


def test_a_blind_run_publishes_the_gate_and_returns_its_verdict_as_the_exit_code(tmp_path, stubbed):
    """The workflow reads the exit code to decide whether to commit, so it is part of
    the contract rather than a convenience."""
    code = cli.cmd_build_search(_args(tmp_path, calibrate=True, write_calibration=True))
    document = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())
    gate = document["calibration"]
    assert "fixed in advance" in gate["criterion"]
    assert code == (0 if gate["passed"] else 2)


def test_a_blind_run_publishes_nothing_unless_asked(tmp_path, stubbed):
    cli.cmd_build_search(_args(tmp_path, calibrate=True))
    assert not (tmp_path / "MID2" / "computed-builds.json").exists()


def test_the_document_is_rewritten_after_every_build(tmp_path, stubbed):
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
        cli.cmd_build_search(_args(tmp_path, build="mage_arcane"))
    finally:
        cli.cmd_build_search.__globals__["_publish"] = real
    assert written == [1, 2, 2], written


def test_plan_reports_without_running_anything(tmp_path, stubbed):
    assert cli.cmd_build_search(_args(tmp_path, plan=True, build="")) == 0
    assert stubbed.calls == []
    assert not (tmp_path / "MID2").exists()


def test_a_build_simc_refuses_is_repaired_and_searched(tmp_path, stubbed):
    """Arms Warrior has no number anywhere on the site because simc refuses its hash.
    The repaired build has to reach the document, carrying the repair's caveats."""
    assert cli.cmd_build_search(_args(tmp_path, build="warrior_arms")) == 0
    row = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())["specs"][0]
    assert row["searched"] is True
    assert row["simc"]["talentHash"]
    text = " ".join(row["caveats"])
    assert "Repaired talent hash, not an optimised build" in text


def test_a_repaired_build_hands_simc_a_loadable_base_actor(tmp_path, stubbed, monkeypatch):
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
    cli.cmd_build_search(_args(tmp_path, build="warrior_arms"))
    del real
    assert seen["base_talents"], "a repaired build must hand simc a loadable base actor"

    seen.clear()
    cli.cmd_build_search(_args(tmp_path, build="mage_arcane_sunfury"))
    # A profile simc already accepts needs no override: simc reads it from the file.
    assert seen["base_talents"] is None


def test_a_build_whose_decode_cannot_be_trusted_is_published_as_unsearched(tmp_path, stubbed):
    """Retribution's hash reads a tree that has changed shape under it. Repairing it
    would be a valid hash for a build nobody wrote, so nothing is searched -- and the
    row says which of the site's absence states applies, with the reason."""
    assert cli.cmd_build_search(_args(tmp_path, build="paladin_retribution_default")) == 0
    row = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())["specs"][0]
    assert row["searched"] is False
    assert row["best"] is None
    assert any("No search ran for this build" in c for c in row["caveats"])
