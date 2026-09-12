"""The catalogue: the scrub, the two stages, the alarms and the gate.

The contract is `docs/progress-catalogue.md`. Every test here pins a claim that file
makes, and the ones whose canary is worth naming say so in the test's own docstring.

No live query is sent anywhere in this file: the stub answers what the CLIENT
returns, never what the service does -- the distinction `addspawns` paid for once,
where a stub built from the envelope would have passed against broken code.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wowdps import catalogue
from wowdps.warcraftlogs import WarcraftLogsError

ZONE = 53
MYTHIC = 5
HEROIC = 4
ENCOUNTER = 3421
#: A live report start, well past the year-2000 floor `kills_in_report` refuses under.
REPORT_START = 1_723_456_000_000


def zone_payload(zone_id=ZONE, *, encounters=((ENCOUNTER, "The Twin Fangs"),), frozen=False):
    return {
        "id": zone_id,
        "name": "The Venomous Abyss",
        "frozen": frozen,
        "encounters": [{"id": e, "name": n} for e, n in encounters],
    }


def kill_fight(fight_id=32, encounter=ENCOUNTER, difficulty=MYTHIC, offset=100_000, length=434_752):
    return {
        "id": fight_id,
        "encounterID": encounter,
        "difficulty": difficulty,
        "kill": True,
        "startTime": offset,
        "endTime": offset + length,
    }


def actors(*, player_name="Somebody", npc_name="Broodling of Ithraz"):
    """A player, its pet, a pet of that pet, and an NPC the boss owns nothing of."""
    return [
        {
            "id": 1,
            "gameID": 111,
            "name": player_name,
            "type": "Player",
            "subType": "Mage",
            "petOwner": None,
        },
        {"id": 2, "gameID": 222, "name": "Fluffy", "type": "Pet", "subType": "Pet", "petOwner": 1},
        {
            "id": 3,
            "gameID": 333,
            "name": "Mirror Image",
            "type": "Pet",
            "subType": "Pet",
            "petOwner": 2,
        },
        {
            "id": 11,
            "gameID": 270898,
            "name": npc_name,
            "type": "NPC",
            "subType": "Boss",
            "petOwner": None,
        },
    ]


def structure(*, code="aBcD", fights=None, master=True, player_name="Somebody"):
    report = {
        "code": code,
        "title": "a title somebody wrote",
        "startTime": REPORT_START,
        "phases": [
            {
                "encounterID": 9999,
                "separatesWipes": False,
                "phases": [{"id": 1, "name": "Another boss's phase", "isIntermission": False}],
            },
            {
                "encounterID": ENCOUNTER,
                "separatesWipes": True,
                "phases": [{"id": 1, "name": "Fangs", "isIntermission": False}],
            },
        ],
        "fights": fights
        if fights is not None
        else [
            dict(
                kill_fight(),
                name="The Twin Fangs",
                size=20,
                friendlyPlayers=[1],
                enemyNPCs=[{"id": 11, "gameID": 270898, "instanceCount": 84, "groupCount": 6}],
                phaseTransitions=[{"id": 1, "startTime": 0}],
            )
        ],
    }
    if master:
        report["masterData"] = {
            "actors": actors(player_name=player_name),
            "abilities": [{"gameID": 1246385, "name": "Avenging Wrath", "type": 1}],
        }
    return report


class StubClient:
    """Answers from canned zones, report lists, report kills and fight structures."""

    def __init__(self, *, zones=None, reports=None, kills=None, structures=None, readings=None):
        self.zones = zones if zones is not None else {ZONE: zone_payload()}
        self.reports = reports if reports is not None else {ZONE: [{"code": "aBcD"}]}
        self.kills = kills if kills is not None else {"aBcD": (REPORT_START, [kill_fight()])}
        self.structures = structures
        self.readings = list(readings or [])
        self.calls: list[tuple] = []
        self.rate_limit_calls = 0
        self.ledger = SimpleNamespace(queries=0)

    def rate_limit(self):
        self.rate_limit_calls += 1
        reading = self.readings.pop(0) if self.readings else None
        if isinstance(reading, Exception):
            raise reading
        return reading or {
            "limitPerHour": 18000.0,
            "pointsSpentThisHour": 100.0,
            "pointsResetIn": 900,
        }

    def zone(self, zone_id, *, cache=True):
        self.calls.append(("zone", zone_id))
        answer = self.zones.get(zone_id)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def reports_in_window(self, zone_id, start_ms, end_ms, page=1, limit=100):
        self.calls.append(("reports", zone_id, page))
        rows = self.reports.get(zone_id, [])
        if isinstance(rows, Exception):
            raise rows
        return {"data": rows[(page - 1) * limit : page * limit]}

    def report_kills(self, code):
        self.calls.append(("report_kills", code))
        answer = self.kills.get(code)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer else (0.0, [])

    def fight_structure(self, code, encounter_id, difficulty):
        self.calls.append(("structure", code, encounter_id, difficulty))
        if self.structures is None:
            return structure(code=code)
        answer = self.structures.get((code, encounter_id, difficulty))
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}


def options(**overrides):
    base = {"zones": (ZONE,), "difficulties": (MYTHIC,), "deadline_minutes": 60}
    base.update(overrides)
    return catalogue.CatalogueOptions(**base)


def run(client, out: Path, **overrides):
    return catalogue.run_catalogue(client, out, options(**overrides), run_id="TEST")


# ── the scrub ───────────────────────────────────────────────────────────────────


def test_a_player_and_everything_it_owns_lose_their_name_and_the_npc_keeps_its():
    """The drop follows OWNERSHIP transitively, not a type test.

    `masterData` types a hunter's pet, a Mirror Image and a boss's add all as ``Pet``,
    so a type test would keep a player's pet name and the drop would be inoperative on
    exactly the actors it exists for. The pet-of-a-pet is in the fixture because a
    non-transitive walk passes the two-level case.
    """
    scrubbed = {a["id"]: a for a in catalogue.scrub_actors(actors())}
    assert "name" not in scrubbed[1], "the player kept its name"
    assert "name" not in scrubbed[2], "the pet kept its name"
    assert "name" not in scrubbed[3], "the pet of a pet kept its name -- the walk is not transitive"
    assert scrubbed[11]["name"] == "Broodling of Ithraz", "the NPC name is the readable handle"
    assert scrubbed[11]["petOwner"] is None and scrubbed[2]["petOwner"] == 1


def test_the_leak_check_does_not_share_the_scrubs_predicate():
    """The cross-check reads ``friendlyPlayers``, from a different part of the payload.

    A guard built on ``type``/``petOwner`` cannot catch those fields changing shape:
    the drop stops recognising a player, the guard stops recognising one too, and the
    names go to disk under a green run. Here the fixture renames ``type``, so
    `scrub_actors` keeps every name -- and the fight's own player list still names the
    id.
    """
    renamed = [dict(a, kind=a.pop("type")) for a in actors()]
    scrubbed = catalogue.scrub_actors(renamed)
    assert all("name" in a for a in scrubbed), (
        "the fixture must break the scrub, or it tests nothing"
    )
    assert catalogue.leaked_player_names(scrubbed, [1]) == [1]
    # And it is one-sided, which the docstring states rather than hides: the pet is
    # not in `friendlyPlayers`, so its surviving name is not caught here.
    assert 2 not in catalogue.leaked_player_names(scrubbed, [1])


def test_a_clean_scrub_leaks_nothing():
    assert catalogue.leaked_player_names(catalogue.scrub_actors(actors()), [1, 2, 3]) == []


# ── Stufe 3 ─────────────────────────────────────────────────────────────────────


def test_times_are_made_absolute_and_a_missing_base_is_refused():
    """``ReportFight.startTime`` counts from the report's start, not the epoch.

    The unit error this project paid for once: used raw, every kill is a number near
    zero that sorts before every real date, and a search reported 100% of kills as
    earlier than the ranked sample. A row whose base is unknown is refused rather than
    written near the epoch, because it cannot be ranked against rows whose base is
    known.
    """
    kills, refusals = catalogue.kills_in_report("aBcD", REPORT_START, [kill_fight(offset=100_000)])
    assert kills[0]["sMs"] == REPORT_START + 100_000
    assert kills[0]["eMs"] == REPORT_START + 100_000 + 434_752

    none, refused = catalogue.kills_in_report("aBcD", 0.0, [kill_fight()])
    assert none == [] and [r["outcome"] for r in refused] == ["unknown-time-base"]


def test_a_ptr_encounter_id_and_a_wipe_are_each_refused_by_name():
    """Two refusals, and the PTR one is the shape a downstream import cannot catch."""
    kills, refusals = catalogue.kills_in_report(
        "aBcD",
        REPORT_START,
        [
            kill_fight(fight_id=1, encounter=53421),
            dict(kill_fight(fight_id=2), kill=False),
            kill_fight(fight_id=3),
        ],
    )
    assert [k["f"] for k in kills] == [3]
    assert [(r["fightId"], r["outcome"]) for r in refusals] == [(1, "ptr-id"), (2, "not-a-kill")]


def test_the_ptr_floor_is_the_sweeps_own_constant():
    """One constant, imported -- two spellings of it is what drifts."""
    from wowdps import progresssweep

    assert catalogue.PTR_TWIN_ID_FLOOR is progresssweep.PTR_TWIN_ID_FLOOR


def test_killslisted_counts_the_rows_the_query_returned():
    """The field is named for what the query can deliver, not for what would be nicer.

    ``fights(killType: Kills)`` filters SERVER-side, so "every fight, kills and wipes"
    is not obtainable from this document at all -- an earlier draft of the contract
    said it was. A name that promises more than its computation delivers is the
    failure this repository keeps recording, so the field is renamed rather than
    re-described.
    """
    line = catalogue.report_line(ZONE, "aBcD", REPORT_START, [], 7, at="T", run_id="R")
    assert line["killsListed"] == 7
    assert "fightsSeen" not in line
    assert "endedAtMs" not in line, "REPORT_KILLS_QUERY selects no report endTime"


# ── Stufe 2 ─────────────────────────────────────────────────────────────────────


def test_the_phase_names_are_this_encounters_entry_and_not_the_outer_array():
    """``report.phases`` is per ENCOUNTER; the fixture holds another boss's entry first."""
    names, separates = catalogue.phase_names(structure(), ENCOUNTER)
    assert [p["name"] for p in names] == ["Fangs"]
    assert separates is True
    assert catalogue.phase_names(structure(), 1234) == ([], None)


