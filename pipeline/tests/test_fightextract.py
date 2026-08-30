"""Reading fight structure out of event payloads, against hand-written fixtures.

The API credentials live in CI, so nothing here can call Warcraft Logs. What can be
tested offline is the whole of the interesting part: every assumption the extraction
makes about what an event stream means is a pure function over a payload, and each
one is pinned here against events shaped the way the API returns them.

Two fixtures carry most of the weight. ``lightblinded_vanguard_events`` is the
owner's stated ground truth written out as events -- three targets up for the whole
pull, one of them carrying a buff for the first twenty seconds -- so a change that
breaks the extraction fails here rather than in CI a day later. ``add_wave_events``
is the other shape: a boss plus waves that arrive, live and leave.
"""

from __future__ import annotations

import pytest

from wowdps import fightextract
from wowdps.fightextract import (
    EncounterObservation,
    active_time_fraction,
    add_patterns,
    aura_windows,
    damage_by_target,
    enemy_lives,
    observe_fight,
    phase_windows,
    target_count_timeline,
)

# Report-relative milliseconds: fights do not start at zero in a real report, and a
# bug that assumed they did would be invisible against a zero-based fixture.
FIGHT_START = 1_000_000
FIGHT_END = FIGHT_START + 300_000


def at(second: float) -> int:
    return int(FIGHT_START + second * 1000)


def damage(second: float, actor: int, instance: int = 0, amount: float = 1000.0) -> dict:
    return {
        "timestamp": at(second),
        "type": "damage",
        "targetID": actor,
        "targetInstance": instance,
        "amount": amount,
    }


def death(second: float, actor: int, instance: int = 0) -> dict:
    return {"timestamp": at(second), "type": "death", "targetID": actor, "targetInstance": instance}


def aura(second: float, kind: str, ability: int, actor: int, instance: int = 0) -> dict:
    return {
        "timestamp": at(second),
        "type": kind,
        "abilityGameID": ability,
        "targetID": actor,
        "targetInstance": instance,
    }


