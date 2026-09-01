"""The probe's orchestration, driven by a fake client instead of the real API.

The queries themselves cannot be tested here -- credentials are CI secrets, and the
answer to "does this GraphQL document parse" only comes from the server. What can be
tested is everything between the response and the report: that the right payload
reaches the right extractor, that the point ceiling stops a pass instead of running
into a 429, and that the readable dump says the things a reader needs in order not
to over-read it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wowdps import fightprobe
from wowdps.fightextract import EncounterObservation, observe_fight
from wowdps.fightprofile import load_profiles
from wowdps.warcraftlogs import PointLedger

# These nine encounters are The Voidspire, Midnight Season 1's raid. They were
# filed under MID2 until 2026-08-17, when Warcraft Logs' own zone list settled
# it -- see the "nine bosses filed under MID2" note in CLAUDE.md. MID2 now holds
# The Venomous Abyss, whose bosses nobody has facts for yet.
VOIDSPIRE_TIER = "MID1"


START, END = 1_000_000, 1_300_000


def damage(second: float, actor: int, amount: float = 1000.0) -> dict:
    return {
        "timestamp": int(START + second * 1000),
        "type": "damage",
        "targetID": actor,
        "targetInstance": 0,
        "amount": amount,
    }


class FakeClient:
    """Answers the four calls ``_probe_fight`` makes, and records what it was asked."""

    def __init__(self, **payloads):
        self.ledger = PointLedger()
        self.calls: list[str] = []
        self.payloads = payloads

    def fight_structure(self, code, encounter_id, difficulty):
        self.calls.append(f"structure:{code}")
        return self.payloads["structure"]

    def fight_events(
        self, code, fight_id, data_type, hostility, start_ms, end_ms, limit, max_pages
    ):
        self.calls.append(f"events:{data_type}:{hostility}")
        return self.payloads["events"].get(data_type, []), self.payloads.get("truncated", False)

    def fight_table(
        self, code, fight_id, data_type="DamageDone", view_by="Default", hostility="Friendlies"
    ):
        self.calls.append(f"table:{view_by}")
        return self.payloads.get("tables", {}).get(view_by)


def structure_payload() -> dict:
    return {
        "code": "aBcD1234",
        "masterData": {
            "actors": [
                {"id": 10, "gameID": 240001, "name": "Lightblinded Zealot", "type": "NPC"},
                {"id": 11, "gameID": 240002, "name": "Lightblinded Champion", "type": "NPC"},
                {"id": 12, "gameID": 240003, "name": "Lightblinded Seer", "type": "NPC"},
            ],
            "abilities": [{"gameID": 555001, "name": "Blinding Fervor"}],
        },
        "phases": [
            {
                "encounterID": 3180,
                "phases": [{"id": 1, "name": "Vanguard", "isIntermission": False}],
            },
            {"encounterID": 3182, "phases": [{"id": 1, "name": "Somewhere else"}]},
        ],
        "fights": [
            {
                "id": 7,
                "encounterID": 3180,
                "name": "Lightblinded Vanguard",
                "difficulty": 5,
                "kill": True,
                "size": 20,
                "startTime": START,
                "endTime": END,
                "friendlyPlayers": list(range(20)),
                "phaseTransitions": [{"id": 1, "startTime": START}],
            },
            {"id": 9, "encounterID": 3180, "name": "Lightblinded Vanguard", "kill": False},
        ],
    }


def settings(**overrides) -> fightprobe.ProbeSettings:
    base = dict(
        encounter_ids=(3180,),
        difficulty=5,
        metric="dps",
        reports=1,
        rankings_page=1,
        order="top",
        rankings_pages=1,
        streams=("damage", "deaths", "buffs", "debuffs"),
        events_limit=10000,
        max_pages=3,
        point_ceiling=0.8,
        significant_share=0.01,
    )
    base.update(overrides)
    return fightprobe.ProbeSettings(**base)


def test_the_probe_assembles_one_fight_from_four_event_streams_and_two_tables():
    client = FakeClient(
        structure=structure_payload(),
        events={
            "DamageTaken": [damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)],
            "Deaths": [
                {"timestamp": int(START + 299_500), "type": "death", "targetID": a}
                for a in (10, 11, 12)
            ],
            "Buffs": [
                {
                    "timestamp": START + 200,
                    "type": "applybuff",
                    "abilityGameID": 555001,
                    "targetID": 11,
                },
                {
                    "timestamp": START + 20_400,
                    "type": "removebuff",
                    "abilityGameID": 555001,
                    "targetID": 11,
                },
            ],
            "Debuffs": [],
        },
        tables={
            "Target": {
                "data": {"entries": [{"name": "Lightblinded Zealot", "id": 10, "total": 1}]}
            },
            "Source": {"data": {"entries": [{"activeTime": 285_000}]}},
        },
    )

    fight = fightprobe._probe_fight(client, "aBcD1234", 7, 3180, settings())

    assert fight is not None
    assert fight.raid_size == 20 and fight.players == 20
    assert fight.significant_timeline.peak == 3
    assert fight.significant_timeline.constant is True
    # Names come from masterData, not from the events.
    assert {add.name for add in fight.adds} == {
        "Lightblinded Zealot",
        "Lightblinded Champion",
        "Lightblinded Seer",
    }
    assert fight.auras[0].ability_name == "Blinding Fervor"
    assert fight.auras[0].duration == pytest.approx(20.2)
    # Only this encounter's phase names, not every encounter in the report.
    assert [phase.name for phase in fight.phases] == ["Vanguard"]


def test_enemy_streams_are_asked_for_with_enemy_hostility():
    """A debuff query with the default Friendlies hostility returns what the raid
    had on itself, which is not what a fight profile is about."""
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    fightprobe._probe_fight(client, "aBcD1234", 7, 3180, settings())
    assert all(call.endswith(":Enemies") for call in client.calls if call.startswith("events:"))


def test_buffs_on_enemies_are_fetched_as_well_as_debuffs():
    """An amplification the encounter puts on its own add is a *buff* on that add.
    A probe that only asked for debuffs would miss the case it exists to find."""
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    fightprobe._probe_fight(client, "aBcD1234", 7, 3180, settings())
    assert "events:Buffs:Enemies" in client.calls
    assert "events:Debuffs:Enemies" in client.calls


def test_a_fight_id_the_report_does_not_contain_is_skipped_not_guessed_at():
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    assert fightprobe._probe_fight(client, "aBcD1234", 404, 3180, settings()) is None


def test_the_point_ceiling_stops_a_pass_before_the_api_does():
    """A 429 loses the whole pass; stopping at 80% keeps what has been collected."""
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    client.ledger.record(
        "x", {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 3400}}
    )
    with pytest.raises(fightprobe.PointBudgetExhausted):
        fightprobe.check_budget(client, 0.8)


def test_a_budget_with_no_reading_yet_does_not_stop_anything():
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    fightprobe.check_budget(client, 0.8)  # must not raise


def test_running_out_of_budget_keeps_the_fights_already_paid_for():
    """Two fights read before the budget ran out are two fights of evidence.
    Throwing them away to raise would mean paying for them again."""

    class Exhausting(StubClient):
        def fight_structure(self, code, encounter_id, difficulty):
            if self.calls.count("structure:aBcD1234") >= 1:
                self.ledger.record(
                    "x", {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 3500}}
                )
            return super().fight_structure(code, encounter_id, difficulty)

        def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
            return {
                "id": encounter_id,
                "name": "Lightblinded Vanguard",
                "characterRankings": {
                    "rankings": [
                        {"amount": 1.0, "report": {"code": "aBcD1234", "fightID": 7}},
                        {"amount": 0.9, "report": {"code": "eFgH5678", "fightID": 7}},
                    ]
                },
            }

    client = Exhausting(
        structure=structure_payload(),
        events={"DamageTaken": [damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)]},
        tables={},
    )
    observation, reason = fightprobe.probe_encounter(client, 3180, settings(reports=2))
    assert len(observation.fights) == 1
    assert reason is not None and "points spent this hour" in reason


def one_fight(code: str) -> object:
    # Each report code is its OWN kill, two seconds longer than the last. Two
    # guilds never kill a boss in the same number of milliseconds, and
    # `group_uploads` reads pulls that agree to the millisecond as one kill
    # somebody uploaded twice -- which is what these fixtures would then be.
    return observe_fight(
        report_code=code,
        fight={
            "id": 1,
            "encounterID": 3180,
            "name": "Lightblinded Vanguard",
            "size": 20,
            "startTime": START,
            "endTime": END + 2_000 * (1 + sum(ord(character) for character in code) % 11),
            "friendlyPlayers": list(range(20)),
            "kill": True,
        },
        damage_events=[damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)],
        death_events=[],
        aura_events=[
            {
                "timestamp": START + 200,
                "type": "applybuff",
                "abilityGameID": 555001,
                "targetID": 11,
            },
            {
                "timestamp": START + 20_400,
                "type": "removebuff",
                "abilityGameID": 555001,
                "targetID": 11,
            },
        ],
        phase_metadata=[],
        ability_names={555001: "Blinding Fervor"},
    )


def test_the_dump_says_that_an_aura_window_is_not_a_magnitude():
    """The one thing a reader could over-read: the probe can time an aura and can
    never say what it is worth."""
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [one_fight("r1"), one_fight("r2")]
    text = "\n".join(fightprobe.render(observation, None))
    assert "windows, not magnitudes" in text
    assert "Blinding Fervor" in text


def test_the_dump_puts_the_profile_next_to_the_measurement():
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    observation.fights = [one_fight("r1"), one_fight("r2")]
    text = "\n".join(fightprobe.render(observation, load_profiles(VOIDSPIRE_TIER).get(3180)))
    assert "profile vs measurement" in text
    assert "desired_targets=3" in text
    # And it says out loud which part of the encounter the scenario does not model.
    assert "NOT MODELLED" in text


def test_a_probe_of_nothing_says_so_rather_than_printing_an_empty_table():
    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    text = "\n".join(fightprobe.render(observation, None))
    assert "no fights were read" in text


# --------------------------------------------------------------------------------
# The whole command, with the API stubbed out
# --------------------------------------------------------------------------------


class StubClient(FakeClient):
    """A FakeClient that also answers the calls ``cmd_fight_probe`` makes directly."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def rate_limit(self):
        spent = 100.0 + 40.0 * len([c for c in self.calls if c == "ratelimit"])
        self.calls.append("ratelimit")
        payload = {"limitPerHour": 3600, "pointsSpentThisHour": spent, "pointsResetIn": 900}
        self.ledger.record("rateLimit", {"rateLimitData": payload})
        return payload

    def encounter_rankings(self, encounter_id, difficulty=5, metric="dps", page=1):
        self.calls.append(f"rankings:{encounter_id}:page{page}")
        return {
            "id": encounter_id,
            "name": "Lightblinded Vanguard",
            "characterRankings": {
                "rankings": [
                    {"amount": 1.0, "report": {"code": "aBcD1234", "fightID": 7}},
                    {"amount": 0.9, "report": {"code": "aBcD1234", "fightID": 7}},
                ]
            },
        }