def test_a_foreign_difficulty_is_refused_and_a_stated_none_is_allowed_through():
    """`harvest`'s three-way rule: unknown is not wrong.

    The fetch is already scoped by difficulty, so a row STATING another one means the
    scoping did not hold. A row stating none is a real kill whose difficulty the
    payload omits, and dropping it would lose it for a reason nobody could see.
    """
    report = structure(fights=[dict(kill_fight(), difficulty=HEROIC)])
    assert catalogue.pick_fight(report, 32, MYTHIC) == (None, "difficulty-mismatch")

    silent = structure(fights=[dict(kill_fight(), difficulty=None)])
    fight, reason = catalogue.pick_fight(silent, 32, MYTHIC)
    assert reason is None and fight["id"] == 32

    assert catalogue.pick_fight(structure(), 999, MYTHIC) == (None, "structure-error")


def test_a_kill_line_drops_the_title_and_keeps_the_ability_names():
    """An ability name is what makes an aura readable and is not personal."""
    line = catalogue.kill_line(
        ZONE,
        ENCOUNTER,
        MYTHIC,
        "aBcD",
        structure(),
        structure()["fights"][0],
        at="T",
        run_id="R",
    )
    assert "title" not in json.dumps(line)
    assert line["abilities"][0]["name"] == "Avenging Wrath"
    assert line["startedAtMs"] == REPORT_START + 100_000
    assert line["lengthMs"] == 434_752
    assert line["separatesWipes"] is True


