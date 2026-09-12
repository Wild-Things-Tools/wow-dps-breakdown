"""Progress hours, pinned against hand-written payloads.

Shapes follow the live schema as introspected on 2026-08-26: `Report.startTime` is
absolute, `ReportFight.startTime`/`endTime` are relative to their own report.
"""

import json

import pytest

from wowdps import progresshours
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
    # The encounter BLOCK, which is what `WarcraftLogsClient.encounter` returns --
    # the client unwraps the envelope, so this function no longer walks it.
    assert encounter_zone({"id": 3176, "zone": {"id": 46, "name": "VS"}}) == 46

    # Each of these must be None rather than 0, because the caller's whole job is to
    # tell "no zone" from a zone, and 0 is what made the failing run look healthy.
    assert encounter_zone({"zone": {"id": 0}}) is None
    assert encounter_zone({"zone": None}) is None
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


# ── The composition split: whose raid ran two Protection Paladins ──────────────
#
# The owner's question is whether a guild's progression time on The Twin Fangs
# differs by whether it fielded two Protection Paladins. Every test below pins a
# decision that, reversed, produces a full set of plausible numbers rather than an
# error -- which is what a split by a mis-read roster looks like from outside.


def _details(dps=(), tanks=(), healers=()):
    """A `playerDetails` payload in the shape Warcraft Logs returns."""
    return {
        "data": {
            "dps": [{"type": c, "specs": [{"spec": s}]} for c, s in dps],
            "tanks": [{"type": c, "specs": [{"spec": s}]} for c, s in tanks],
            "healers": [{"type": c, "specs": [{"spec": s}]} for c, s in healers],
        }
    }


def test_the_class_is_read_beside_the_spec():
    """`Protection` alone names two different specialisations.

    Protection Paladin and Protection Warrior share a spec NAME, so a key built from
    the spec alone counts every prot warrior as a paladin -- and a raid running one
    of each would answer the owner's question with a yes it never earned.
    """
    composition = progresshours.raid_composition(
        _details(tanks=(("Paladin", "Protection"), ("Warrior", "Protection")))
    )
    assert composition.specs == ("Paladin/Protection", "Warrior/Protection")
    assert progresshours.fields_at_least(composition, "Paladin/Protection", 2) is False


def test_every_role_bucket_is_read_not_just_the_tanks():
    """Which bucket a player lands in is Warcraft Logs' classification of what they
    DID; the question is about what they ARE.

    A Protection Paladin filed under `dps` is still a Protection Paladin in the raid,
    and reading only `tanks` would undercount in a way that looks exactly like a guild
    that did not run the composition.
    """
    composition = progresshours.raid_composition(
        _details(tanks=(("Paladin", "Protection"),), dps=(("Paladin", "Protection"),))
    )
    assert progresshours.fields_at_least(composition, "Paladin/Protection", 2) is True


def test_a_payload_with_no_buckets_is_unknown_rather_than_empty():
    """ "The roster could not be read" and "the raid fielded nobody" are different
    findings, and only one of them may be counted into `without`."""
    assert progresshours.raid_composition({"data": {}}) is None
    assert progresshours.raid_composition("not json") is None
    assert progresshours.raid_composition(_details()).specs == ()


def test_an_unreadable_player_makes_the_answer_unknown_not_no():
    """A row whose class or spec cannot be read is an unknown player, never a player
    of some other spec. Counting it as "not a Protection Paladin" biases the split in
    exactly one direction and nothing downstream could see it."""
    composition = progresshours.raid_composition(
        _details(tanks=(("Paladin", "Protection"),), dps=(("Mage", None),))
    )
    assert composition.unreadable == 1
    assert progresshours.fields_at_least(composition, "Paladin/Protection", 2) is None


def test_enough_readable_players_settle_it_despite_an_unreadable_one():
    """The order of the two tests is the whole of `fields_at_least`.

    Once two readable Protection Paladins are counted, a third unreadable row cannot
    change the answer -- so refusing there would throw away a roster that does answer.
    """
    composition = progresshours.raid_composition(
        _details(
            tanks=(("Paladin", "Protection"), ("Paladin", "Protection")),
            dps=(("Mage", None),),
        )
    )
    assert composition.unreadable == 1
    assert progresshours.fields_at_least(composition, "Paladin/Protection", 2) is True