def test_the_command_writes_a_dump_a_json_file_and_a_measured_cost(tmp_path, monkeypatch):
    """End to end with the network stubbed: this is the shape the CI artifact has."""
    from wowdps import cli, warcraftlogs

    stub = StubClient(
        structure=structure_payload(),
        events={"DamageTaken": [damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)]},
        tables={},
    )
    monkeypatch.setattr(
        warcraftlogs.Credentials,
        "from_env",
        classmethod(lambda cls: warcraftlogs.Credentials("i", "s")),
    )
    monkeypatch.setattr(fightprobe, "WarcraftLogsClient", lambda *a, **k: stub)

    args = cli.build_parser().parse_args(
        # Encounter 3180 is Lightblinded Vanguard, which lives under MID1 since the
        # re-file -- the dump pairs a measurement with its *profile*, so the tier has
        # to be the one that holds it or there is nothing to pair with.
        [
            "fight-probe",
            "--tier",
            VOIDSPIRE_TIER,
            "--encounter",
            "3180",
            "--reports",
            "1",
            "--out",
            str(tmp_path),
        ]
    )
    assert fightprobe.cmd_fight_probe(args) == 0

    import json

    payload = json.loads((tmp_path / f"fight-probe-{VOIDSPIRE_TIER}.json").read_text())
    # The cost is the difference between the two bracketing readings, not a guess.
    assert payload["cost"]["pointsSpentThisRun"] == 40.0
    assert payload["encounters"][0]["peakTargets"]["median"] == 3
    # And the sampling is stated in the artifact rather than in a commit message.
    # The default order is "first", so it names the earliest kills.
    assert payload["order"] == "first"
    assert "earliest kills" in payload["sampling"]

    text = (tmp_path / f"fight-probe-{VOIDSPIRE_TIER}.txt").read_text()
    assert "Lightblinded Vanguard" in text and "profile vs measurement" in text