def test_an_absent_masterdata_and_a_leaked_name_are_each_an_alarm():
    """Both states make the file set write nothing, rather than logging and going on."""
    with pytest.raises(catalogue.CatalogueAlarm, match="masterData"):
        catalogue.kill_line(
            ZONE,
            ENCOUNTER,
            MYTHIC,
            "aBcD",
            structure(master=False),
            structure()["fights"][0],
            at="T",
            run_id="R",
        )

    broken = structure()
    broken["masterData"]["actors"] = [
        dict(a, kind=a.pop("type")) for a in broken["masterData"]["actors"]
    ]
    with pytest.raises(catalogue.CatalogueAlarm, match="scrub is inoperative"):
        catalogue.kill_line(
            ZONE,
            ENCOUNTER,
            MYTHIC,
            "aBcD",
            broken,
            broken["fights"][0],
            at="T",
            run_id="R",
        )


# ── the two stages end to end ───────────────────────────────────────────────────


def test_stufe_3_writes_per_zone_and_stufe_2_per_zone_and_difficulty(tmp_path):
    """The split is the contract's measurement, and the file names are where it shows.

    Stufe 3 per zone because `REPORT_KILLS_QUERY` takes only ``$code`` -- one request
    answers every boss AND every difficulty -- and Stufe 2 per (zone, difficulty)
    because `FIGHT_STRUCTURE_QUERY`'s difficulty argument is the only filter there is.
    """
    report = run(StubClient(), tmp_path)
    assert report.exit_code == catalogue.EXIT_OK, report.to_json()
    assert (tmp_path / "z53.reports.jsonl").is_file()
    assert (tmp_path / "z53-d5.kills.jsonl").is_file()
    assert not (tmp_path / "z53-d5.reports.jsonl").exists()
    assert not (tmp_path / "z53.kills.jsonl").exists()

    line = json.loads((tmp_path / "z53.reports.jsonl").read_text().strip())
    assert line["code"] == "aBcD" and line["kills"][0]["e"] == ENCOUNTER
    kill = json.loads((tmp_path / "z53-d5.kills.jsonl").read_text().strip())
    assert kill["encounterId"] == ENCOUNTER and kill["difficulty"] == MYTHIC


