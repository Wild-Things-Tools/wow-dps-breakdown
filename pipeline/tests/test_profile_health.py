"""Whether a tier's profiles still build an actor.

The reason this is code rather than a shell loop somebody rewrites each season:
"old-tier profiles rot" is a fact the project acts on -- the previous tier is kept
off the schedule because of it -- and a fact that decides a schedule should be
re-measurable in one command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from wowdps.profiles import SpecProfile, check_loads


def a_profile(tmp_path: Path) -> SpecProfile:
    path = tmp_path / "MID1_Mage_Fire.simc"
    path.write_text("mage=x\n", encoding="utf-8")
    return SpecProfile(
        path=path,
        tier="MID1",
        wow_class="Mage",
        spec="Fire",
        hero_talent=None,
        role="spell",
        talent_hash="C8DA",
    )


def fake_run(monkeypatch, stdout: str, stderr: str = ""):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)


def test_a_clean_run_loads(tmp_path, monkeypatch):
    fake_run(monkeypatch, "Simulating... ( iterations=1 )\nDPS: 100\n")
    assert check_loads(Path("simc"), a_profile(tmp_path)).loads is True


def test_both_shapes_of_talent_rot_are_recognised(tmp_path, monkeypatch):
    """simc words this two ways and both mean the hash no longer fits the tree.

    Measured on MID1 against simc 1210-01: 15 profiles fail, 9 with the first
    message and 6 with the second. A check that knew only the first would report
    the tier as mostly healthy.
    """
    first = (
        "Error: Initialization error: Player 'MID1_Mage_Fire': Hash 'C8DA': "
        "Selected node 5 entry 7 is not available to player's spec."
    )
    second = (
        "Error: Initialization error: Player 'MID1_Paladin_Retribution': Hash 'CYEA': "
        "Node 81527 is not a choice node but has index selection."
    )
    for message in (first, second):
        fake_run(monkeypatch, message)
        health = check_loads(Path("simc"), a_profile(tmp_path))
        assert health.loads is False
        assert health.rotten_talents is True


def test_a_failure_that_is_not_talent_rot_is_reported_but_not_counted_as_rot(tmp_path, monkeypatch):
    fake_run(monkeypatch, "Error: Setup failure: Sim setup: Option 'json2': Missing file name.")
    health = check_loads(Path("simc"), a_profile(tmp_path))
    assert health.loads is False
    assert health.rotten_talents is False
    assert "Missing file name" in (health.reason or "")


def test_the_check_never_passes_an_empty_json2(tmp_path, monkeypatch):
    """`html=` empty is fine and `json2=` empty is a setup failure, so a check that
    passes both reports every profile in the game as broken. That shipped once."""
    seen: list[list[str]] = []

    def run(*args, **kwargs):
        seen.append(list(args[0]))
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", run)
    check_loads(Path("simc"), a_profile(tmp_path))

    assert "html=" in seen[0]
    assert not any(arg.startswith("json2") for arg in seen[0])
    assert "iterations=1" in seen[0]