def test_every_setting_the_resume_checks_is_written_onto_the_encounter(tmp_path, monkeypatch):
    """The check and the record are two halves, and only the record is reachable
    from a real run.

    `is_complete` reads `order`, `eventBudget` and `difficulty` off the entry. If
    the writing half is dropped, every unit test of `is_complete` still passes --
    it is handed hand-built dicts -- while the check is inoperative against any
    payload the command actually produces. That is the `seen_difficulties` failure
    this repository already shipped once: a guard present and unable to fire.
    Verified by deleting each line in turn; without this test nothing goes red.
    """
    from wowdps import cli, warcraftlogs

    stub = StubClient(
        structure=structure_payload(),
        events={"DamageTaken": [damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)]},
        tables={},
    )
    monkeypatch.setattr(
        warcraftlogs.Credentials,
        "from_env",
        classmethod(lambda cls: warcraftlogs.Credentials("i", "s")),
    )
    monkeypatch.setattr(fightprobe, "WarcraftLogsClient", lambda *a, **k: stub)

    args = cli.build_parser().parse_args(
        [
            "fight-probe",
            "--tier",
            VOIDSPIRE_TIER,
            "--encounter",
            "3180",
            "--reports",
            "1",
            "--difficulty",
            "4",
            "--out",
            str(tmp_path),
        ]
    )
    assert fightprobe.cmd_fight_probe(args) == 0

    import json

    payload = json.loads((tmp_path / f"fight-probe-{VOIDSPIRE_TIER}.json").read_text())
    entry = payload["encounters"][0]
    # Per encounter, not only on the document: the payload-level field says what the
    # *last* run asked for, and every resumed run carries entries it did not collect.
    assert entry["difficulty"] == 4
    assert entry["order"] == args.order
    assert isinstance(entry["eventBudget"], int)

    # And the resume agrees with itself: the entry this run wrote counts as done for
    # the same settings and as outstanding for another difficulty.
    assert fightprobe.is_complete(entry, 1, entry["eventBudget"], entry["order"], 4) is True
    assert fightprobe.is_complete(entry, 1, entry["eventBudget"], entry["order"], 5) is False


def test_first_kills_are_taken_by_date_across_gathered_pages():
    """WCL sorts rankings by damage, so the earliest kills sit deep in the list. The
    selector reads the startTime every row carries and takes the earliest, across
    every gathered page, one fight per report."""
    from wowdps.warcraftlogs import select_report_fights

    def page(*rows):
        return {"characterRankings": {"rankings": list(rows)}}

    def row(code, fight, start):
        return {"report": {"code": code, "fightID": fight}, "startTime": start}

    # Page 1 is the highest damage (recent, geared, fast). Page 2 has the early kills.
    p1 = page(row("SPEED1", 1, 5000), row("SPEED2", 2, 5200))
    p2 = page(row("FIRST1", 1, 1000), row("FIRST2", 3, 1100), row("SPEED1", 9, 5000))

    first = select_report_fights([p1, p2], 2, order="first")
    assert [(code, fight) for code, fight, _ in first] == [("FIRST1", 1), ("FIRST2", 3)]
    # The kill time travels with the route: without it, nothing downstream can say
    # whether "the earliest kills we gathered" were early in absolute terms.
    assert [start for _, _, start in first] == [1000, 1100]

    top = select_report_fights([p1, p2], 2, order="top")
    assert [(code, fight) for code, fight, _ in top] == [("SPEED1", 1), ("SPEED2", 2)]