def test_stufe_3_runs_before_stufe_2(tmp_path):
    """Order is the contract's and is not an optimisation.

    A report can be set private or deleted, and when it goes the directory entry goes
    with it. A kill's shape loses nothing by waiting, because a kill whose report
    survives can be opened tomorrow.
    """
    client = StubClient()
    run(client, tmp_path)
    kinds = [call[0] for call in client.calls]
    assert kinds.index("report_kills") < kinds.index("structure")


def test_a_second_run_over_a_settled_zone_opens_no_report_and_no_fight(tmp_path):
    """A stored key is skipped BEFORE a query is sent -- the whole point of a directory."""
    run(StubClient(), tmp_path)
    second = StubClient()
    report = run(second, tmp_path)
    assert [c for c in second.calls if c[0] in {"report_kills", "structure"}] == []
    assert report.new_reports == 0 and report.new_kills == 0


def test_a_run_that_finds_nothing_leaves_the_rows_byte_identical(tmp_path):
    """Rows are touched only when there is something to append.

    So "the commit is the change" is true at the byte level, and a quiet run leaves
    git with nothing to say.
    """
    run(StubClient(), tmp_path)
    before = {
        path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix == ".jsonl"
    }
    run(StubClient(), tmp_path)
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix == ".jsonl"}
    assert before == after and before, "a settled run rewrote a row file"


def test_a_settled_run_does_not_even_TOUCH_the_row_files(tmp_path, monkeypatch):
    """The byte comparison above cannot see this, which is a finding about that test.

    Rewriting a file with the content it already holds produces identical bytes, so
    `test_a_run_that_finds_nothing_leaves_the_rows_byte_identical` passes whether or
    not the `if new_rows:` guard exists -- its canary stayed green. It pins the
    CLAIM the contract makes; this pins the MECHANISM, by counting the writes.

    The guard is load-bearing for a case byte-identity cannot reach: it is what makes
    `read_lines` + `"".join` round-tripping exactly a requirement only of runs that
    append. A file the round trip ever mangled would otherwise be mangled by a run
    that found nothing to say.
    """
    run(StubClient(), tmp_path)

    written: list[str] = []
    real = catalogue.atomic_write
    monkeypatch.setattr(
        catalogue,
        "atomic_write",
        lambda path, text: (written.append(Path(path).name), real(path, text))[1],
    )
    run(StubClient(), tmp_path)

    assert not [name for name in written if name.endswith(".jsonl")], written
    # State and manifest ARE rewritten every run: they are derived, not append-only,
    # and both reproduce their bytes, which the test above is what checks.
    assert "manifest.json" in written


