"""The probe's orchestration, driven by a fake client instead of the real API.

The queries themselves cannot be tested here -- credentials are CI secrets, and the
answer to "does this GraphQL document parse" only comes from the server. What can be
tested is everything between the response and the report: that the right payload
reaches the right extractor, that the point ceiling stops a pass instead of running
into a 429, and that the readable dump says the things a reader needs in order not
to over-read it.
"""

from __future__ import annotations

import pytest

from wowdps import fightprobe
from wowdps.fightextract import EncounterObservation, observe_fight
from wowdps.fightprofile import load_profiles
from wowdps.warcraftlogs import PointLedger

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
        fightprobe._check_budget(client, 0.8)


def test_a_budget_with_no_reading_yet_does_not_stop_anything():
    client = FakeClient(structure=structure_payload(), events={}, tables={})
    fightprobe._check_budget(client, 0.8)  # must not raise


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
    return observe_fight(
        report_code=code,
        fight={
            "id": 1,
            "encounterID": 3180,
            "name": "Lightblinded Vanguard",
            "size": 20,
            "startTime": START,
            "endTime": END,
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
    text = "\n".join(fightprobe.render(observation, load_profiles("MID2").get(3180)))
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
        ["fight-probe", "--encounter", "3180", "--reports", "1", "--out", str(tmp_path)]
    )
    assert fightprobe.cmd_fight_probe(args) == 0

    import json

    payload = json.loads((tmp_path / "fight-probe-MID2.json").read_text())
    # The cost is the difference between the two bracketing readings, not a guess.
    assert payload["cost"]["pointsSpentThisRun"] == 40.0
    assert payload["encounters"][0]["peakTargets"]["median"] == 3
    # And the sampling bias is stated in the artifact rather than in a commit message.
    assert "speed-kill" in payload["sampling"]

    text = (tmp_path / "fight-probe-MID2.txt").read_text()
    assert "Lightblinded Vanguard" in text and "profile vs measurement" in text