def test_one_fight_per_report_even_across_pages():
    from wowdps.warcraftlogs import select_report_fights

    pages = [
        {
            "characterRankings": {
                "rankings": [{"report": {"code": "A", "fightID": 1}, "startTime": 10}]
            }
        },
        {
            "characterRankings": {
                "rankings": [{"report": {"code": "A", "fightID": 2}, "startTime": 5}]
            }
        },
    ]
    # Same report on two pages is one kill; the first-seen fight id is kept.
    selected = select_report_fights(pages, 5, order="first")
    assert [(code, fight) for code, fight, _ in selected] == [("A", 1)]


def test_a_row_without_a_timestamp_sorts_last_not_first():
    """A missing startTime is zero, which would masquerade as the earliest kill."""
    from wowdps.warcraftlogs import select_report_fights

    pages = [
        {
            "characterRankings": {
                "rankings": [
                    {"report": {"code": "NOTS", "fightID": 1}},
                    {"report": {"code": "REAL", "fightID": 2}, "startTime": 999},
                ]
            }
        }
    ]
    selected = select_report_fights(pages, 2, order="first")
    assert [(code, fight) for code, fight, _ in selected] == [("REAL", 2), ("NOTS", 1)]


# --------------------------------------------------------------------------------
# Resuming a pass the point ceiling cut short
# --------------------------------------------------------------------------------


def test_a_completed_encounter_is_read_back_rather_than_refetched(tmp_path):
    """The whole point: points reset hourly, so an interrupted pass should continue,
    not start over. The first MID2 pass at 40 reports stopped with three bosses unread
    at 80% of the budget -- re-fetching the six it had would have spent the next hour
    on work already done."""
    from wowdps.fightprobe import is_complete, load_previous

    path = tmp_path / "fight-probe-MID2.json"
    path.write_text(
        json.dumps(
            {
                "encounters": [
                    {"encounterId": 3176, "fightsSampled": 40},
                    {"encounterId": 3177, "fightsSampled": 12},
                ]
            }
        ),
        encoding="utf-8",
    )
    previous = load_previous(path)
    # Keyed on (encounter, difficulty). These rows state none, so the key's second
    # member is None -- kept as a real key rather than dropped, on the same
    # "unknown is not wrong" rule the rest of the difficulty handling follows.
    assert set(previous) == {(3176, None), (3177, None)}
    assert is_complete(previous[(3176, None)], 40) is True
    assert is_complete(previous[(3177, None)], 40) is False


def test_one_payload_holds_both_difficulties_rather_than_one_replacing_the_other():
    """The owner's decision, 2026-08-26: both difficulties in one document.

    Keyed on the encounter alone -- which is what this did until then -- reading a
    boss at a second difficulty replaced the first row, and the document then stamped
    one `difficulty` over the lot. Four MID2 bosses have zero Mythic kills and real
    Heroic ones, so this is the case, not a hypothetical.
    """
    from wowdps.fightprobe import load_previous

    path = tmp_path_for_both()
    previous = load_previous(path)
    assert set(previous) == {(3176, 5), (3176, 4), (3177, 5)}
    # And the two rows of one boss stay distinguishable, which is the whole point.
    assert previous[(3176, 5)]["fightsSampled"] == 9
    assert previous[(3176, 4)]["fightsSampled"] == 30


def test_a_run_asks_the_resume_about_its_own_difficulty():
    """A Mythic row is not an answer to a Heroic run, and vice versa. Without the pair
    in the lookup a Heroic pass against a Mythic payload reports every boss finished
    and changes nothing -- the silent no-op this repository has shipped twice."""
    from wowdps.fightprobe import is_complete, load_previous

    previous = load_previous(tmp_path_for_both())
    assert (3177, 4) not in previous  # 3177 was never read at Heroic
    assert is_complete(previous[(3177, 5)], 5, None, None, 5) is True
    assert is_complete(previous[(3177, 5)], 5, None, None, 4) is False


def test_raising_the_sample_size_reopens_every_encounter():
    """Somebody who raises --reports means they want a bigger sample, not a skip."""
    from wowdps.fightprobe import is_complete

    assert is_complete({"fightsSampled": 40}, 40) is True
    assert is_complete({"fightsSampled": 40}, 60) is False


def test_a_missing_or_corrupt_previous_payload_starts_a_fresh_pass(tmp_path):
    """Never a hard failure: the resume is an optimisation, and losing it costs
    points rather than correctness."""
    from wowdps.fightprobe import load_previous

    assert load_previous(tmp_path / "nothing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_previous(broken) == {}


def test_an_encounter_with_no_id_is_ignored_rather_than_keyed_on_none(tmp_path):
    from wowdps.fightprobe import load_previous

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"encounters": [{"fightsSampled": 5}]}), encoding="utf-8")
    assert load_previous(path) == {}


def test_a_bigger_event_budget_reopens_an_encounter_that_has_enough_fights():
    """The half of the sample that is not the number of kills.

    The 30-kill MID2 pass had every fight it asked for and still produced no band on
    three bosses, because a bounded event fetch stopped partway through each pull --
    Midnight Falls was read to 17%. Raising --max-pages to fix that must not be a
    silent no-op just because the fight count is already satisfied.
    """
    from wowdps.fightprobe import is_complete

    collected = {"fightsSampled": 30, "eventBudget": 3 * 10000}
    assert is_complete(collected, 30, 3 * 10000) is True
    assert is_complete(collected, 30, 8 * 10000) is False
    # Lowering it is not a reason to re-fetch: what is stored already covers more.
    assert is_complete(collected, 30, 1 * 10000) is True