def test_a_player_who_swapped_spec_is_unreadable_rather_than_one_of_them():
    """`harvest.spec_of_row` refuses a row carrying two specs, and this inherits it:
    picking either would be a coin toss recorded as a measurement."""
    payload = {
        "data": {
            "tanks": [
                {"type": "Paladin", "specs": [{"spec": "Protection"}, {"spec": "Retribution"}]}
            ]
        }
    }
    composition = progresshours.raid_composition(payload)
    assert composition.specs == ()
    assert composition.unreadable == 1


def test_the_split_counts_unknown_apart_from_without():
    """Three groups, never two. A guild nobody could establish anything about is not
    evidence that the composition does not help."""
    rows = [
        {"outcome": "measured", "hours": 1.0, "fieldsSpec": True},
        {"outcome": "measured", "hours": 3.0, "fieldsSpec": True},
        {"outcome": "measured", "hours": 5.0, "fieldsSpec": False},
        {"outcome": "measured", "hours": 9.0, "fieldsSpec": None},
        # Not measured: no hours to put in any group.
        {"outcome": "no-reports", "reportsSeen": 0},
    ]
    split = progresshours.composition_split(rows, "Paladin/Protection", 2)
    assert split["with"]["sample"] == 2
    assert split["with"]["medianHours"] == 2.0
    assert split["without"]["sample"] == 1
    assert split["unknown"]["sample"] == 1
    assert split["spec"] == "Paladin/Protection"
    assert split["atLeast"] == 2


def test_a_row_from_a_run_that_read_no_roster_is_unknown_not_without():
    """A document written before `--composition` existed carries no `fieldsSpec` at
    all. Reading its absence as False would publish the whole tier as "does not field
    the spec" -- the loudest possible wrong answer."""
    rows = [{"outcome": "measured", "hours": 2.0}]
    split = progresshours.composition_split(rows, "Paladin/Protection", 2)
    assert split["unknown"]["sample"] == 1
    assert split["without"]["sample"] == 0


def test_a_boss_no_roster_was_read_for_publishes_no_split():
    """An empty split on every boss of every ordinary run reads as "nobody fields this
    spec", which is an answer to a question that pass never asked."""
    boss = progresshours.BossProgress(
        encounter_id=3421, name="The Twin Fangs", order=2, difficulty=5
    )
    boss.record(1, "measured", 4, hours=2.0, attempts=9)
    assert "compositionSplit" not in boss.to_json()


def test_a_boss_a_roster_was_read_for_publishes_the_split_and_the_roster():
    boss = progresshours.BossProgress(
        encounter_id=3421, name="The Twin Fangs", order=2, difficulty=5
    )
    composition = progresshours.raid_composition(
        _details(tanks=(("Paladin", "Protection"), ("Paladin", "Protection")))
    )
    boss.record(1, "measured", 4, hours=2.0, attempts=9, composition=composition, fields_spec=True)
    document = boss.to_json()
    assert document["compositionSplit"]["with"]["sample"] == 1
    assert document["guilds"][0]["composition"] == ["Paladin/Protection", "Paladin/Protection"]
    assert document["guilds"][0]["fieldsSpec"] is True


def test_the_kill_carries_the_report_and_fight_it_was_read_from():
    """Without this the roster cannot be fetched without walking the reports again,
    and a second walk could land on a different kill."""
    reports = [
        {
            "code": "abc",
            "startTime": 1_000_000,
            "fights": [
                {
                    "id": 7,
                    "startTime": 0,
                    "endTime": 60_000,
                    "kill": False,
                    "encounterID": 3421,
                    "difficulty": 5,
                },
                {
                    "id": 9,
                    "startTime": 120_000,
                    "endTime": 300_000,
                    "kill": True,
                    "encounterID": 3421,
                    "difficulty": 5,
                },
            ],
        }
    ]
    answer = progresshours.pull_time(reports, 3421, 5, kill_time_ms=1_120_000)
    assert answer.ms is not None
    assert (answer.kill_report_code, answer.kill_fight_id) == ("abc", 9)


def test_a_refused_window_points_at_no_kill():
    """A window with no kill has no first kill to re-open, and naming the pull it
    happened to find would label a farm night as a progression's end."""
    reports = [
        {
            "code": "abc",
            "startTime": 1_000_000,
            "fights": [
                {
                    "id": 7,
                    "startTime": 0,
                    "endTime": 60_000,
                    "kill": False,
                    "encounterID": 3421,
                    "difficulty": 5,
                }
            ],
        }
    ]
    answer = progresshours.pull_time(reports, 3421, 5)
    assert answer.ms is None
    assert (answer.kill_report_code, answer.kill_fight_id) == (None, None)