def fight(**overrides) -> dict:
    base = {
        "id": 7,
        "encounterID": 3180,
        "name": "Lightblinded Vanguard",
        "difficulty": 5,
        "kill": True,
        "size": 20,
        "startTime": FIGHT_START,
        "endTime": FIGHT_END,
        "friendlyPlayers": list(range(1, 21)),
        "enemyNPCs": [],
        "phaseTransitions": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------
# Lifespans
# --------------------------------------------------------------------------------


def test_instances_of_one_npc_are_separate_enemies():
    """Five copies of an add share one actor id and differ only by instance.

    Keying on the actor alone would report a wave of five as a single enemy alive
    for the union of their lifetimes -- one add, five times too long.
    """
    events = [damage(10, 50, instance=i) for i in range(1, 6)]
    events += [damage(25, 50, instance=i) for i in range(1, 6)]
    lives = enemy_lives(events, [], FIGHT_START, FIGHT_END)
    assert len(lives) == 5
    assert {life.instance for life in lives} == {1, 2, 3, 4, 5}


def test_an_enemy_that_dies_ends_at_its_death():
    lives = enemy_lives([damage(5, 50), damage(40, 50)], [death(42, 50)], FIGHT_START, FIGHT_END)
    assert lives[0].died is True
    assert lives[0].last_seen == 42.0
    assert lives[0].lifetime == 37.0


def test_an_enemy_with_no_death_ends_at_its_last_hit():
    """Despawns leave no death event, so the honest end is the last observed hit."""
    lives = enemy_lives([damage(5, 50), damage(40, 50)], [], FIGHT_START, FIGHT_END)
    assert lives[0].died is False
    assert lives[0].last_seen == 40.0


def test_death_events_that_name_the_dying_unit_as_source_are_still_read():
    """Log versions differ on whether a death's subject is target or source."""
    events = [{"timestamp": at(30), "type": "death", "sourceID": 50, "sourceInstance": 0}]
    lives = enemy_lives([damage(5, 50)], events, FIGHT_START, FIGHT_END)
    assert lives[0].died is True and lives[0].last_seen == 30.0


def test_first_seen_is_first_damage_not_spawn():
    """Named for what it is. The API has no general spawn event, and pretending
    otherwise would report every add as spawning when the raid got round to it."""
    lives = enemy_lives([damage(37, 50)], [], FIGHT_START, FIGHT_END)
    assert lives[0].first_seen == 37.0


# --------------------------------------------------------------------------------
# Timelines
# --------------------------------------------------------------------------------


def test_three_permanent_targets_read_as_three_constant_targets():
    lives = enemy_lives(
        [damage(0.5, actor) for actor in (10, 11, 12)]
        + [damage(299, actor) for actor in (10, 11, 12)],
        [death(299.5, actor) for actor in (10, 11, 12)],
        FIGHT_START,
        FIGHT_END,
    )
    timeline = target_count_timeline(lives, 300.0)
    assert timeline.peak == 3
    assert timeline.constant is True
    assert timeline.mean == pytest.approx(3.0, abs=0.02)


def test_a_wave_that_comes_and_goes_is_not_constant():
    lives = enemy_lives(
        [damage(0.5, 10), damage(299, 10)] + [damage(60, 50, i) for i in range(5)],
        [death(80, 50, i) for i in range(5)],
        FIGHT_START,
        FIGHT_END,
    )
    timeline = target_count_timeline(lives, 300.0)
    assert timeline.peak == 6
    assert timeline.constant is False
    # 20 of 300 seconds at six targets, the rest at one.
    assert timeline.mean == pytest.approx(1 + 5 * 20 / 300, abs=0.02)


def test_targets_dying_in_the_last_second_do_not_make_a_fight_variable():
    """Every kill ends with the targets dying one after another. A `constant` flag
    that reacted to that would say no fight in the game has a constant shape."""
    lives = enemy_lives(
        [damage(0.5, actor) for actor in (10, 11, 12)],
        [death(298.5, 10), death(299.2, 11), death(300.0, 12)],
        FIGHT_START,
        FIGHT_END,
    )
    timeline = target_count_timeline(lives, 300.0)
    assert timeline.constant is True
    assert timeline.peak_share > 0.99


def test_an_empty_fight_yields_a_zero_timeline_rather_than_an_error():
    timeline = target_count_timeline([], 300.0)
    assert timeline.steps == ((0.0, 0),)
    assert timeline.mean == 0.0 and timeline.peak == 0


# --------------------------------------------------------------------------------
# Add patterns
# --------------------------------------------------------------------------------


def test_wave_cadence_and_lifetime_come_out_in_raid_event_shape():
    events = []
    deaths = []
    for wave, start in enumerate((20, 80, 140)):
        for instance in range(5):
            events.append(damage(start, 50, instance=wave * 5 + instance))
            deaths.append(death(start + 20, 50, instance=wave * 5 + instance))
    lives = enemy_lives(events, deaths, FIGHT_START, FIGHT_END)
    pattern = add_patterns(lives, total_damage=sum(life.damage for life in lives))[0]

    assert pattern.instances == 15
    assert pattern.first_seen == 20.0
    assert pattern.lifetime == 20.0
    assert pattern.present_at_pull is False
    # Instances within a wave arrive together, so most gaps are zero; the median
    # over fifteen instances is the within-wave gap, not the wave cadence. That is
    # the honest reading of "time between one instance and the next", and the
    # reason wave detection needs the owner rather than a cleverer median.
    assert pattern.cadence == 0.0


def test_targets_up_at_the_pull_are_marked_as_such():
    """The distinction that decides the scenario: a target count, or an add wave."""
    lives = enemy_lives([damage(1.4, 10), damage(45, 50)], [], FIGHT_START, FIGHT_END)
    patterns = {pattern.name: pattern for pattern in add_patterns(lives, 2000.0)}
    assert patterns["actor 10"].present_at_pull is True
    assert patterns["actor 50"].present_at_pull is False


def test_a_single_appearance_has_no_cadence():
    """One appearance is not a rhythm, and a cooldown derived from it is invented."""
    lives = enemy_lives([damage(20, 50)], [], FIGHT_START, FIGHT_END)
    assert add_patterns(lives, 1000.0)[0].cadence is None


# --------------------------------------------------------------------------------
# Auras
# --------------------------------------------------------------------------------


def test_an_aura_window_is_its_application_to_its_removal():
    windows = aura_windows(
        [aura(0, "applybuff", 1234, 11), aura(20, "removebuff", 1234, 11)],
        FIGHT_START,
        FIGHT_END,
        {1234: "Blinding Light"},
    )
    assert len(windows) == 1
    assert windows[0].ability_name == "Blinding Light"
    assert (windows[0].start, windows[0].duration) == (0.0, 20.0)
    assert windows[0].truncated is False


def test_a_refresh_does_not_open_a_second_window():
    """Otherwise one twenty-second aura is reported as four five-second ones."""
    events = [
        aura(0, "applybuff", 1234, 11),
        aura(5, "refreshbuff", 1234, 11),
        aura(10, "refreshbuff", 1234, 11),
        aura(20, "removebuff", 1234, 11),
    ]
    windows = aura_windows(events, FIGHT_START, FIGHT_END)
    assert len(windows) == 1 and windows[0].duration == 20.0


def test_an_aura_still_up_at_the_end_is_truncated_not_measured():
    windows = aura_windows([aura(250, "applydebuff", 99, 10)], FIGHT_START, FIGHT_END)
    assert windows[0].truncated is True and windows[0].duration == 50.0


def test_a_removal_with_no_application_is_flagged_rather_than_dropped():
    """The aura was up before the fight or before the event page. Its start is
    unknown, which is a different statement from 'it did not happen'."""
    windows = aura_windows([aura(12, "removebuff", 99, 10)], FIGHT_START, FIGHT_END)
    assert len(windows) == 1
    assert windows[0].start == 0.0 and windows[0].truncated is True


def test_windows_on_different_instances_do_not_merge():
    events = [
        aura(0, "applybuff", 1234, 50, instance=1),
        aura(0, "applybuff", 1234, 50, instance=2),
        aura(20, "removebuff", 1234, 50, instance=1),
        aura(30, "removebuff", 1234, 50, instance=2),
    ]
    windows = aura_windows(events, FIGHT_START, FIGHT_END)
    assert sorted(window.duration for window in windows) == [20.0, 30.0]


# --------------------------------------------------------------------------------
# Phases and tables
# --------------------------------------------------------------------------------


def test_phases_need_both_the_transitions_and_the_metadata():
    """Transitions carry times without names; report.phases carries names without
    times. Neither one alone is a phase list."""
    transitions = [
        {"id": 1, "startTime": FIGHT_START},
        {"id": 2, "startTime": FIGHT_START + 90_000},
        {"id": 1, "startTime": FIGHT_START + 150_000},
    ]
    metadata = [
        {"id": 1, "name": "Vanguard", "isIntermission": False},
        {"id": 2, "name": "Blinding", "isIntermission": True},
    ]
    windows = phase_windows(transitions, metadata, 300.0)
    assert [w.name for w in windows] == ["Vanguard", "Blinding", "Vanguard"]
    assert [w.start for w in windows] == [0.0, 90.0, 150.0]
    assert windows[1].is_intermission is True
    assert windows[-1].duration == 150.0


def test_an_encounter_with_no_transitions_has_no_phases():
    assert phase_windows([], [{"id": 1, "name": "Only"}], 300.0) == []


def test_damage_by_target_shares_sum_to_one():
    table = {
        "data": {
            "entries": [
                {"name": "Boss", "id": 10, "total": 750},
                {"name": "Add", "id": 11, "total": 250},
            ]
        }
    }
    rows = damage_by_target(table)
    assert [row.name for row in rows] == ["Boss", "Add"]
    assert sum(row.share for row in rows) == pytest.approx(1.0)


def test_an_unrecognised_table_shape_yields_nothing_rather_than_a_wrong_number():
    assert damage_by_target({"unexpected": True}) == []
    assert damage_by_target(None) == []


def test_active_time_is_a_median_fraction_of_the_fight():
    table = {
        "data": {
            "entries": [{"activeTime": 240_000}, {"activeTime": 270_000}, {"activeTime": 300_000}]
        }
    }
    assert active_time_fraction(table, 300.0) == pytest.approx(0.9)


# --------------------------------------------------------------------------------
# Whole fights: the two shapes that matter
# --------------------------------------------------------------------------------


def lightblinded_vanguard_events():
    """The owner's ground truth, written out as events.

    Three targets up for the whole pull; one of the three carries a buff for the
    first twenty seconds. If the extraction stops reproducing this, it is the
    extraction that is wrong.
    """
    damage_events = []
    for actor in (10, 11, 12):
        for second in range(0, 300, 10):
            damage_events.append(damage(second + 0.5, actor, amount=1000))
    death_events = [death(299.5, actor) for actor in (10, 11, 12)]
    aura_events = [aura(0.2, "applybuff", 555_001, 11), aura(20.4, "removebuff", 555_001, 11)]
    return damage_events, death_events, aura_events


def test_lightblinded_vanguard_reads_as_three_permanent_targets():
    damage_events, death_events, aura_events = lightblinded_vanguard_events()
    observation = observe_fight(
        report_code="aBcD1234",
        fight=fight(),
        damage_events=damage_events,
        death_events=death_events,
        aura_events=aura_events,
        phase_metadata=[],
        actor_names={
            10: "Lightblinded Zealot",
            11: "Lightblinded Champion",
            12: "Lightblinded Seer",
        },
        ability_names={555_001: "Blinding Fervor"},
        damage_table={
            "data": {"entries": [{"name": "Lightblinded Zealot", "id": 10, "total": 30000}]}
        },
        player_table={"data": {"entries": [{"activeTime": 285_000}]}},
    )

    assert observation.raid_size == 20
    assert observation.players == 20
    assert observation.significant_timeline.peak == 3
    assert observation.significant_timeline.constant is True
    assert observation.significant_timeline.mean == pytest.approx(3.0, abs=0.05)
    assert len(observation.adds) == 3
    assert all(add.present_at_pull for add in observation.adds)

    assert len(observation.auras) == 1
    window = observation.auras[0]
    assert window.ability_name == "Blinding Fervor"
    assert window.start == pytest.approx(0.2)
    assert window.duration == pytest.approx(20.2)
    # One of the three, not all three: the aura is on a single actor.
    assert window.actor_id == 11


def add_wave_fight():
    damage_events = [damage(second, 10) for second in range(0, 300, 5)]
    death_events = []
    for wave, start in enumerate((20, 80, 140, 200, 260)):
        for instance in range(5):
            key = wave * 5 + instance
            damage_events.append(damage(start, 50, instance=key, amount=500))
            damage_events.append(damage(start + 19, 50, instance=key, amount=500))
            death_events.append(death(start + 20, 50, instance=key))
    return damage_events, death_events


def test_an_add_wave_fight_reads_as_a_boss_plus_waves():
    damage_events, death_events = add_wave_fight()
    observation = observe_fight(
        report_code="wave0001",
        fight=fight(name="Some Add Fight"),
        damage_events=damage_events,
        death_events=death_events,
        aura_events=[],
        phase_metadata=[],
        actor_names={10: "Boss", 50: "Summoned Zealot"},
    )
    assert observation.significant_timeline.peak == 6
    assert observation.significant_timeline.constant is False
    by_name = {add.name: add for add in observation.adds}
    assert by_name["Boss"].present_at_pull is True
    assert by_name["Summoned Zealot"].instances == 25
    assert by_name["Summoned Zealot"].lifetime == pytest.approx(20.0)


def test_an_enemy_under_the_significance_floor_is_kept_but_not_counted_as_a_target():
    """'Three things exist' and 'three things matter' are different claims."""
    damage_events = [damage(second, 10, amount=10_000) for second in range(0, 300, 5)]
    damage_events += [damage(50, 99, amount=1), damage(60, 99, amount=1)]  # a prop
    observation = observe_fight(
        report_code="prop0001",
        fight=fight(),
        damage_events=damage_events,
        death_events=[],
        aura_events=[],
        phase_metadata=[],
    )
    assert observation.timeline.peak == 2
    assert observation.significant_timeline.peak == 1


def test_an_enemy_hit_exactly_once_has_no_width_and_no_effect_on_the_timeline():
    """A known limit rather than an accident: with damage as the presence signal,
    an enemy's window runs from its first hit to its last, so one hit is a point.
    It is still listed as an enemy; it just cannot raise a target count."""
    observation = observe_fight(
        report_code="once0001",
        fight=fight(),
        damage_events=[damage(second, 10) for second in range(0, 300, 5)] + [damage(50, 99)],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
    )
    assert len(observation.enemies) == 2
    assert observation.timeline.peak == 1


def test_a_fight_with_no_enemy_events_says_so_instead_of_reporting_zero_targets():
    observation = observe_fight(
        report_code="empty001",
        fight=fight(),
        damage_events=[],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
    )
    assert any("target counts could not be measured" in w for w in observation.warnings)


# --------------------------------------------------------------------------------
# Pooling across fights
# --------------------------------------------------------------------------------


def one_fight(code: str, size: int, duration_s: float) -> fightextract.FightObservation:
    end = FIGHT_START + int(duration_s * 1000)
    damage_events, death_events, aura_events = lightblinded_vanguard_events()
    damage_events = [event for event in damage_events if event["timestamp"] <= end]
    death_events = [event for event in death_events if event["timestamp"] <= end]
    return observe_fight(
        report_code=code,
        fight=fight(size=size, endTime=end, friendlyPlayers=list(range(size))),
        damage_events=damage_events,
        death_events=death_events,
        aura_events=aura_events,
        phase_metadata=[],
        actor_names={10: "A", 11: "B", 12: "C"},
        ability_names={555_001: "Blinding Fervor"},
    )


def test_pooled_facts_carry_the_spread_they_were_pooled_from():
    """A profile built from five logs is five guilds' pulls. A bare median would
    read as more precise than the sample it came from."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [
        one_fight("r1", 20, 240),
        one_fight("r2", 20, 300),
        one_fight("r3", 20, 280),
    ]

    assert observation.raid_size.agrees is True
    assert observation.raid_size.median == 20

    duration = observation.duration
    assert duration.median == 280 and (duration.low, duration.high) == (240, 300)
    assert duration.agrees is False
    assert duration.n == 3


def test_an_aura_seen_in_one_fight_of_three_is_not_a_fight_pattern():
    """A single observation cannot tell 'this encounter does this' from 'this pull
    went badly'."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    lonely = observe_fight(
        report_code="r9",
        fight=fight(),
        damage_events=[damage(1, 10)],
        death_events=[],
        aura_events=[aura(5, "applydebuff", 777, 10), aura(9, "removedebuff", 777, 10)],
        phase_metadata=[],
    )
    observation.fights = [one_fight("r1", 20, 300), one_fight("r2", 20, 300), lonely]

    ids = {entry["abilityId"] for entry in observation.pooled_auras()}
    assert 555_001 in ids
    assert 777 not in ids