def test_an_encounter_with_no_recorded_budget_is_left_alone():
    """Treating unknown as zero would re-open a whole zone on the next run for
    everybody holding a payload written before the budget was recorded."""
    from wowdps.fightprobe import is_complete

    assert is_complete({"fightsSampled": 30}, 30, 8 * 10000) is True


def test_a_different_difficulty_reopens_an_encounter_that_has_enough_fights():
    """Heroic Sszorak and Mythic Sszorak are different fights, not two samples of one.

    The failure this prevents is worse than the `order` and `max_pages` ones it sits
    beside, and it is not a no-op: a Heroic run against a Mythic payload replaces the
    encounters it reaches, leaves the rest on Mythic, and stamps `difficulty: 4` over
    the whole document. A target band pooled across two difficulties is a wrong answer
    wearing a right one's label, and nothing downstream can tell.
    """
    from wowdps.fightprobe import is_complete

    collected = {"fightsSampled": 30, "difficulty": 5}
    assert is_complete(collected, 30, None, None, 5) is True
    assert is_complete(collected, 30, None, None, 4) is False
    # Asking about nothing in particular is not asking for a different difficulty.
    assert is_complete(collected, 30, None, None, None) is True


def test_an_encounter_with_no_recorded_difficulty_is_left_alone():
    """Same "unknown is not zero" rule the budget and the order already follow.

    Every payload written before 2026-08-25 is in this state, and reading the absence
    as a mismatch would re-open every zone for everybody on the next hourly run.
    """
    from wowdps.fightprobe import is_complete

    assert is_complete({"fightsSampled": 30}, 30, None, None, 4) is True


def test_the_fight_count_still_wins_regardless_of_budget():
    from wowdps.fightprobe import is_complete

    assert is_complete({"fightsSampled": 5, "eventBudget": 99999999}, 30, 10) is False


class _ReportSearchClient:
    """A client that answers the report-search queries and nothing else."""

    def __init__(self, pages, kills, limit=100, starts=None):
        self._pages = pages
        self._kills = kills
        self._starts = starts or {}
        self._limit = limit
        self.ledger = type("L", (), {"limit_per_hour": None, "last_reading": None})()
        self.kills_asked = []

    def encounter_zone(self, encounter_id):
        return {"id": 7, "name": "The Voidspire", "frozen": False}

    def reports_in_window(self, zone_id, start_ms, end_ms, page=1, limit=100):
        self.window = (start_ms, end_ms)
        rows = self._pages[page - 1] if page <= len(self._pages) else []
        return {"data": rows}

    def report_kills(self, code):
        # Unfiltered by encounter, exactly as the real client now asks: one request
        # per report for a whole zone rather than one per report per boss. Returns
        # the report's own start time with the fights, because ReportFight.startTime
        # is relative to it.
        self.kills_asked.append(code)
        return self._starts.get(code, 0.0), self._kills.get(code, [])


def _settings(**over):
    from wowdps.fightprobe import ProbeSettings

    base = dict(
        encounter_ids=(42,),
        difficulty=5,
        metric="dps",
        reports=2,
        rankings_page=1,
        order="public",
        rankings_pages=1,
        streams=("damage",),
        events_limit=10,
        max_pages=1,
        point_ceiling=0.8,
        significant_share=0.01,
        report_limit=2,
        report_pages=5,
    )
    base.update(over)
    return ProbeSettings(**base)


def test_the_report_search_finds_a_kill_the_rankings_never_carried():
    """The whole point of the route: a public log Warcraft Logs did not rank.

    The anchor is the earliest *ranked* kill. A kill before it is one the ranking
    route could not have returned at any window width.
    """
    from wowdps.fightprobe import _public_first_kills

    # A real anchor, because kill times are epoch and are now compared as such.
    anchor = 1_700_000_000_000.0
    # ReportFight.startTime is relative to the report, so each report carries a base
    # and the fight sits at a small offset inside it -- the real shape of the data.
    base = 3_600_000.0

    def kill(offset):
        return [
            {"id": 3, "encounterID": 42, "kill": True, "startTime": offset, "endTime": offset + 200}
        ]

    client = _ReportSearchClient(
        pages=[[{"code": "EARLY"}, {"code": "LATE"}], [{"code": "EARLIER"}]],
        kills={"EARLY": kill(base), "LATE": kill(base), "EARLIER": kill(base)},
        starts={
            "EARLY": anchor - 5_000 - base,
            "LATE": anchor + 9_000 - base,
            "EARLIER": anchor - 90_000 - base,
        },
    )

    pairs, outcome = _public_first_kills(client, 42, anchor, _settings())

    assert [code for code, _, _ in pairs] == ["EARLIER", "EARLY"]
    assert outcome.beat_anchor == 2
    assert outcome.reports_seen == 3
    assert "2 earlier than the best-parse sample" in outcome.summary(anchor)


def test_paging_stops_on_a_short_page_rather_than_on_an_unverified_field():
    """`ReportPagination` could not be introspected, so `has_more_pages` is not trusted."""
    from wowdps.fightprobe import _public_first_kills

    client = _ReportSearchClient(pages=[[{"code": "A"}]], kills={})
    _, outcome = _public_first_kills(client, 42, 1_700_000_000_000.0, _settings())

    # One page returned fewer rows than the limit of 2, so no second page was asked for.
    assert outcome.pages_read == 1


