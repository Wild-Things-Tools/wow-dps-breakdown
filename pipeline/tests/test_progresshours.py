"""Progress hours, pinned against hand-written payloads.

Shapes follow the live schema as introspected on 2026-08-26: `Report.startTime` is
absolute, `ReportFight.startTime`/`endTime` are relative to their own report.
"""

from wowdps.progresshours import (
    BossProgress,
    fight_duration_ms,
    median,
    ordered_fights,
    pull_time,
    quartiles,
    stacked_total,
)

ENCOUNTER = 3180
MYTHIC = 5


def fight(start, end, kill=False, encounter=ENCOUNTER, difficulty=MYTHIC):
    return {
        "startTime": start,
        "endTime": end,
        "kill": kill,
        "encounterID": encounter,
        "difficulty": difficulty,
    }


def report(start, fights):
    return {"startTime": start, "fights": fights}


def test_progress_time_is_every_attempt_up_to_and_including_the_kill():
    reports = [
        report(1_000, [fight(0, 60_000), fight(70_000, 130_000)]),
        report(2_000, [fight(0, 90_000, kill=True), fight(100_000, 160_000)]),
    ]
    result = pull_time(reports, ENCOUNTER, MYTHIC)
    assert result.ms == 210_000  # the pull AFTER the kill is not progress time
    assert result.attempts == 3


def test_reports_are_ordered_by_their_own_absolute_start():
    """`ReportFight.startTime` is relative to its report. Sorting all fights on it
    interleaves raid nights and moves where the first kill falls -- and the total
    still looks plausible, which is what makes it dangerous."""
    later = report(9_000, [fight(0, 10_000, kill=True)])
    first = report(1_000, [fight(5_000, 105_000)])
    result = pull_time([later, first], ENCOUNTER, MYTHIC)
    assert result.ms == 110_000
    assert result.ms != 10_000  # what sorting on fight startTime would answer


def test_a_window_with_no_kill_is_refused_rather_than_summed():
    reports = [report(1_000, [fight(0, 60_000), fight(70_000, 130_000)])]
    result = pull_time(reports, ENCOUNTER, MYTHIC)
    assert result.ms is None and result.reason == "no-kill"


def test_another_difficulty_never_counts_toward_this_one():
    reports = [report(1_000, [fight(0, 600_000, difficulty=4), fight(700_000, 760_000, kill=True)])]
    assert pull_time(reports, ENCOUNTER, MYTHIC).ms == 60_000


def test_a_fight_stating_no_difficulty_is_kept():
    """Unknown is not wrong; dropping it loses real attempts silently."""
    reports = [
        report(1_000, [fight(0, 60_000, difficulty=None), fight(70_000, 130_000, kill=True)])
    ]
    assert pull_time(reports, ENCOUNTER, MYTHIC).ms == 120_000


def test_a_non_positive_duration_is_refused_not_clamped():
    assert fight_duration_ms(fight(100, 100)) is None
    assert fight_duration_ms(fight(200, 100)) is None
    assert fight_duration_ms(fight(0, 5_000)) == 5_000


def test_ordered_fights_keeps_within_report_order():
    reports = [report(1_000, [fight(0, 10), fight(50, 90)])]
    assert [f["startTime"] for f in ordered_fights(reports, ENCOUNTER, MYTHIC)] == [0, 50]


def test_median_ignores_the_guilds_with_no_answer():
    assert median([100, None, 300, None]) == 200
    assert median([]) is None


def test_quartiles_refuse_a_sample_too_thin_to_describe_a_spread():
    assert quartiles([1, 2, 3]) is None
    assert quartiles([1, 2, 3, 4]) == (1.5, 3.5)


def test_a_boss_nobody_could_be_measured_for_publishes_null_not_zero():
    """A stacked column must not draw "unmeasured" as a segment of zero height --
    that reads as a boss that cost nothing."""
    row = BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC).to_json()
    assert row["medianHours"] is None
    assert row["sample"] == 0


def test_the_season_total_refuses_when_any_boss_is_unmeasured():
    """A column built from six of eight bosses is shorter than one built from eight
    and reads as a CHEAPER SEASON -- which is the exact comparison the chart is for."""
    measured = BossProgress(1, "A", 1, MYTHIC, hours=[2.0])
    unmeasured = BossProgress(2, "B", 2, MYTHIC)
    assert stacked_total([measured]) == 2.0
    assert stacked_total([measured, unmeasured]) is None
    assert stacked_total([]) is None