def test_adds_are_pooled_on_game_id_because_actor_ids_are_report_local():
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    first = observe_fight(
        report_code="r1",
        fight=fight(),
        damage_events=[damage(1, 10)],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
        actor_names={10: "Zealot"},
        actor_game_ids={10: 240001},
    )
    # Same NPC, different report, different report-local actor id.
    second = observe_fight(
        report_code="r2",
        fight=fight(),
        damage_events=[damage(1, 77)],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
        actor_names={77: "Zealot"},
        actor_game_ids={77: 240001},
    )
    observation.fights = [first, second]
    pooled = observation.pooled_adds()
    assert len(pooled) == 1 and pooled[0]["seenInFights"] == 2


def test_player_debuffs_are_not_offered_as_encounter_mechanics():
    """The first real probe run nominated a Paladin cooldown as a boss mechanic.

    A boss buffing its own add and a player landing a debuff on that add both put
    an aura on an enemy and both arrive in the same event stream. Only the source
    tells them apart.
    """
    from wowdps.fightextract import (
        AuraWindow,
        EncounterObservation,
        FightObservation,
        TargetCountTimeline,
    )

    def fight(code: str) -> FightObservation:
        return FightObservation(
            report_code=code,
            fight_id=1,
            encounter_id=3180,
            encounter_name="Lightblinded Vanguard",
            difficulty=5,
            kill=True,
            duration=300.0,
            raid_size=20,
            players=20,
            timeline=TargetCountTimeline(steps=((0.0, 3),), duration=300.0),
            significant_timeline=TargetCountTimeline(steps=((0.0, 3),), duration=300.0),
            enemies=(),
            adds=(),
            phases=(),
            auras=(
                # The encounter's own buff on one of its enemies.
                AuraWindow(555001, "Blinding Fervor", 90, 1, 1.0, 21.0, False, source_id=90),
                # A player debuff on the same enemy, same rough window.
                AuraWindow(31884, "Avenging Wrath", 90, 1, 2.0, 22.0, False, source_id=7),
            ),
            damage_by_target=(),
            active_time_fraction=None,
            friendly_ids=frozenset({7}),
        )

    observation = EncounterObservation(
        encounter_id=3180,
        encounter_name="Lightblinded Vanguard",
        difficulty=5,
        fights=[fight("aaa"), fight("bbb")],
    )

    names = {aura["ability"] for aura in observation.pooled_auras()}
    assert names == {"Blinding Fervor"}, "a player-applied aura reached the candidate list"

    # The unfiltered view still sees both, so nothing is lost -- only the default
    # for "what could the encounter be doing" is narrowed.
    both = {aura["ability"] for aura in observation.pooled_auras(encounter_only=False)}
    assert both == {"Blinding Fervor", "Avenging Wrath"}