def test_without_a_ranked_timestamp_there_is_nothing_to_anchor_on():
    """Reported and refused, rather than searching from the epoch for every boss."""
    from wowdps.fightprobe import _public_first_kills

    client = _ReportSearchClient(pages=[], kills={})
    pairs, outcome = _public_first_kills(client, 42, 0.0, _settings())

    assert pairs == []
    assert outcome.reports_seen == 0


def test_the_window_is_anchored_on_the_earliest_ranked_kill():
    from wowdps.fightprobe import _public_first_kills
    from wowdps.firstkills import DAY_MS

    anchor = 100.0 * DAY_MS
    client = _ReportSearchClient(pages=[], kills={})
    _public_first_kills(client, 42, anchor, _settings(lookback_days=10, forward_days=14))

    assert client.window == (90 * DAY_MS, 114 * DAY_MS)


def test_switching_order_re_opens_an_encounter_rather_than_counting_it_done():
    """A different --order is a different sample, not more of the same one.

    Without this the resume silently defeats the switch: everything collected at
    `first` counts as done, the report search never runs, and the pass reports
    success having changed nothing. That is how the max_pages default went inert,
    and it cost a whole afternoon of hourly runs to notice.
    """
    from wowdps.fightprobe import is_complete

    entry = {"fightsSampled": 30, "eventBudget": 200_000, "order": "first"}

    assert is_complete(entry, 30, 200_000, "first") is True
    assert is_complete(entry, 30, 200_000, "public") is False


def test_an_entry_from_before_the_order_was_recorded_is_left_alone():
    """Unknown is not 'wrong order' -- the same rule the event budget follows."""
    from wowdps.fightprobe import is_complete

    entry = {"fightsSampled": 30, "eventBudget": 200_000}
    assert is_complete(entry, 30, 200_000, "public") is True


def test_the_point_ceiling_returns_what_was_found_instead_of_losing_the_pass():
    """A budget abort inside the report search must not escape.

    It did on the first live run: 2880 of 3600 points spent, the exception
    propagated out of probe_encounter, and the traceback threw away all nine
    encounters instead of publishing the eight that were already read. The module's
    own rule is that partial evidence comes back and says it is partial.
    """
    from wowdps.fightprobe import PointBudgetExhausted, _public_first_kills

    anchor = 1_700_000_000_000.0

    class _Ceiling(_ReportSearchClient):
        def report_kills(self, code):
            if code == "SECOND":
                raise PointBudgetExhausted("2880 of 3600 points spent this hour")
            return super().report_kills(code)

    client = _Ceiling(
        pages=[[{"code": "FIRST"}, {"code": "SECOND"}]],
        kills={
            "FIRST": [{"id": 1, "encounterID": 42, "kill": True, "startTime": 0, "endTime": 1_000}]
        },
        starts={"FIRST": anchor - 1_000},
    )

    pairs, outcome = _public_first_kills(client, 42, anchor, _settings())

    # What was reached still comes back.
    assert [code for code, _, _ in pairs] == ["FIRST"]
    assert outcome.aborted is not None
    assert outcome.truncated is True
    # And the summary refuses to call a partial search a complete answer.
    assert "did not finish" not in outcome.summary(anchor)
    assert "STOPPED EARLY" in outcome.summary(anchor)


def test_a_stopped_search_that_found_nothing_earlier_does_not_claim_there_is_nothing():
    from wowdps.firstkills import SearchOutcome

    outcome = SearchOutcome(reports_seen=5, pages_read=1, kills_found=2, aborted="ceiling")
    assert "none earlier so far, but the search did not finish" in outcome.summary(1_000.0)


def test_the_boss_list_comes_from_the_tier_not_the_newest_ranked_zone():
    """Probing "the newest unfrozen zone" filed another raid's kills under MID2.

    That shipped on 2026-08-17: the probe read The Voidspire's nine encounters and
    published them under MID2, whose raid is The Venomous Abyss, leaving 17
    encounters in a file that should hold eight. Same fault cmd_verify had, in a
    second place.
    """
    from wowdps.fightprobe import _tier_encounters
    from wowdps.fightprofile import FightProfile, TierProfiles

    profiles = TierProfiles(
        tier="MID2",
        note="",
        profiles={
            53470: FightProfile(tier="MID2", encounter_id=53470, name="Nek'zali", difficulty=5),
            53420: FightProfile(tier="MID2", encounter_id=53420, name="Sszorak", difficulty=5),
        },
    )
    assert _tier_encounters(profiles) == [53420, 53470]


def test_a_tier_with_no_fight_profiles_refuses_rather_than_borrowing_a_raid():
    """A full set of real measurements filed under the wrong season is worse than none."""
    from wowdps.fightprobe import _tier_encounters
    from wowdps.fightprofile import TierProfiles
    from wowdps.warcraftlogs import WarcraftLogsError

    with pytest.raises(WarcraftLogsError, match="no fight profiles"):
        _tier_encounters(TierProfiles(tier="MID9", note="", profiles={}))


def test_a_search_that_saw_everything_counts_as_done_however_few_kills_it_found():
    """Otherwise an encounter with genuinely few kills is re-opened forever.

    MID2 stalled on exactly this for two days: every hourly run restarted at the
    first encounter, spent the point ceiling re-reading the four it already had,
    and never reached the other four -- which sat at `sampled: null` while the raid
    was open.
    """
    from wowdps.fightprobe import is_complete

    short = {"fightsSampled": 3, "eventBudget": 200_000, "order": "public"}
    assert is_complete(short, 30, 200_000, "public") is False

    short["searchExhausted"] = True
    assert is_complete(short, 30, 200_000, "public") is True


