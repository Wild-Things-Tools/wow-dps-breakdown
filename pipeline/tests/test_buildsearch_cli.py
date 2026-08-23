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


def test_a_build_whose_decode_cannot_be_trusted_is_published_as_unsearched(tmp_path, stubbed):
    """Retribution's hash reads a tree that has changed shape under it. Repairing it
    would be a valid hash for a build nobody wrote, so nothing is searched -- and the
    row says which of the site's absence states applies, with the reason."""
    assert cli.cmd_build_search(_args(tmp_path, build="paladin_retribution_default")) == 0
    row = json.loads((tmp_path / "MID2" / "computed-builds.json").read_text())["specs"][0]
    assert row["searched"] is False
    assert row["best"] is None
    assert any("No search ran for this build" in c for c in row["caveats"])