def test_the_manifest_carries_no_run_stamp(tmp_path):
    """A run that changes nothing must leave the manifest byte-identical.

    Anything read off the clock defeats a settle -- `_PROVENANCE_PATHS` is the same
    lesson in the published datasets, where `measurement.cost` restamped `fights.json`
    for weeks with the guard beside it looking present.
    """
    run(StubClient(), tmp_path)
    first = (tmp_path / "manifest.json").read_bytes()
    run(StubClient(), tmp_path)
    assert (tmp_path / "manifest.json").read_bytes() == first

    manifest = json.loads(first)
    assert "generatedAt" not in manifest and "run" not in manifest
    stages = {entry["path"]: entry["stage"] for entry in manifest["files"]}
    assert stages == {"z53.reports.jsonl": 3, "z53-d5.kills.jsonl": 2}


def test_an_alarmed_pair_writes_nothing_at_all(tmp_path):
    """No lines, no state -- and the run goes on and reports exit 3.

    A scrub that fails open is the one failure this folder's privacy cannot absorb,
    so the answer is to keep the bytes off disk rather than to log it and continue.
    Stufe 3 is untouched, because the alarm is a fact about the Stufe 2 payload.
    """
    broken = structure()
    broken["masterData"]["actors"] = [
        dict(a, kind=a.pop("type")) for a in broken["masterData"]["actors"]
    ]
    client = StubClient(structures={("aBcD", ENCOUNTER, MYTHIC): broken})
    report = run(client, tmp_path)

    assert report.exit_code == catalogue.EXIT_SCHEMA_ALARM
    assert report.alarmed_sets == ["z53-d5"]
    assert not (tmp_path / "z53-d5.kills.jsonl").exists(), "the alarmed pair wrote lines"
    assert not (tmp_path / "z53-d5.state.json").exists(), "the alarmed pair wrote state"
    assert (tmp_path / "z53.reports.jsonl").is_file(), "Stufe 3 was not the thing that alarmed"


def test_a_ptr_zone_is_refused_whole_and_before_anything_is_written(tmp_path):
    """The one shape a downstream import cannot catch: its zone guard AGREES with it.

    `worldData.zone(id:)` reaches a zone `worldData.zones` never lists, so ``--zones
    54`` is a plausible hand dispatch. Refusing the zone is cheaper and louder than
    refusing every row it would have produced.
    """
    client = StubClient(
        zones={54: zone_payload(54, encounters=((53421, "The Twin Fangs"),))},
        reports={54: [{"code": "aBcD"}]},
    )
    report = catalogue.run_catalogue(client, tmp_path, options(zones=(54,)), run_id="TEST")
    assert report.exit_code == catalogue.EXIT_ZONE_SKIPPED
    assert report.zones_skipped == [54]
    assert list(tmp_path.glob("z54*")) == []
    assert ("report_kills", "aBcD") not in client.calls


def test_a_transport_failure_refuses_that_report_and_the_run_goes_on(tmp_path):
    """A refusal names the thing and the reason, never a silent skip."""
    client = StubClient(
        reports={ZONE: [{"code": "bad"}, {"code": "aBcD"}]},
        kills={"bad": WarcraftLogsError("boom"), "aBcD": (REPORT_START, [kill_fight()])},
    )
    report = run(client, tmp_path)
    assert report.new_reports == 1
    refused = [
        json.loads(line) for line in (tmp_path / "z53.refused.jsonl").read_text().splitlines()
    ]
    assert [(r["code"], r["outcome"]) for r in refused] == [("bad", "report-error")]


def test_a_first_reading_that_fails_is_a_run_that_could_not_start(tmp_path):
    """Exit 1, not a green run that catalogued nothing -- the #219 shape.

    Nothing has been paid for at that point, so a rotated secret or a dead route is a
    failure to start rather than a budget stop.
    """
    client = StubClient(readings=[WarcraftLogsError("401")])
    report = run(client, tmp_path)
    assert report.exit_code == catalogue.EXIT_FAILED
    assert report.failed and "401" in report.failed
    assert list(tmp_path.glob("z*")) == []