def test_a_truncated_search_is_not_exhausted():
    """A page limit or the point ceiling stopping the walk means more may exist."""
    from wowdps.firstkills import SearchOutcome

    assert SearchOutcome(truncated=True).aborted is None
    # The probe's rule, stated where it can be checked: exhausted means the walk
    # ended because it ran out of reports, not because something stopped it.
    for outcome in (
        SearchOutcome(truncated=True),
        SearchOutcome(aborted="ceiling"),
        SearchOutcome(truncated=True, aborted="ceiling"),
    ):
        assert not (not outcome.truncated and outcome.aborted is None)
    assert SearchOutcome().aborted is None and not SearchOutcome().truncated


def test_the_search_counts_the_difficulties_it_saw():
    """The diagnostic fires. Its guard was permanently False before this.

    `seen_difficulties` was declared and never written to, so
    `if not rows and seen_difficulties` could not be true on any run and the one
    thing that explains an empty encounter never printed. Present, and inoperative
    -- the same shape as a settle guard that misses a nested stamp.
    """
    from wowdps.fightprobe import _public_first_kills

    base = 1_700_000_000_000.0

    def heroic(fight_id):
        return [
            {
                "id": fight_id,
                "encounterID": 42,
                "kill": True,
                "startTime": 10_000,
                "difficulty": 4,
            }
        ]

    client = _ReportSearchClient(
        pages=[[{"code": "AAA"}, {"code": "BBB"}]],
        kills={"AAA": heroic(1), "BBB": heroic(2)},
        starts={"AAA": base, "BBB": base},
        limit=2,
    )

    pairs, outcome = _public_first_kills(client, 42, base, _settings(difficulty=5))

    # Two Heroic kills exist and neither is the Mythic kill that was asked for, so
    # "0 kills" would be true and useless. This is the number that names the fix.
    assert pairs == []
    assert outcome.difficulties_seen == {4: 2}


