"""Progress hours, pinned against hand-written payloads.

Shapes follow the live schema as introspected on 2026-08-26: `Report.startTime` is
absolute, `ReportFight.startTime`/`endTime` are relative to their own report.
"""

from wowdps.progresshours import (
    Attempt,
    BossProgress,
    encounter_zone,
    median,
    ordered_attempts,
    partition_nights,
    pull_time,
    quartiles,
    ranking_rows,
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


#: Two raid nights, a day apart. **The gap is load-bearing and used not to be.**
#:
#: This fixture read `1_000` and `2_000` while attempts were ordered per report and
#: only compared within one. Once attempts moved onto the absolute clock those bases
#: describe two reports starting one second apart with pulls running for two minutes
#: -- so the second report's kill correctly lands BETWEEN the first report's two
#: pulls, and the sum drops to 150_000. The code was right and the fixture was
#: impossible. Give report bases real distance, or a test states physics that cannot
#: happen and then pins the answer to it.
NIGHT_1 = 1_700_000_000_000
NIGHT_2 = NIGHT_1 + 86_400_000


def test_progress_time_is_every_attempt_up_to_and_including_the_kill():
    reports = [
        report(NIGHT_1, [fight(0, 60_000), fight(70_000, 130_000)]),
        report(NIGHT_2, [fight(0, 90_000, kill=True), fight(100_000, 160_000)]),
    ]
    result = pull_time(reports, ENCOUNTER, MYTHIC)
    assert result.ms == 210_000  # the pull AFTER the kill is not progress time
    assert result.attempts == 3
    assert result.nights == 2


def test_two_overlapping_reports_interleave_on_the_absolute_clock():
    """A split raid logging two reports at once has ONE first kill: the earlier one.

    Ordering per report and only comparing within one would count the other team's
    pulls after that kill as progression toward it. This is the case the absolute
    clock exists for, and it is the reason the fixture above needed real dates.
    """
    reports = [
        report(NIGHT_1, [fight(0, 60_000), fight(3_600_000, 3_660_000)]),
        report(NIGHT_1 + 120_000, [fight(0, 90_000, kill=True)]),
    ]
    result = pull_time(reports, ENCOUNTER, MYTHIC)
    # 60s of team A, then team B's kill 120s in. Team A's later pull is not progress.
    assert result.ms == 150_000
    assert result.attempts == 2
    assert result.kill_at == NIGHT_1 + 120_000


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
    """The rule moved from `fight_duration_ms` onto `Attempt`; it did not go away.

    A zero reads as a very fast pull while shrinking the total, and a negative one
    shrinks it twice, so `pull_time` must skip both rather than add them.
    """
    assert Attempt(100.0, 100.0, False).duration_ms == 0
    assert Attempt(200.0, 100.0, False).duration_ms < 0
    assert Attempt(0.0, 5_000.0, False).duration_ms == 5_000
    # And the sum built from them ignores exactly those.
    reports = [report(1_000, [fight(0, 0), fight(10, 5), fight(100, 5_100, kill=True)])]
    answer = pull_time(reports, ENCOUNTER, MYTHIC)
    assert answer.ms == 5_000
    assert answer.attempts == 3 and answer.usable_attempts == 1


def test_attempts_are_put_on_the_absolute_clock():
    """`ReportFight.startTime` is report-relative; every reader here needs absolute.

    Two reports whose fights both start at 0 are hours apart in reality, and a reader
    that keeps the relative number cannot order them, partition nights, or compare a
    kill against the ranking.
    """
    reports = [report(1_000, [fight(0, 10), fight(50, 90)])]
    attempts, dateless = ordered_attempts(reports, ENCOUNTER, MYTHIC)
    assert dateless == 0
    assert [a.start_ms for a in attempts] == [1_000, 1_050]


def test_a_report_with_no_start_time_is_refused_rather_than_sorted_to_zero():
    """It used to sort FIRST, ahead of every real report.

    If such a report held a kill it terminated the sum immediately and the guild
    published a near-zero progress time -- the same shape as the farm-kill bug.
    """
    dateless = {"code": "d", "fights": [fight(0, 60_000, kill=True)]}
    real = report(1_000_000_000_000, [fight(0, 3_600_000), fight(4_000_000, 7_600_000, kill=True)])
    attempts, skipped = ordered_attempts([dateless, real], ENCOUNTER, MYTHIC)
    assert skipped == 1
    assert len(attempts) == 2
    assert pull_time([dateless, real], ENCOUNTER, MYTHIC).ms == 7_200_000


def test_a_listing_of_nothing_but_dateless_reports_says_so():
    """Distinct from `no-fights`: one is a payload problem, the other is about the boss."""
    dateless = {"code": "d", "fights": [fight(0, 60_000)]}
    assert pull_time([dateless], ENCOUNTER, MYTHIC).reason == "no-report-time"


def test_nights_partition_what_was_observed_and_never_invent_one():
    day = 86_400_000
    same_night = [Attempt(0, 1, False), Attempt(3_600_000, 2, False)]
    assert partition_nights(same_night) == 1
    assert partition_nights([Attempt(0, 1, False), Attempt(day, 2, False)]) == 2
    assert partition_nights([]) == 0


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


# ── The completeness screens ─────────────────────────────────────────────────
#
# Everything below pins behaviour that did not exist before 2026-08-27. Each case
# published a confident, plausible number until the screens were added, which is why
# they are stated as tests rather than described in a docstring.

RANKED_KILL = NIGHT_2 + 90_000


def ranking(guild_id, kill_time=RANKED_KILL, from_log=1):
    row = {"guild": {"id": guild_id}}
    if kill_time is not None:
        row["killTime"] = kill_time
    if from_log is not None:
        row["fromlog"] = from_log
    return row


def payload(rows):
    return {"worldData": {"encounter": {"fightRankings": {"rankings": rows}}}}


def test_a_ranking_row_carries_the_kill_time_and_whether_it_came_from_a_log():
    kills, missing = ranking_rows(payload([ranking(7), ranking(8, from_log=0)]))
    assert missing == 0
    assert [k.guild_id for k in kills] == [7, 8]
    assert [k.from_log for k in kills] == [True, False]
    assert kills[0].kill_time_ms == RANKED_KILL


def test_a_row_with_no_guild_is_counted_rather_than_silently_shortening_the_sample():
    """`guild.id` is null on roughly 4% of live rows, mostly CN."""
    kills, missing = ranking_rows(payload([{"killTime": 1, "fromlog": 1}, ranking(7)]))
    assert missing == 1 and [k.guild_id for k in kills] == [7]


def test_a_row_stating_neither_screen_field_fails_closed():
    """Absence must not read as "passed".

    A renamed field would otherwise switch both screens off while every published
    number still looked healthy -- this repository's signature defect.
    """
    kills, _ = ranking_rows(payload([ranking(7, kill_time=None, from_log=None)]))
    assert kills[0].from_log is None
    assert kills[0].kill_time_ms is None


def test_a_kill_the_log_never_saw_is_refused_rather_than_replaced_by_a_farm_kill():
    """The guild killed it on an unlogged night; the earliest LOGGED kill is farm.

    Before the screen this published 4 attempts and every pull before that farm kill
    as progression toward a kill that had already happened.
    """
    real_kill = NIGHT_1 + 3_600_000
    reports = [
        report(NIGHT_1, [fight(0, 60_000), fight(70_000, 130_000)]),
        report(NIGHT_2, [fight(0, 60_000), fight(70_000, 130_000, kill=True)]),
    ]
    assert pull_time(reports, ENCOUNTER, MYTHIC).ms == 240_000  # what it used to say
    assert pull_time(reports, ENCOUNTER, MYTHIC, real_kill).reason == "kill-too-late"


def test_a_log_holding_only_farm_nights_is_refused():
    """One wipe and one kill published as a guild's whole progression."""
    reports = [report(NIGHT_2, [fight(0, 60_000), fight(70_000, 130_000, kill=True)])]
    assert pull_time(reports, ENCOUNTER, MYTHIC).ms == 120_000  # what it used to say
    ranked_weeks_earlier = NIGHT_1 - 14 * 86_400_000
    assert pull_time(reports, ENCOUNTER, MYTHIC, ranked_weeks_earlier).reason == "kill-too-late"


def test_a_logged_kill_earlier_than_the_ranked_one_is_a_finding_not_a_correction():
    """The log and the ranking disagree. Refused and counted APART.

    Picking a winner here would bury exactly the disagreement worth knowing about.
    """
    reports = [report(NIGHT_1, [fight(0, 60_000, kill=True)])]
    later = NIGHT_2 + 5 * 86_400_000
    assert pull_time(reports, ENCOUNTER, MYTHIC, later).reason == "kill-too-early"


def test_a_kill_inside_the_tolerance_still_measures():
    """The tolerance is generous on purpose: `killTime` may be the kill's start or
    its end, and the failures the screen catches are hours to weeks out, not minutes.
    """
    reports = [report(NIGHT_1, [fight(0, 60_000), fight(70_000, 130_000, kill=True)])]
    logged_kill = NIGHT_1 + 70_000
    for drift in (-1_500_000, 0, 1_500_000):
        assert pull_time(reports, ENCOUNTER, MYTHIC, logged_kill + drift).ms == 120_000


def test_the_residue_measures_and_discloses_rather_than_being_repaired():
    """A night nobody uploaded, before a kill that IS logged and DOES match.

    Nothing in the Warcraft Logs API can see that night, so this stays `measured` --
    and the published figure is a lower bound. The disclosure is the coverage: one
    observed night across a two-day span is what a partial view looks like from
    outside. This test asserts the LIMITATION, which is the point of it.
    """
    reports = [
        report(NIGHT_1, [fight(0, 3_600_000)]),
        # Tuesday raided and not uploaded. Nothing here can represent it.
        report(NIGHT_2 + 86_400_000, [fight(0, 3_600_000, kill=True)]),
    ]
    kill_at = NIGHT_2 + 86_400_000
    answer = pull_time(reports, ENCOUNTER, MYTHIC, kill_at)
    assert answer.ms == 7_200_000  # two hours, where the guild really spent more
    assert answer.nights == 2
    assert answer.span_days == 2.0