def test_the_preflight_reading_never_sleeps(tmp_path):
    """#168, inherited: a sleep is a bet that there is work, taken before any skip
    decision exists. The pre-flight reading reads and walks on."""
    slept: list[float] = []
    client = StubClient(
        readings=[{"limitPerHour": 100.0, "pointsSpentThisHour": 99.0, "pointsResetIn": 10}]
    )
    catalogue.run_catalogue(
        client, tmp_path, options(point_ceiling=0.3), run_id="TEST", sleep=slept.append
    )
    assert slept == [], "the pre-flight reading slept before any skip decision existed"


# ── the cursor ──────────────────────────────────────────────────────────────────


def test_the_cursor_is_a_prefilter_and_the_key_set_is_the_exact_test(tmp_path):
    """Two kills on the same millisecond: neither is lost and neither is read twice.

    A cursor alone cannot be exact on a tie -- it either skips a kill it never judged
    or re-reads one it did. So the cursor skips STRICTLY earlier and the stored key
    set catches the rest, which is exact by construction.
    """
    twins = [kill_fight(fight_id=1), kill_fight(fight_id=2)]
    client = StubClient(
        kills={"aBcD": (REPORT_START, twins)},
        structures={
            ("aBcD", ENCOUNTER, MYTHIC): structure(
                fights=[
                    dict(
                        f,
                        name="The Twin Fangs",
                        size=20,
                        friendlyPlayers=[1],
                        enemyNPCs=[],
                        phaseTransitions=[],
                    )
                    for f in twins
                ]
            )
        },
    )
    run(client, tmp_path, kills=1)
    first = [
        json.loads(line) for line in (tmp_path / "z53-d5.kills.jsonl").read_text().splitlines()
    ]
    assert [row["fightId"] for row in first] == [1]

    run(client, tmp_path, kills=1)
    both = [json.loads(line) for line in (tmp_path / "z53-d5.kills.jsonl").read_text().splitlines()]
    assert [row["fightId"] for row in both] == [1, 2], "the tie lost a kill or read one twice"


# ── the gate ────────────────────────────────────────────────────────────────────


def test_validate_passes_a_directory_the_run_just_wrote(tmp_path):
    run(StubClient(), tmp_path)
    assert catalogue.validate(tmp_path) == []


def test_validate_catches_a_player_name_on_a_committed_line(tmp_path):
    """The THIRD place the scrub is checked, and the reason the gate is not reused.

    The scrub runs at extraction and the alarm fires at the writer; this one reads the
    FILE, which is what actually leaves the machine. A line written by an older build
    of this module would pass both of the others.
    """
    run(StubClient(), tmp_path)
    path = tmp_path / "z53-d5.kills.jsonl"
    line = json.loads(path.read_text().strip())
    line["actors"] = [dict(a, name="Somebody") for a in line["actors"]]
    path.write_text(catalogue.dumps_line(line), encoding="utf-8")

    problems = catalogue.validate(tmp_path)
    assert any("carry a name" in p for p in problems), problems


def test_validate_refuses_a_stray_temp_file_and_a_count_that_disagrees(tmp_path):
    run(StubClient(), tmp_path)
    (tmp_path / "z53.reports.jsonl.tmp").write_text("{}\n", encoding="utf-8")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["lines"] = 99
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    problems = catalogue.validate(tmp_path)
    assert any("temp file" in p for p in problems), problems
    assert any("manifest says 99" in p for p in problems), problems


def test_describe_reads_the_files_and_sends_no_query(tmp_path):
    """The check to run after a seed and before a cron: no client at all."""
    run(StubClient(), tmp_path)
    lines = catalogue.describe(tmp_path, [ZONE], [MYTHIC])
    assert any("stage 3: 1 report(s)" in line for line in lines), lines
    assert any("stage 2: 1 kill(s)" in line for line in lines), lines


def test_the_catalogue_sends_no_graphql_document_of_its_own():
    """Schritt 3's rule applied to a new caller rather than re-derived.

    Every question goes through a `WarcraftLogsClient` method that already exists, so
    the catalogue adds no second copy of a document -- which would be a second cache
    entry for one thing, and a fifth entry in the reading-less ratchet.
    """
    documents = {
        name: value
        for name, value in vars(catalogue).items()
        if name.endswith("QUERY")
        and isinstance(value, str)
        and ("query" in value or "mutation" in value)
    }
    assert documents == {}, documents
