"""The arithmetic of the projection check, without a binary.

What is pinned here is the part that decides what the run *says*: which builds are
worth measuring, when two margins genuinely disagree, and the refusals. The
measurement itself needs simc and is not simulated -- a stub that returned numbers
would only be testing the stub.
"""

import math

from wowdps import projectioncheck as pc


class M:
    """A Measurement, as `buildsearchrun.measure` returns them."""

    def __init__(self, dps: float, dps_error: float = 0.05):
        self.dps = dps
        self.dps_error = dps_error


def entry(build_id: str, simc_dps: float, best_dps: float, error: float = 0.05) -> dict:
    return {
        "id": build_id,
        "simc": {"dps": simc_dps, "dpsError": error, "talentHash": "AAA"},
        "best": {"dps": best_dps, "dpsError": error, "talentHash": "BBB"},
    }


def test_only_builds_the_site_draws_a_projection_for_are_measured():
    """The rule is `bestBuild.ts`'s, restated: a tie is drawn as simc's own build and
    carries no projection, so measuring it would answer a question nobody asked."""
    document = {
        "specs": [
            entry("clear_winner", 100_000, 102_000),
            entry("inside_the_band", 100_000, 100_020),
            entry("loser", 100_000, 98_000),
        ]
    }
    assert [e["id"] for e in pc.marked_builds(document)] == ["clear_winner"]


def test_a_row_missing_a_side_is_skipped_rather_than_half_measured():
    document = {
        "specs": [
            {"id": "no_best", "simc": {"dps": 100_000, "dpsError": 0.05}},
            {"id": "no_simc", "best": {"dps": 100_000, "dpsError": 0.05}},
            {"id": "zero_simc", "simc": {"dps": 0}, "best": {"dps": 1}},
        ]
    }
    assert pc.marked_builds(document) == []


def test_two_margins_that_agree_do_not_separate():
    """The projection holding is the outcome that must not be manufactured: identical
    margins have to come back as a tie however tight the errors are."""
    anchored = {"simc": M(100_000), "best": M(102_000)}
    shipped = {"simc": M(120_000), "best": M(122_400)}  # same +2.00%
    row = pc.compare("b", anchored, shipped, published_margin=0.02)
    assert row is not None
    assert abs(row.difference) < 1e-12
    assert row.separates is False
    assert row.reproduced is True


def test_a_real_disagreement_separates_and_keeps_its_direction():
    """Direction is load-bearing: over- and under-stating lead to different fixes, so
    "they disagree" is not a sufficient answer."""
    anchored = {"simc": M(100_000), "best": M(102_000)}  # +2.00%
    shipped = {"simc": M(100_000), "best": M(100_500)}  # +0.50%
    row = pc.compare("b", anchored, shipped, published_margin=0.02)
    assert row is not None
    assert row.separates is True
    assert row.difference < 0  # the projection overstates
    assert "overstates" in pc.verdict([row])


def test_the_band_grows_with_every_error_that_went_into_it():
    """Four measurements make each comparison, so four errors do -- not two. Using
    only the shipped pair's errors would call jitter a finding."""
    tight = pc.compare(
        "b",
        {"simc": M(100_000, 0.01), "best": M(102_000, 0.01)},
        {"simc": M(100_000, 0.01), "best": M(102_000, 0.01)},
        None,
    )
    loose = pc.compare(
        "b",
        {"simc": M(100_000, 0.5), "best": M(102_000, 0.5)},
        {"simc": M(100_000, 0.5), "best": M(102_000, 0.5)},
        None,
    )
    assert tight is not None and loose is not None
    assert loose.band > tight.band
    assert math.isclose(loose.band, math.hypot(math.hypot(0.005, 0.005), math.hypot(0.005, 0.005)))


def test_failing_to_reproduce_the_published_margin_is_reported_separately():
    """The control. A run that cannot reproduce what was published is measuring
    something else, and its second number means nothing -- so this must not be folded
    into `separates`, which would read as a finding about the projection."""
    anchored = {"simc": M(100_000), "best": M(102_000)}  # this run says +2.00%
    shipped = {"simc": M(100_000), "best": M(102_000)}
    row = pc.compare("b", anchored, shipped, published_margin=0.05)  # the document said +5%
    assert row is not None
    assert row.reproduced is False
    assert row.separates is False  # the projection question itself is untouched


def test_no_published_margin_means_unknown_rather_than_failed():
    row = pc.compare(
        "b",
        {"simc": M(100_000), "best": M(102_000)},
        {"simc": M(100_000), "best": M(102_000)},
        None,
    )
    assert row is not None
    assert row.reproduced is None
    assert "reproducedPublished" not in row.to_json()


def test_a_missing_measurement_is_a_refusal_not_a_smaller_claim():
    """A margin computed against a measurement that never happened is a wrong number,
    not a thin one."""
    assert pc.compare("b", {"simc": M(1.0)}, {"simc": M(1.0), "best": M(1.0)}, None) is None
    assert pc.compare("b", {"simc": M(1.0), "best": M(1.0)}, {"best": M(1.0)}, None) is None
    assert (
        pc.compare("b", {"simc": M(0.0), "best": M(1.0)}, {"simc": M(1.0), "best": M(1.0)}, None)
        is None
    )


def test_a_run_that_compared_nothing_says_so_rather_than_claiming_the_projection_holds():
    """The failure direction that matters. "No build disagreed" and "no build was
    measured" produce the same empty list, and only one of them is evidence."""
    assert "says nothing" in pc.verdict([])
    assert "holds" not in pc.verdict([])
