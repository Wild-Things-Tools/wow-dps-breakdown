"""Asking simc to write a profile back out, and the two ways that can fail quietly.

`regenerate_profile` exists for one question -- what primary stat is a materialised
profile built around -- and the reason it is a simc call rather than a table here is
in its own docstring. What this file pins is the *failure* handling, because both
failure modes return a plausible string rather than an error unless something checks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wowdps import simc_runner


def a_profile(tmp_path: Path) -> Path:
    path = tmp_path / "MID2_Paladin_Retribution.simc"
    path.write_text("paladin=X\nspec=retribution\n", encoding="utf-8")
    return path


def fake_simc(monkeypatch, *, returncode: int = 0, writes: str | None = None, stderr: str = ""):
    """Stand in for simc, writing to whatever path the `save=` option names."""
    seen: list[list[str]] = []

    def run(*args, **kwargs):
        cmd = list(args[0])
        seen.append(cmd)
        if writes is not None:
            target = next(a.split("=", 1)[1] for a in cmd if a.startswith("save="))
            Path(target).write_text(writes, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_the_export_is_returned_and_the_options_follow_the_profile(tmp_path, monkeypatch):
    """`save=` after the profile path, as every other option in this module is.

    Ahead of it, it would be overridden by the profile rather than overriding it --
    the same ordering rule that decides whether `talents=` bites.
    """
    body = "paladin=X\n# Gear Summary\n# gear_strength=1361\n"
    seen = fake_simc(monkeypatch, writes=body)

    assert simc_runner.regenerate_profile(Path("simc"), a_profile(tmp_path)) == body

    cmd = seen[0]
    assert cmd[1].endswith("MID2_Paladin_Retribution.simc")
    assert cmd[2].startswith("save=")


def test_a_refused_profile_raises_rather_than_returning_nothing(tmp_path, monkeypatch):
    """simc exits 81 on a talent hash its own parser refuses -- MID2_Paladin_Retribution
    does, measured 2026-08-30 -- and writes no file. The exit code is the loud half."""
    fake_simc(monkeypatch, returncode=81, stderr="Hash 'C8': Node 91020 is not a choice node.")

    with pytest.raises(simc_runner.SimcError, match="81"):
        simc_runner.regenerate_profile(Path("simc"), a_profile(tmp_path))


def test_an_exit_of_zero_that_wrote_no_file_is_still_a_refusal(tmp_path, monkeypatch):
    """The quiet half, and the one that produced four identical wrong answers.

    A caller reusing one output path across profiles reads the PREVIOUS profile's
    export when this one writes nothing, and a strength spec duly reported intellect.
    A fresh directory per call makes that read a missing file; this makes the missing
    file an error instead of an empty string with a plausible shape.
    """
    fake_simc(monkeypatch, returncode=0, writes=None)

    with pytest.raises(simc_runner.SimcError, match="wrote no profile"):
        simc_runner.regenerate_profile(Path("simc"), a_profile(tmp_path))


def test_two_calls_never_share_an_output_path(tmp_path, monkeypatch):
    """The structural half of the same fix: even a caller that ignores the errors
    above cannot read one profile's export as another's."""
    seen = fake_simc(monkeypatch, writes="# gear_strength=1\n")

    simc_runner.regenerate_profile(Path("simc"), a_profile(tmp_path))
    simc_runner.regenerate_profile(Path("simc"), a_profile(tmp_path))

    paths = [next(a for a in cmd if a.startswith("save=")) for cmd in seen]
    assert paths[0] != paths[1]
