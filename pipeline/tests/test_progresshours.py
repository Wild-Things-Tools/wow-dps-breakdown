"""Progress hours, pinned against hand-written payloads.

Shapes follow the live schema as introspected on 2026-08-26: `Report.startTime` is
absolute, `ReportFight.startTime`/`endTime` are relative to their own report.
"""

from wowdps.progresshours import (
    BossProgress,
    encounter_zone,
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


# --------------------------------------------------------------------------------
# The three defects the first successful live run exposed (2026-08-26, MID1 Mythic).
#
# That run measured -- no `error` refusals at all -- and every number in it was
# wrong, which is the harder failure to see. Medians came out at 0.09-0.30 HOURS,
# i.e. five to eighteen minutes, for first-killing a Mythic boss.
# --------------------------------------------------------------------------------


def test_the_zone_is_read_out_of_the_encounter_and_zero_is_not_a_zone():
    """`zoneID: 0` is not a narrower question, it is a different one.

    `fight_profiles.json` carries no `zoneId` for ANY tier, so the run's
    `block.get("zoneId") or args.zone or 0` resolved to 0 on every query -- and
    Warcraft Logs accepted it and answered with each guild's reports across all
    content. The third disguise of a trap this repo has recorded twice already
    (`hostilityType`, `includeResources`): after an omitted argument comes a *zero*
    one, and a zero is a value the service may interpret.
    """
    payload = {"worldData": {"encounter": {"id": 3176, "zone": {"id": 46, "name": "VS"}}}}
    assert encounter_zone(payload) == 46

    # Each of these must be None rather than 0, because the caller's whole job is to
    # tell "no zone" from a zone, and 0 is what made the failing run look healthy.
    assert encounter_zone({"worldData": {"encounter": {"zone": {"id": 0}}}}) is None
    assert encounter_zone({"worldData": {"encounter": {"zone": None}}}) is None
    assert encounter_zone({"worldData": {"encounter": None}}) is None
    assert encounter_zone({}) is None


def test_the_attempt_count_is_published_beside_the_hours():
    """The cheapest thing that separates a progress kill from a FARM kill.

    The live run's document had no field that could say "these medians are one pull",
    so a chart drawn from it would have read as progress time. A boss whose median
    attempts is 1 was not progressed in the window that was read, whatever the hours
    say -- and that is now legible from the document alone.
    """
    boss = BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC, hours=[0.12, 0.15], attempts=[1, 1])
    row = boss.to_json()
    assert row["medianAttempts"] == 1.0

    progressed = BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC, hours=[9.0, 11.0], attempts=[41, 63])
    assert progressed.to_json()["medianAttempts"] == 52.0

    # Absent, not zero: no guild measured means no attempt count, and a 0 there would
    # read as "killed without attempting it".
    assert BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC).to_json()["medianAttempts"] is None


def test_the_zone_it_searched_is_published_so_a_wrong_scope_is_visible():
    """The failing run scoped every query to zone 0 and said so nowhere."""
    assert BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC, zone_id=46).to_json()["zoneId"] == 46
    assert BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC).to_json()["zoneId"] is None


# --------------------------------------------------------------------------------
# Attributing the refusal rate. The 2026-08-26 20-guild run refused 4 to 12 of 20
# guilds per boss on the same zone -- and the document could not say which of two
# opposite things had happened.
# --------------------------------------------------------------------------------


def test_an_empty_listing_and_a_listing_without_this_boss_are_different_refusals():
    """`no-reports` is about the guild; `no-fights` is about the boss.

    Under one name the two are indistinguishable, and the whole per-boss spread (1 in
    8 refused on one boss, 7 in 8 on another, same zone, same guilds) cannot be
    attributed. An empty listing would refuse identically on all nine bosses; a
    listing that holds reports but no pull of *this* boss would not.
    """
    empty = pull_time([], ENCOUNTER, MYTHIC)
    assert empty.reason == "no-reports"
    assert empty.reports == 0

    elsewhere = report(0, [fight(0, 1000, kill=True, encounter=999)])
    other_boss = pull_time([elsewhere], ENCOUNTER, MYTHIC)
    assert other_boss.reason == "no-fights"
    # The count is the point: reports WERE read, they just held nothing for this boss.
    assert other_boss.reports == 1


def test_the_report_count_is_published_so_the_split_survives_in_the_artifact():
    """A run's log is not durable tracking; the document has to carry the answer."""
    boss = BossProgress(ENCOUNTER, "A Boss", 1, MYTHIC, reports_seen=[0, 40, 12, 30])
    assert boss.to_json()["medianReportsSeen"] == 21.0
    # Absent rather than 0: no guild sampled means no denominator, and a 0 there would
    # read as "every guild's listing was empty".
    assert BossProgress(ENCOUNTER, "A", 1, MYTHIC).to_json()["medianReportsSeen"] is None


def test_every_sampled_guild_is_named_with_its_outcome():
    """The cross-boss join the refusal rate needs, and it needs guild ids to exist.

    Guild ids are public Warcraft Logs data. The rule this project keeps about never
    collecting names is about *characters* -- a build is not a person -- and a guild
    id is the join key, not an identity.
    """
    boss = BossProgress(
        ENCOUNTER,
        "A Boss",
        1,
        MYTHIC,
        guilds=[
            {"id": 1, "outcome": "measured", "reportsSeen": 40, "hours": 2.5, "attempts": 30},
            {"id": 2, "outcome": "no-reports", "reportsSeen": 0},
            {"id": 3, "outcome": "no-fights", "reportsSeen": 55},
        ],
    )
    rows = boss.to_json()["guilds"]
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert {r["outcome"] for r in rows} == {"measured", "no-reports", "no-fights"}