def test_an_aura_with_no_source_is_kept():
    """Unknown is not the same as 'a player did it'."""
    from wowdps.fightextract import AuraWindow, aura_windows

    windows = aura_windows(
        [
            {"type": "applybuff", "abilityGameID": 42, "targetID": 90, "timestamp": 1000},
            {"type": "removebuff", "abilityGameID": 42, "targetID": 90, "timestamp": 21000},
        ],
        fight_start_ms=1000,
        fight_end_ms=301000,
    )
    assert len(windows) == 1
    assert isinstance(windows[0], AuraWindow)
    assert windows[0].source_id is None


def test_the_applying_source_wins_over_the_removing_one():
    """A window belongs to whoever put it there, not whoever happened to strip it."""
    from wowdps.fightextract import aura_windows

    windows = aura_windows(
        [
            {
                "type": "applydebuff",
                "abilityGameID": 7,
                "targetID": 5,
                "sourceID": 11,
                "timestamp": 0,
            },
            {
                "type": "removedebuff",
                "abilityGameID": 7,
                "targetID": 5,
                "sourceID": 99,
                "timestamp": 5000,
            },
        ],
        fight_start_ms=0,
        fight_end_ms=300000,
    )
    assert windows[0].source_id == 11


# --------------------------------------------------------------------------------
# Which enemy carries an aura, and which one the priority target stands for
# --------------------------------------------------------------------------------
#
# The question these answer: "the amplification sits on one of the three targets --
# which one?" It was asked of a person twice and never answered. It is in the event
# stream, keyed on (actor_id, instance), with the names in the report's master data.