def tmp_path_for_both():
    """A payload holding one boss at two difficulties and another at one."""
    import tempfile

    path = Path(tempfile.mkdtemp()) / "fight-probe-MID2.json"
    path.write_text(
        json.dumps(
            {
                "difficulty": 4,
                "difficulties": [5, 4],
                "encounters": [
                    {"encounterId": 3176, "difficulty": 5, "fightsSampled": 9},
                    {"encounterId": 3176, "difficulty": 4, "fightsSampled": 30},
                    {"encounterId": 3177, "difficulty": 5, "fightsSampled": 6},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------------
# #135: does a duplicate upload state the same start time?
# --------------------------------------------------------------------------------


def _observation_with(*starts_and_durations) -> EncounterObservation:
    """An observation whose fights carry only what the reading looks at."""
    from wowdps.fightextract import TargetCountTimeline

    observation = EncounterObservation(3180, "Lightblinded Vanguard", 5)
    for index, (started_at, duration) in enumerate(starts_and_durations):
        fight = observe_fight(
            report_code=f"r{index}",
            fight={
                "id": 7,
                "kill": True,
                "size": 20,
                "startTime": 0,
                "endTime": int(duration * 1000),
                "friendlyPlayers": list(range(1, 21)),
                "enemyNPCs": [],
                "phaseTransitions": [],
            },
            damage_events=[],
            death_events=[],
            aura_events=[],
            phase_metadata=[],
            started_at=started_at,
        )
        assert isinstance(fight.significant_timeline, TargetCountTimeline)
        observation.fights.append(fight)
    return observation


def test_the_probe_reports_what_a_start_time_rule_would_have_to_fit_between():
    """#135. The reading is taken on every pass because it costs nothing -- every
    row has carried `startedAt` since #134 -- and the filter waits for the answer.

    Both numbers are printed, because a within-group spread with nothing to compare
    it against cannot say whether a threshold exists.
    """
    base = 1_700_000_000_000.0
    lines = fightprobe.describe_upload_start_times(
        _observation_with(
            (base, 300.0), (base + 300, 300.0), (base + 90_000, 240.0)
        ).upload_start_times()
    )
    joined = "\n".join(lines)
    assert "1 of 2 kill(s) were uploaded more than once" in joined
    assert "0.300s" in joined
    assert "89.700s" in joined
    assert "would have to sit between those two" in joined


def test_the_probe_says_nothing_when_no_kill_was_uploaded_twice():
    """A run printing `widest: None` on every boss of a clean tier is noise that
    trains a reader to skip the line -- and the absence IS the answer here."""
    base = 1_700_000_000_000.0
    assert (
        fightprobe.describe_upload_start_times(
            _observation_with((base, 300.0), (base + 5_000, 240.0)).upload_start_times()
        )
        == []
    )


def test_the_probe_names_a_sample_a_start_time_rule_could_not_separate():
    """The finding that would kill the idea, said rather than left to arithmetic:
    two different kills stating starts closer together than one kill's uploads do."""
    base = 1_700_000_000_000.0
    lines = fightprobe.describe_upload_start_times(
        _observation_with(
            (base, 300.0), (base + 10_000, 300.0), (base + 10_100, 240.0)
        ).upload_start_times()
    )
    assert any("cannot separate this sample" in line for line in lines)


def test_a_row_with_no_start_time_is_named_in_the_transcript():
    """It can be placed on neither side of a threshold, so it is counted apart."""
    base = 1_700_000_000_000.0
    lines = fightprobe.describe_upload_start_times(
        _observation_with(
            (base, 300.0), (base + 200, 300.0), (0.0, 300.0), (base + 60_000, 240.0)
        ).upload_start_times()
    )
    assert any("state no start time" in line for line in lines)


def test_the_payload_wide_reading_never_pools_two_encounters_rows():
    """Reading the payload's fights as one flat list is simpler and wrong twice.

    `group_duplicate_uploads` merges on length and curve alone, so two bosses' pulls
    of the same length group together and count as one kill uploaded twice; and
    `closestBetweenGroups` would then measure how close two DIFFERENT encounters' kills
    sit on the clock, which is a fact about a raid night's schedule. Both push the
    "different kills" number down -- the direction that makes a workable threshold look
    impossible.

    Two encounters, each holding two pulls of identical length, three seconds apart on
    the clock. Pooled they are one group of four with nothing to compare it to; read
    apart they are two encounters of one duplicate group each.
    """
    from wowdps import fightdataset

    def rows(base: float) -> list[dict]:
        return [
            {
                "reportCode": f"r{index}",
                "durationSeconds": 300.0,
                "startedAt": base + index * 100,
                "steps": [[0.0, 2]],
            }
            for index in range(2)
        ]

    base = 1_700_000_000_000.0
    per_encounter = [
        fightdataset.upload_start_times(rows(base)),
        fightdataset.upload_start_times(rows(base + 3_000)),
    ]
    folded = fightprobe.fold_upload_start_times(per_encounter)

    # Two encounters, one duplicate group each, and NOT one group of four.
    assert folded["groups"] == 2
    assert folded["groupsWithSeveralUploads"] == 2
    assert folded["widestWithinGroup"] == 0.1
    # Neither encounter has a second group, so nothing measures a cross-group gap --
    # and the 3.0s between the two encounters is emphatically not it.
    assert folded["closestBetweenGroups"] is None


def test_the_fold_keeps_the_extremes_a_threshold_would_have_to_fit_between():
    """A rule must tolerate the widest within-group disagreement ANYWHERE and stay
    under the closest cross-group pair ANYWHERE, so the fold takes max and min."""
    folded = fightprobe.fold_upload_start_times(
        [
            {
                "groups": 3,
                "groupsWithSeveralUploads": 1,
                "unstamped": 0,
                "widestWithinGroup": 0.2,
                "closestBetweenGroups": 90.0,
            },
            {
                "groups": 2,
                "groupsWithSeveralUploads": 1,
                "unstamped": 2,
                "widestWithinGroup": 0.9,
                "closestBetweenGroups": 40.0,
            },
            # An encounter that could produce neither number contributes neither,
            # rather than a zero that would collapse the max or the min.
            {
                "groups": 1,
                "groupsWithSeveralUploads": 0,
                "unstamped": 0,
                "widestWithinGroup": None,
                "closestBetweenGroups": None,
            },
        ]
    )
    assert folded == {
        "groups": 6,
        "groupsWithSeveralUploads": 2,
        "unstamped": 2,
        "widestWithinGroup": 0.9,
        "closestBetweenGroups": 40.0,
    }


def test_the_command_reads_the_start_times_per_encounter_and_not_pooled(tmp_path, monkeypatch):
    """The call site, driven end to end -- and the reason this test exists.

    `fold_upload_start_times` has its own unit tests, and swapping the CALL SITE back
    to one flat list over every encounter's rows left every one of them green: the
    fold was tested and the thing that uses it was not. That is the shape this repo
    keeps producing (`seen_difficulties` declared and never written to,
    `PointBudgetExhausted` beside the guard rather than inside it), so the fix is a
    test that runs the command.

    Two encounters, each holding two byte-identical uploads of one kill and nothing
    else. Pooled, `group_duplicate_uploads` merges all four into ONE group -- their
    lengths and curves agree -- and the transcript would say "1 of 1 kill(s)". Read
    per encounter it says "2 of 2".
    """
    from wowdps import cli, fightdataset, warcraftlogs

    stub = StubClient(
        structure=structure_payload(),
        events={"DamageTaken": [damage(s, a) for a in (10, 11, 12) for s in (0.5, 299.0)]},
        tables={},
    )
    monkeypatch.setattr(
        warcraftlogs.Credentials,
        "from_env",
        classmethod(lambda cls: warcraftlogs.Credentials("i", "s")),
    )
    monkeypatch.setattr(fightprobe, "WarcraftLogsClient", lambda *a, **k: stub)

    real = fightdataset.upload_start_times
    captured: list[int] = []

    def spy(fights):
        captured.append(len(fights))
        return real(fights)

    monkeypatch.setattr(fightdataset, "upload_start_times", spy)

    # NO `--encounter`, deliberately: with one encounter, "pooled" and "per encounter"
    # are the same list and the canary cannot fire. The first version of this test did
    # exactly that and stayed green against the pooled call site. MID1 carries nine
    # encounters, and the stub answers each identically -- so pooled they would be one
    # group of nine byte-identical pulls, which is the merge this guards against.
    args = cli.build_parser().parse_args(
        ["fight-probe", "--tier", VOIDSPIRE_TIER, "--reports", "1", "--out", str(tmp_path)]
    )
    assert fightprobe.cmd_fight_probe(args) == 0

    import json

    payload = json.loads((tmp_path / f"fight-probe-{VOIDSPIRE_TIER}.json").read_text())
    encounters = payload["encounters"]
    assert len(encounters) > 1, "this test needs several encounters to say anything"

    # Once per encounter, each handed only that encounter's rows -- never one call
    # over the union, which is what would let two bosses' pulls group as one kill.
    assert captured == [len(entry.get("fights") or []) for entry in encounters]