def test_a_boolean_fight_id_is_refused():
    """`isinstance(True, int)` is True in Python, so a boolean would arrive as fight 1
    and re-open somebody else's pull."""
    attempts, _ = progresshours.ordered_attempts(
        [
            {
                "code": "abc",
                "startTime": 0,
                "fights": [
                    {
                        "id": True,
                        "startTime": 0,
                        "endTime": 10,
                        "kill": True,
                        "encounterID": 1,
                        "difficulty": 5,
                    }
                ],
            }
        ],
        1,
        5,
    )
    assert attempts[0].fight_id is None


# --------------------------------------------------------------- the separation test


def test_two_medians_side_by_side_travel_with_a_test_and_an_interval():
    """Two bars ASSERT a separation, so the document has to be able to deny one.

    On the sample this was built for -- Twin Fangs, 23 guilds fielding two Protection
    Paladins against 9 that did not -- the medians differ by half an hour and the
    difference is inside the noise. A chart drawn from `with` and `without` alone
    cannot say that, and nothing else in the document could either.
    """
    # `fieldsSpec` reaches the row only when a roster was actually READ, so a
    # `composition=None` fixture puts every guild in `unknown` and the split has
    # nothing to test. Found by this test failing; the fixture was wrong, not the code.
    two = progresshours.Composition(
        specs=("Paladin/Protection", "Paladin/Protection"), unreadable=0, buckets=("tanks",)
    )
    one = progresshours.Composition(
        specs=("Paladin/Protection", "Warrior/Protection"), unreadable=0, buckets=("tanks",)
    )
    rows = [
        progresshours.guild_row(i, "measured", 3, hours=h, composition=two, fields_spec=True)
        for i, h in enumerate([2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0])
    ] + [
        progresshours.guild_row(i, "measured", 3, hours=h, composition=one, fields_spec=False)
        for i, h in enumerate([2.2, 2.7, 3.2, 3.7, 4.2, 4.7], start=100)
    ]
    split = progresshours.composition_split(rows, "Paladin/Protection", 2)
    assert split["separation"] is not None
    assert 0.0 <= split["separation"]["p"] <= 1.0
    assert split["difference"] is not None
    assert split["difference"]["ci95LowHours"] <= split["difference"]["ci95HighHours"]


def test_the_test_fires_on_two_samples_that_plainly_do_separate():
    """The control. Without it the test above passes against a function returning p=1."""
    separated = progresshours.mann_whitney(
        [1.0, 1.1, 1.2, 1.3, 1.4, 1.5], [9.0, 9.1, 9.2, 9.3, 9.4, 9.5]
    )
    assert separated is not None
    assert separated["p"] < 0.05


def test_a_thin_group_gets_no_p_and_no_interval():
    """Publishing a p from three observations gives a thin sample a test's authority.

    `None` rather than a number, the same refusal `quartiles` makes below four values.
    """
    assert progresshours.mann_whitney([1.0, 2.0, 3.0], [4.0, 5.0, 6.0, 7.0, 8.0]) is None
    assert (
        progresshours.bootstrap_median_difference([1.0, 2.0, 3.0], [4.0, 5.0, 6.0, 7.0, 8.0])
        is None
    )


def test_ties_are_corrected_rather_than_inflating_the_z():
    """Hours are rounded to four decimals, so they tie more often than raw times.

    Uncorrected, the variance is too large... which is the wrong direction to
    remember: the correction SHRINKS the variance, so an uncorrected z is too SMALL
    and a real separation reads as weaker than it is. Either way it is not the number
    the method claims to be, and a sample that is entirely ties has no variance at all
    -- which must come back as no test rather than as a division by zero.
    """
    assert progresshours.mann_whitney([2.0] * 6, [2.0] * 6) is None


def test_the_interval_is_the_same_interval_on_a_second_run():
    """A resampled interval that moved between two runs of one dataset is a number
    nobody could check, which is why the seed is fixed and published."""
    # Twenty distinct values per group, not six. With six the reachable medians are
    # so few that two UNSEEDED runs land on the same percentiles anyway -- measured,
    # by this canary refusing to fire on the first version of this fixture. A test
    # whose data cannot express the difference pins nothing.
    a = [round(1.0 + i * 0.37, 4) for i in range(20)]
    b = [round(1.3 + i * 0.41, 4) for i in range(20)]
    first = progresshours.bootstrap_median_difference(a, b)
    second = progresshours.bootstrap_median_difference(a, b)
    assert first == second
    assert first["seed"] == 0
    # ...and a DIFFERENT seed must move it, or "seeded" is a word rather than a fact.
    assert progresshours.bootstrap_median_difference(a, b, seed=7) != first