def test_an_enemy_named_for_the_encounter_is_the_priority_target():
    lives = enemy_lives(
        [damage(1, 10, amount=100), damage(1, 11, amount=9000)],
        [],
        FIGHT_START,
        FIGHT_END,
        actor_names={10: "Lightblinded Vanguard", 11: "Zealot"},
    )
    nomination = fightextract.nominate_priority_enemy(lives, "Lightblinded Vanguard")

    # The name wins even though the add took ninety times the damage: an encounter
    # where the adds out-damage the boss is ordinary and is not a naming problem.
    assert nomination.name == "Lightblinded Vanguard"
    assert "named for the encounter" in nomination.evidence


def test_the_enemy_that_took_clearly_the_most_damage_is_nominated():
    lives = enemy_lives(
        [damage(1, 10, amount=8000), damage(1, 11, amount=1000)],
        [],
        FIGHT_START,
        FIGHT_END,
        actor_names={10: "Warlord", 11: "Zealot"},
    )
    nomination = fightextract.nominate_priority_enemy(lives, "Some Other Name")

    assert nomination.name == "Warlord"
    assert nomination.role_of(10) == fightextract.ROLE_PRIORITY
    assert nomination.role_of(11) == fightextract.ROLE_ADD
    assert "of the damage" in nomination.evidence


def test_three_enemies_hit_about_equally_nominate_nothing():
    """A permanent three-target fight is a shape, not a measurement failure.

    Picking the largest of three near-equal numbers would manufacture exactly the
    fact this is supposed to establish, so it refuses and says why.
    """
    lives = enemy_lives(
        [damage(1, actor, amount=1000) for actor in (10, 11, 12)],
        [],
        FIGHT_START,
        FIGHT_END,
        actor_names={10: "Zealot", 11: "Champion", 12: "Seer"},
    )
    nomination = fightextract.nominate_priority_enemy(lives, "Lightblinded Vanguard")

    assert nomination.known is False
    assert nomination.role_of(11) == fightextract.ROLE_UNKNOWN
    assert "nothing in the events nominates one" in nomination.evidence


def test_an_aura_names_the_enemy_that_carried_it():
    """The answer to "which of the three", in the published payload."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [
        observe_fight(
            report_code=code,
            fight=fight(),
            damage_events=[damage(1, actor) for actor in (10, 11, 12)],
            death_events=[],
            aura_events=[
                aura(1.0, "applybuff", 555_001, 11),
                aura(21.0, "removebuff", 555_001, 11),
            ],
            phase_metadata=[],
            actor_names={10: "Zealot", 11: "Champion", 12: "Seer"},
            actor_game_ids={10: 1, 11: 2, 12: 3},
            ability_names={555_001: "Blinding Fervor"},
        )
        for code in ("r1", "r2")
    ]

    pooled = observation.pooled_auras()[0]
    assert [entry["name"] for entry in pooled["carriedBy"]] == ["Champion"]
    assert pooled["carriedBy"][0]["seenInFights"] == 2
    # Named, but not classified: nothing in this fight nominates a boss, and the
    # aura's role must not be invented out of the enemy merely having a name.
    assert pooled["role"] == fightextract.ROLE_UNKNOWN
    assert "Champion" in pooled["roleEvidence"]


def test_an_aura_on_the_boss_is_reported_as_being_on_the_priority_target():
    """The case that makes an amplification expressible in simc at all."""
    observation = EncounterObservation(3180, "Warlord", 5)
    observation.fights = [
        observe_fight(
            report_code=code,
            fight=fight(name="Warlord"),
            damage_events=[damage(1, 10, amount=9000), damage(1, 11, amount=1000)],
            death_events=[],
            aura_events=[
                aura(1.0, "applybuff", 555_001, 10),
                aura(21.0, "removebuff", 555_001, 10),
            ],
            phase_metadata=[],
            actor_names={10: "Warlord", 11: "Zealot"},
            ability_names={555_001: "Blinding Fervor"},
        )
        for code in ("r1", "r2")
    ]

    pooled = observation.pooled_auras()[0]
    assert pooled["role"] == fightextract.ROLE_PRIORITY
    assert pooled["carriedBy"][0]["name"] == "Warlord"


def test_aura_carriers_pool_on_game_id_across_reports():
    """Actor ids are report-local; the same NPC must not be reported twice."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)

    def pull(code: str, actor: int):
        return observe_fight(
            report_code=code,
            fight=fight(),
            damage_events=[damage(1, actor)],
            death_events=[],
            aura_events=[
                aura(1.0, "applybuff", 555_001, actor),
                aura(21.0, "removebuff", 555_001, actor),
            ],
            phase_metadata=[],
            actor_names={actor: "Champion"},
            actor_game_ids={actor: 240_002},
            ability_names={555_001: "Blinding Fervor"},
        )

    observation.fights = [pull("r1", 11), pull("r2", 88)]
    carriers = observation.pooled_auras()[0]["carriedBy"]
    assert len(carriers) == 1
    assert carriers[0]["gameId"] == 240_002
    assert carriers[0]["seenInFights"] == 2


# --------------------------------------------------------------------------------
# A bounded event fetch, and the statistic it destroys
# --------------------------------------------------------------------------------
#
# The bug these pin, from the first real nine-boss pass: enemy damage-taken is
# paginated and bounded, a twenty-player Mythic pull outruns the budget, and the
# fetch stops part-way. Every enemy's last hit is then the cut point. Dividing a
# time-weighted count by the *fight* length rather than by the part that was read
# reported four of nine bosses at a mean concurrent target count below 1.0 --
# including Vorasius, a single-target boss, at 0.59.


def truncated_fight(read_to: float, fight_length: float = 300.0):
    """One enemy, up the whole time, whose events stop being fetched at `read_to`."""
    return observe_fight(
        report_code="r1",
        fight=fight(startTime=FIGHT_START, endTime=int(FIGHT_START + fight_length * 1000)),
        damage_events=[damage(0.1, 10), damage(read_to, 10)],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
        actor_names={10: "Boss"},
        truncated=True,
    )


def test_a_single_target_boss_never_averages_below_one_target():
    """The impossible number that gave the bug away."""
    observed = truncated_fight(read_to=96.0, fight_length=163.0)

    assert observed.significant_timeline.mean == pytest.approx(1.0, abs=0.01)
    assert observed.event_coverage == pytest.approx(96.0 / 163.0, abs=0.001)


def test_a_complete_fetch_leaves_the_mean_where_it_was():
    """The fix must not move a number that was already right."""
    observed = truncated_fight(read_to=299.9, fight_length=300.0)

    assert observed.significant_timeline.mean == pytest.approx(1.0, abs=0.01)
    assert observed.event_coverage > 0.99


def test_a_partly_read_fight_says_how_far_it_got():
    observed = truncated_fight(read_to=60.0, fight_length=300.0)

    assert observed.event_coverage == pytest.approx(0.2, abs=0.001)
    assert any("20%" in warning for warning in observed.warnings), observed.warnings


def test_peak_survives_truncation_where_the_mean_does_not():
    """Why the promotion path uses the peak.

    Two targets for the first thirty seconds of a five-minute fight, read to sixty
    seconds. The peak is 2 either way; the mean is whatever the denominator says.
    """
    observed = observe_fight(
        report_code="r1",
        fight=fight(startTime=FIGHT_START, endTime=FIGHT_START + 300_000),
        damage_events=[damage(0.1, 10), damage(60.0, 10), damage(0.2, 11), damage(30.0, 11)],
        death_events=[],
        aura_events=[],
        phase_metadata=[],
        actor_names={10: "Boss", 11: "Add"},
    )
    assert observed.significant_timeline.peak == 2
    # Over the sixty seconds that were read, not over three hundred: half of them
    # had two enemies up.
    assert observed.significant_timeline.mean == pytest.approx(1.5, abs=0.02)


def test_pooled_coverage_is_published_so_a_caller_can_refuse():
    observation = EncounterObservation(3180, "Midnight Falls", 5)
    observation.fights = [
        truncated_fight(read_to=95.0, fight_length=480.0),
        truncated_fight(read_to=108.0, fight_length=486.0),
    ]
    coverage = observation.event_coverage
    assert coverage is not None
    assert coverage.high < 0.25