def test_the_interval_brackets_the_observed_difference():
    a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    b = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    found = progresshours.bootstrap_median_difference(a, b)
    assert found["ci95LowHours"] <= found["medianHours"] <= found["ci95HighHours"]


# ------------------------------------------------------------------ the published doc


def boss_row(encounter: int, difficulty: int = 5, sample: int = 4, **over) -> dict:
    row = {
        "encounterId": encounter,
        "name": f"Boss {encounter}",
        "difficulty": difficulty,
        "sample": sample,
        "medianHours": 3.0,
    }
    row.update(over)
    return row


def test_a_one_boss_run_keeps_every_other_boss_the_document_already_had():
    """A run measures one difficulty and frequently one boss.

    A document that replaced its input wholesale would delete every boss it did not
    read, and the deletion would look exactly like a season nobody has measured --
    the union rule `merge_gear_shards` arrived at the hard way.
    """
    published = {"difficulty": 5, "bosses": [boss_row(1), boss_row(2), boss_row(3)]}
    fresh = {"difficulty": 5, "bosses": [boss_row(2, medianHours=9.0)]}
    merged = progresshours.merge_documents([published, fresh])
    assert [b["encounterId"] for b in merged["bosses"]] == [1, 2, 3]
    by_id = {b["encounterId"]: b for b in merged["bosses"]}
    assert by_id[2]["medianHours"] == 9.0
    assert by_id[1]["medianHours"] == 3.0


def test_heroic_sits_beside_mythic_rather_than_over_it():
    """Two difficulties are two populations, and `fights.json` splits them for the
    same reason. A Heroic pass must not replace the Mythic row for one boss."""
    mythic = {"difficulty": 5, "bosses": [boss_row(1, 5)]}
    heroic = {"difficulty": 4, "bosses": [boss_row(1, 4)]}
    merged = progresshours.merge_documents([mythic, heroic])
    assert sorted(b["difficulty"] for b in merged["bosses"]) == [4, 5]


def test_a_row_written_before_rows_carried_a_difficulty_takes_the_documents():
    """An older document states the difficulty once, at the top. Reading the absence
    as "no difficulty" would put every such row under one key and merge two seasons'
    populations into one row."""
    old = {"difficulty": 4, "bosses": [{"encounterId": 7, "sample": 3}]}
    merged = progresshours.merge_documents([old])
    assert merged["bosses"][0]["difficulty"] == 4


def test_a_boss_without_a_split_is_named_rather_than_counted(tmp_path):
    document = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1), boss_row(2)]}
    document["bosses"][0]["compositionSplit"] = {"spec": "Paladin/Protection"}
    out = progresshours.publish_document(tmp_path / "MID2", document)
    assert out["coverage"]["withoutSplit"] == [{"encounterId": 2, "difficulty": 5}]


def test_a_quiet_rerun_leaves_the_published_file_byte_identical(tmp_path):
    """`cost` is a reading of Warcraft Logs' hourly meter, so it differs on every run
    by construction; left in the comparison the settle can never fire."""
    out = tmp_path / "MID2"
    base = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1)]}

    first = progresshours.publish_document(out, {**base, "generatedAt": "A", "cost": {"points": 1}})
    progresshours.write_progress_hours(out, first)
    was = (out / "progress-hours.json").read_bytes()

    second = progresshours.publish_document(
        out, {**base, "generatedAt": "B", "cost": {"points": 999}}
    )
    progresshours.write_progress_hours(out, second)
    assert (out / "progress-hours.json").read_bytes() == was


def test_a_real_change_still_writes(tmp_path):
    """The control: without it the settle test passes against a writer that never
    writes."""
    out = tmp_path / "MID2"
    base = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1)]}
    progresshours.write_progress_hours(out, progresshours.publish_document(out, base))
    was = (out / "progress-hours.json").read_bytes()

    moved = {**base, "bosses": [boss_row(1, medianHours=7.5)]}
    progresshours.write_progress_hours(out, progresshours.publish_document(out, moved))
    assert (out / "progress-hours.json").read_bytes() != was