def test_a_self_buff_with_no_source_is_not_an_encounter_mechanic():
    """The regression the first real nine-boss pass shipped.

    Avenging Wrath and Divine Shield reached the published MID2 aura list, on a
    fight whose amplification is asserted as "about twenty seconds at the pull" --
    and Avenging Wrath lasts twenty seconds and is cast at the pull. The source
    test alone did not catch them, because a self-buff often carries no sourceID,
    and "unknown is not a player" then keeps it. Only the target test does: an
    aura on a player is not an aura on an enemy, whatever applied it.
    """
    from wowdps.fightextract import AuraWindow, FightObservation, TargetCountTimeline

    def pull(code: str) -> FightObservation:
        return FightObservation(
            report_code=code,
            fight_id=1,
            encounter_id=3180,
            encounter_name="Lightblinded Vanguard",
            difficulty=5,
            kill=True,
            duration=300.0,
            raid_size=20,
            players=20,
            timeline=TargetCountTimeline(steps=((0.0, 3),), duration=300.0),
            significant_timeline=TargetCountTimeline(steps=((0.0, 3),), duration=300.0),
            enemies=(),
            adds=(),
            phases=(),
            auras=(
                # The encounter's own buff on one of its enemies.
                AuraWindow(1258659, "Light Infused", 90, 1, 0.0, 285.0, False, source_id=None),
                # A paladin's twenty-second cooldown on himself, no source id --
                # the exact shape of the asserted amplification window.
                AuraWindow(1246385, "Avenging Wrath", 7, 0, 0.5, 20.5, False, source_id=None),
            ),
            damage_by_target=(),
            active_time_fraction=None,
            friendly_ids=frozenset({7}),
        )

    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5, [pull("a"), pull("b")])
    assert {aura["ability"] for aura in observation.pooled_auras()} == {"Light Infused"}


def test_a_report_with_no_player_list_says_the_aura_filter_is_inoperative():
    """Both aura tests need the player ids. Without them nothing is filtered, and
    that is invisible in the output unless it is said."""
    observed = observe_fight(
        report_code="r1",
        fight=fight(),
        damage_events=[damage(1, 10)],
        death_events=[],
        aura_events=[aura(1.0, "applybuff", 42, 10), aura(21.0, "removebuff", 42, 10)],
        phase_metadata=[],
        friendly_ids=frozenset(),
    )
    assert any("cannot be told from" in warning for warning in observed.warnings)


def test_a_pets_debuff_on_an_enemy_is_its_owners_and_not_an_encounter_mechanic():
    """The second half of the same regression, found in the published MID2 data.

    `masterData.actors` types a hunter's pet, a mage's Mirror Image and a boss's
    summoned add all as `Pet`. Reading only `type == "Player"` therefore filtered
    none of them, and Mirror Image's Frostbolt was published as something
    Lightblinded Vanguard does to its own adds. Ownership is what separates them.
    """
    from wowdps.fightextract import friendly_source_ids

    actors = [
        {"id": 7, "type": "Player", "name": "A mage"},
        {"id": 8, "type": "Pet", "name": "Mirror Image", "petOwner": 7},
        # A pet of a pet is still the raid's.
        {"id": 9, "type": "Pet", "name": "A pet's pet", "petOwner": 8},
        # The boss's own summon carries the same type and must not be swept up.
        {"id": 90, "type": "NPC", "name": "General Amias Bellamy"},
        {"id": 91, "type": "Pet", "name": "Spirit of the Defender", "petOwner": 90},
        # An ownerless NPC, and a malformed self-owning one that must not hang.
        {"id": 92, "type": "NPC", "name": "War Chaplain Senn"},
        {"id": 93, "type": "Pet", "name": "Broken", "petOwner": 93},
    ]
    assert friendly_source_ids(actors) == frozenset({7, 8, 9})


def test_an_ownership_cycle_that_never_reaches_a_player_is_not_friendly():
    from wowdps.fightextract import friendly_source_ids

    actors = [
        {"id": 1, "type": "Player"},
        {"id": 50, "type": "Pet", "petOwner": 51},
        {"id": 51, "type": "Pet", "petOwner": 50},
    ]
    assert friendly_source_ids(actors) == frozenset({1})


def test_the_kill_dates_of_the_sample_are_published():
    """ "The earliest kills" is a claim about dates, so the dates have to be in the output.

    `--order first` sorts by kill time *within the ranking pages gathered*, and
    Warcraft Logs sorts those by damage. A slow first-night kill therefore sits deep
    in the list and a narrow gather returns later kills while still being, truthfully,
    the earliest ones seen. Nothing in the payload used to say when they happened, so
    the gap was unnoticeable.
    """
    from wowdps.fightextract import EncounterObservation

    observation = EncounterObservation(encounter_id=1, encounter_name="Boss", difficulty=5)
    observation.fights = [
        _stub_fight(started_at=1_700_000_000_000),
        _stub_fight(started_at=1_700_432_000_000),
    ]

    span = observation.killed_between
    assert span == (1_700_000_000_000, 1_700_432_000_000)

    published = observation.to_json()["killedBetween"]
    assert published["first"].startswith("2023-11-14")
    assert published["spanDays"] == 5.0


def test_no_kill_dates_publishes_null_rather_than_an_epoch_zero_span():
    """A row with no timestamp is unknown, not 1970."""
    from wowdps.fightextract import EncounterObservation

    observation = EncounterObservation(encounter_id=1, encounter_name="Boss", difficulty=5)
    observation.fights = [_stub_fight(started_at=0.0)]

    assert observation.killed_between is None
    assert observation.to_json()["killedBetween"] is None


def _stub_fight(started_at: float):
    """A FightObservation with only the fields these two cases read."""
    from wowdps.fightextract import FightObservation, TargetCountTimeline

    empty = TargetCountTimeline(steps=(), duration=1.0, observed=1.0)
    return FightObservation(
        report_code="abc",
        fight_id=1,
        encounter_id=1,
        encounter_name="Boss",
        difficulty=5,
        kill=True,
        duration=1.0,
        raid_size=20,
        players=20,
        timeline=empty,
        significant_timeline=empty,
        enemies=(),
        adds=(),
        phases=(),
        auras=(),
        damage_by_target=(),
        active_time_fraction=None,
        started_at=started_at,
    )


def test_reading_nothing_is_zero_coverage_and_not_the_whole_fight():
    """``None`` and ``0.0`` are opposite states and used to share a branch.

    ``None`` means no bounded event fetch was involved, so the whole fight is the
    right window. ``0.0`` means a bounded fetch read *nothing*, and answering
    ``duration`` there made ``coverage`` come out at **1.0** -- the maximum -- for
    the one case the field exists to catch.

    The value is not hypothetical: the committed MID2 ``fights.json`` carries a
    Lost Explorers pull with a single step at zero targets and ``coverage: 1.0``,
    beside eight real pulls at 0.9994-0.9999.
    """
    from wowdps.fightextract import TargetCountTimeline

    not_applicable = TargetCountTimeline(steps=((0.0, 0),), duration=100.0, observed=None)
    assert not_applicable.window == 100.0
    assert not_applicable.coverage == 1.0

    read_nothing = TargetCountTimeline(steps=((0.0, 0),), duration=100.0, observed=0.0)
    assert read_nothing.window == 0.0
    assert read_nothing.coverage == 0.0

    # The ordinary partial read is untouched.
    read_half = TargetCountTimeline(steps=((0.0, 2),), duration=100.0, observed=50.0)
    assert read_half.coverage == 0.5


# --------------------------------------------------------------------------------
# seenInFights means fights, at all three sites that publish it
# --------------------------------------------------------------------------------


def transitions(*pairs: tuple[int, float]) -> list[dict]:
    """``phaseTransitions`` as WCL sends it: report-relative milliseconds."""
    return [
        {"id": phase_id, "startTime": FIGHT_START + int(second * 1000)}
        for phase_id, second in pairs
    ]


PHASE_NAMES = [
    {"id": 1, "name": "Stage One: Entombed Sentinels", "isIntermission": False},
    {"id": 2, "name": "Intermission: Vitriolic Stasis", "isIntermission": True},
]

# One pull of Entombed Sentinels, in the shape the committed MID2 dataset has:
# the phase cycles, so a single kill contributes four Stage One windows and three
# Intermissions.
SENTINELS_PULL = transitions(
    (1, 0.0), (2, 46.4), (1, 74.4), (2, 165.4), (1, 191.4), (2, 282.4), (1, 292.4)
)


def sentinels_fight(report_code: str, *, with_phases: bool) -> object:
    return observe_fight(
        report_code=report_code,
        fight=fight(phaseTransitions=SENTINELS_PULL if with_phases else []),
        damage_events=[damage(1, 50)],
        death_events=[],
        aura_events=[],
        phase_metadata=PHASE_NAMES,
        actor_names={50: "Sentinel"},
        actor_game_ids={50: 258558},
    )


def test_a_phase_is_counted_once_per_fight_however_often_it_recurs():
    """The defect this replaces published four-window pulls as four fights.

    Two of these four kills carry `phaseTransitions` at all -- which is ordinary,
    Warcraft Logs does not return them on every fight -- and each of those two
    cycles Stage One four times. The old count published 8, i.e. every kill in
    the sample, for a phase six of them say nothing about.

    Reverting `seenInFights` to `len(group)` turns this red at 8 != 2.
    """
    observation = EncounterObservation(53445, "Entombed Sentinels", 5)
    observation.fights = [
        sentinels_fight("r1", with_phases=True),
        sentinels_fight("r2", with_phases=True),
        sentinels_fight("r3", with_phases=False),
        sentinels_fight("r4", with_phases=False),
    ]

    stage_one, intermission = observation.pooled_phases()

    assert observation.to_json()["fightsSampled"] == 4
    assert stage_one["seenInFights"] == 2
    assert intermission["seenInFights"] == 2
    # The window count is not lost -- it moves to a field that says what it is,
    # and it is what the spreads' own `n` counts.
    assert stage_one["windows"] == 8
    assert intermission["windows"] == 6
    assert stage_one["start"]["n"] == 8
    assert intermission["duration"]["n"] == 6


def test_two_kills_from_one_report_are_two_fights_for_an_aura():
    """`pooled_auras` and its carriers counted distinct *reports*.

    No published number moves: today's sampler takes the earliest N distinct
    reports, so the two counts coincide on every measurement in the committed
    MID2 file. This is the sampler that would break it -- and the gate is the
    sharper half, because at `min_fights=2` a genuine two-kill observation from
    one log used to be dropped entirely rather than merely mislabelled.

    Reverting either site to a set of `report_code` turns this red.
    """
    observation = EncounterObservation(53445, "Entombed Sentinels", 5)

    def kill(fight_id: int):
        return observe_fight(
            report_code="one-log",
            fight=fight(id=fight_id),
            damage_events=[damage(1, 50)],
            death_events=[],
            aura_events=[aura(5, "applybuff", 1284207, 50), aura(30, "removebuff", 1284207, 50)],
            phase_metadata=[],
            actor_names={50: "Breath of Ula'tek"},
            actor_game_ids={50: 258557},
        )

    observation.fights = [kill(11), kill(12)]

    [pooled] = observation.pooled_auras()
    assert pooled["seenInFights"] == 2
    assert pooled["applications"] == 2
    assert pooled["carriedBy"][0]["seenInFights"] == 2