def test_a_write_that_would_discard_every_measurement_is_refused(tmp_path):
    out = tmp_path / "MID2"
    base = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1)]}
    progresshours.write_progress_hours(out, progresshours.publish_document(out, base))

    empty = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1, sample=0)]}
    with pytest.raises(progresshours.MeasurementsWouldBeLost):
        progresshours.write_progress_hours(out, empty)
    progresshours.write_progress_hours(out, empty, force=True)


# --------------------------------------------------------------------------------
# The published guild identity: a quasi-identifier in a public repository
# --------------------------------------------------------------------------------


def guilds_of(document, encounter_id=1):
    for boss in document["bosses"]:
        if boss["encounterId"] == encounter_id:
            return boss["guilds"]
    raise AssertionError(f"no boss {encounter_id}")


def test_a_published_guild_row_never_carries_the_raw_id(tmp_path):
    """The whole point: a guild id resolves through the API to a name and a realm,
    and this document is committed to a public repository."""
    out = tmp_path / "MID2"
    rows = [{"id": 1546, "outcome": "measured", "hours": 3.5}, {"id": 99, "outcome": "no-reports"}]
    document = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1, guilds=rows)]}

    published = progresshours.publish_document(out, document, salt="pepper")

    assert published["guildIdentity"] == "pseudonym"
    assert all("id" not in row for row in guilds_of(published))
    assert all(isinstance(row["guild"], str) for row in guilds_of(published))
    # Everything the rows are FOR survives; only the identity is replaced.
    assert [row["outcome"] for row in guilds_of(published)] == ["measured", "no-reports"]
    assert guilds_of(published)[0]["hours"] == 3.5


def test_the_pseudonym_is_the_same_string_in_two_runs(tmp_path):
    """Run-stable, or the rows carry a distribution and nothing else -- and the
    settle could never fire, because every run would rewrite every pseudonym."""
    rows = [{"id": 1546, "outcome": "measured"}]
    document = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1, guilds=rows)]}

    first = progresshours.publish_document(tmp_path / "a", document, salt="pepper")
    second = progresshours.publish_document(tmp_path / "b", document, salt="pepper")

    assert guilds_of(first)[0]["guild"] == guilds_of(second)[0]["guild"]


def test_a_different_salt_gives_a_different_pseudonym():
    """The control. Without it the stability test passes against a function that
    ignores the salt entirely, which would be an unsalted hash of an enumerable id."""
    assert progresshours.guild_pseudonym(1546, "pepper") != progresshours.guild_pseudonym(
        1546, "salt"
    )


def test_an_already_published_raw_id_is_converted_by_the_next_publish(tmp_path):
    """A repair rather than only a stop: the published file is folded in as the
    oldest document, so ids already committed are converted on the next write."""
    out = tmp_path / "MID2"
    out.mkdir(parents=True)
    stale = {
        "difficulty": 5,
        "tier": "MID2",
        "bosses": [boss_row(2, guilds=[{"id": 4242, "outcome": "measured"}])],
    }
    (out / "progress-hours.json").write_text(json.dumps(stale), encoding="utf-8")

    fresh = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1)]}
    published = progresshours.publish_document(out, fresh, salt="pepper")

    assert "id" not in guilds_of(published, 2)[0]
    assert guilds_of(published, 2)[0]["guild"] == progresshours.guild_pseudonym(4242, "pepper")


def test_a_pseudonym_is_not_hashed_a_second_time():
    """Idempotence, and it is load-bearing: most rows arrive already converted from
    the published file, and a hash of a hash would move on every run."""
    rows = [{"id": 1546, "outcome": "measured"}]
    document = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1, guilds=rows)]}

    once = progresshours.pseudonymise_guilds(document, salt="pepper")
    twice = progresshours.pseudonymise_guilds(once, salt="pepper")

    assert twice["bosses"][0]["guilds"] == once["bosses"][0]["guilds"]


def test_no_salt_withholds_the_identity_rather_than_publishing_it_raw(tmp_path):
    """Fail closed. A run without the secret is a local run or a misconfigured
    workflow, and neither is a reason to put a quasi-identifier in a public file."""
    out = tmp_path / "MID2"
    rows = [{"id": 1546, "outcome": "measured", "hours": 3.5}]
    document = {"difficulty": 5, "tier": "MID2", "bosses": [boss_row(1, guilds=rows)]}

    published = progresshours.publish_document(out, document, salt=None)

    assert published["guildIdentity"] == "withheld"
    assert guilds_of(published) == [{"outcome": "measured", "hours": 3.5}]
