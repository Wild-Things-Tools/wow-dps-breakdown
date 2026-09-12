"""The catalogue: which reports hold which kills, and what a kill looks like.

`docs/progress-catalogue.md` is the contract and wins wherever this module and that
file disagree. Two stages, and the split between them is a measurement rather than a
preference:

* **Stufe 3** (`z<zone>.reports.jsonl`) is the directory. `REPORT_KILLS_QUERY` takes
  exactly one variable, ``$code``, so one request answers every boss AND every
  difficulty in a report -- which is what makes the stage affordable and why it is
  written per **zone**. Splitting it per difficulty would throw that saving away.
* **Stufe 2** (`z<zone>-d<diff>.kills.jsonl`) is the shape of one kill.
  `FIGHT_STRUCTURE_QUERY` takes code + encounter + difficulty, and that call is the
  only difficulty filter there is, so this stage is written per (zone, difficulty).

Neither stage stores a ranking row, and Stufe 1 -- the events -- is not here at all.

**This module sends no GraphQL document of its own.** Every question goes through a
`WarcraftLogsClient` method that already exists, which is Schritt 3's rule ("one
sender per question") applied to a new caller rather than re-derived: a second copy of
a document is a second cache entry for one thing, and this repository has measured
that twice.

The ceiling, the sleep-until-reset, the deadline, the atomic writes and the commit
gate are `progresssweep`'s, **imported rather than copied**. The contract says
"ported"; importing is the stronger reading, because two implementations of one rule
are exactly what drifts, and this repository has the `PTR_TWIN_ID_FLOOR` measurement
to show for it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import fightextract, firstkills
from .progresssweep import (
    PTR_TWIN_ID_FLOOR,
    Budget,
    DeadlineReached,
    _git_head_line_count,
    atomic_write,
    dumps_line,
    read_lines,
)
from .warcraftlogs import RateLimited, WarcraftLogsError

log = logging.getLogger(__name__)

#: The catalogue's own schema version. Deliberately NOT shared with the cohort
#: folder's: the two live in one repository and share no file, no manifest and no
#: schema, so a change to one must not read as a change to the other.
SCHEMA_VERSION = 1

DEFAULT_REPORT_PAGES = 5
DEFAULT_REPORT_LIMIT = 100
DEFAULT_POINT_CEILING = 0.3
DEFAULT_DEADLINE_MINUTES = 300.0
DEFAULT_DIFFICULTIES: tuple[int, ...] = (5, 4)

#: Stufe 3 refusals do not retry; a `report-error` does, on the next run, because a
#: transport failure is not a property of the report.
RETRYABLE_OUTCOMES = frozenset({"report-error", "structure-error"})


# ── the scrub ───────────────────────────────────────────────────────────────────


#: Kept on every actor. `name` is added back only for an actor the raid does not own
#: -- see `scrub_actors`, and the contract's table for why each of these is
#: load-bearing rather than leftover.
_ACTOR_FIELDS = ("id", "gameID", "type", "subType", "petOwner")


def scrub_actors(actors: object) -> list[dict]:
    """Every actor, with a **player-owned** actor's name removed.

    "Player-owned" is `fightextract.friendly_source_ids`, not a type test, and that
    is the whole of the decision: `masterData.actors` types a hunter's pet, a mage's
    Mirror Image and a boss's summoned add all as ``Pet``, so a type test separates
    nothing and would leave a player's pet name on disk. That function follows
    ``petOwner`` transitively and cycle-safely and is already the predicate the aura
    filter uses; a second spelling of it here is the thing that drifts.

    An NPC's name is **kept**, and is not an oversight: it is not personal, and it is
    the only readable handle on "Broodling of Ithraz". `_probe_fight` builds
    ``{actor id: name}`` over every actor and falls back to ``str(id)`` where a name
    is missing, so dropping a player's costs that reader nothing and dropping an
    NPC's would cost it everything it is for.
    """
    rows = [actor for actor in (actors or []) if isinstance(actor, dict)]
    owned = fightextract.friendly_source_ids(rows)
    scrubbed: list[dict] = []
    for actor in rows:
        kept = {key: actor.get(key) for key in _ACTOR_FIELDS if key in actor}
        if actor.get("id") not in owned:
            kept["name"] = actor.get("name")
        scrubbed.append(kept)
    return scrubbed


def leaked_player_names(actors: Sequence[dict], friendly_players: object) -> list[int]:
    """Ids the FIGHT calls players and that still carry a name, on a scrubbed list.

    **The cross-check deliberately does not use the predicate the scrub uses.** A
    guard built on ``type``/``petOwner`` -- the fields `scrub_actors` reads -- cannot
    catch those fields changing shape: the drop would stop recognising a player, the
    guard would stop recognising one too, and the names would go to disk under a
    green run. That is this repository's signature defect ("a guard that is present
    and answers over the wrong population"), and the only way out is a signal from
    somewhere else in the payload.

    ``fight.friendlyPlayers`` is that signal: the fight's own list of player actor
    ids, from a different part of the response than ``masterData``. If ``type`` is
    renamed, every actor comes back un-owned, every name survives the scrub, and this
    still names twenty ids.

    It is **one-sided** and says so: ``friendlyPlayers`` holds the players and not
    their pets, so a pet name surviving a broken scrub is not caught here. A
    one-sided check that fires on the twenty players is worth having; a two-sided one
    that shares the broken predicate is not.
    """
    if not isinstance(friendly_players, list):
        return []
    players = {pid for pid in friendly_players if isinstance(pid, int)}
    return sorted(
        actor["id"]
        for actor in actors
        if isinstance(actor.get("id"), int) and actor["id"] in players and actor.get("name")
    )


class CatalogueAlarm(Exception):
    """A schema alarm: this file set writes nothing, the run goes on, exit 3.

    Raised for the two states the contract names -- a player name reaching the writer,
    and a `masterData` block that is absent entirely -- and for nothing else. A scrub
    that fails open is the one failure this folder's privacy cannot absorb, and a
    fight with no actor list makes the player-aura filter inoperative downstream,
    which is worse than a missing line because it is invisible.
    """


# ── Stufe 3: what a report holds ────────────────────────────────────────────────


def kills_in_report(
    code: str, report_start_ms: float, fights: object
) -> tuple[list[dict], list[dict]]:
    """``(kills, refusals)`` out of one report's kill-filtered fight list.

    ``kill`` is re-checked even though ``killType: Kills`` is passed, for the reason
    `firstkills.kills_from_report` already carries: a filter that silently stopped
    filtering would put a wipe into a catalogue of kills.

    Times are made **absolute** here. ``ReportFight.startTime`` counts from the
    report's own start, and a line carrying the raw offset is a number near zero that
    sorts before every real date -- the unit error this project paid for once, where
    a search reported 100% of kills as earlier than the ranked sample.
    """
    kills: list[dict] = []
    refusals: list[dict] = []
    for fight in fights if isinstance(fights, list) else []:
        if not isinstance(fight, dict):
            continue
        fight_id = fight.get("id")
        encounter = fight.get("encounterID")
        if not isinstance(encounter, int):
            continue
        if encounter >= PTR_TWIN_ID_FLOOR:
            # The one shape a downstream import cannot catch: a PTR row joins to a
            # live catalogue and lands under the wrong zone, and the zone-mismatch
            # guard AGREES with it.
            refusals.append(
                {"code": code, "fightId": fight_id, "encounterId": encounter, "outcome": "ptr-id"}
            )
            continue
        if not fight.get("kill"):
            refusals.append(
                {
                    "code": code,
                    "fightId": fight_id,
                    "encounterId": encounter,
                    "outcome": "not-a-kill",
                }
            )
            continue
        start = fight.get("startTime")
        if not isinstance(start, (int, float)) or not report_start_ms:
            refusals.append(
                {
                    "code": code,
                    "fightId": fight_id,
                    "encounterId": encounter,
                    "outcome": "unknown-time-base",
                }
            )
            continue
        absolute = float(report_start_ms) + float(start)
        if absolute < firstkills._IMPLAUSIBLE_BEFORE_MS:
            refusals.append(
                {
                    "code": code,
                    "fightId": fight_id,
                    "encounterId": encounter,
                    "outcome": "unknown-time-base",
                }
            )
            continue
        end = fight.get("endTime")
        kills.append(
            {
                "e": encounter,
                "d": fight.get("difficulty"),
                "f": int(fight_id) if isinstance(fight_id, int) else None,
                "sMs": int(absolute),
                "eMs": int(float(report_start_ms) + float(end))
                if isinstance(end, (int, float))
                else None,
            }
        )
    return kills, refusals


def report_line(
    zone_id: int,
    code: str,
    report_start_ms: float,
    kills: list[dict],
    listed: int,
    *,
    at: str,
    run_id: str,
) -> dict:
    """One Stufe 3 line: every kill one report holds, with its time base.

    ``killsListed`` is **the number of rows the kill-filtered query returned**, before
    this module's own re-check and refusals. It is what separates "this report holds
    no kill of anything" from "this report was not read" -- a line exists at all only
    because the report was read.

    An earlier draft of the contract called it ``fightsSeen`` and described it as
    "every fight the report listed, kills and wipes". That is **not obtainable from
    this query**: ``fights(killType: Kills)`` filters server-side, so wipes never
    reach us, and a second unfiltered query is a cost nothing here has asked for. The
    field is renamed rather than re-described, because a name that promises more than
    its computation delivers is the failure mode this repository keeps recording.

    There is no report END time, and that is the document rather than an omission:
    ``REPORT_KILLS_QUERY`` selects ``report { code startTime fights{...} }``. The last
    kill's ``eMs`` is a lower bound on it.
    """
    return {
        "v": SCHEMA_VERSION,
        "zoneId": zone_id,
        "code": code,
        "startedAtMs": int(report_start_ms),
        "kills": kills,
        "killsListed": listed,
        "readAt": at,
        "run": run_id,
    }


# ── Stufe 2: the shape of one kill ──────────────────────────────────────────────


def phase_names(report: dict, encounter_id: int) -> tuple[list[dict], bool | None]:
    """``(this encounter's phase names, separatesWipes)`` out of ``report.phases``.

    ``report.phases`` is per **encounter** -- ``{encounterID, separatesWipes,
    phases:[...]}`` -- so the entry is picked by id and only its nested list is
    stored. Carrying the outer array would file another boss's phase names under this
    one's key, and a catalogue line is about one encounter.
    """
    for entry in report.get("phases") or []:
        if isinstance(entry, dict) and entry.get("encounterID") == encounter_id:
            names = [p for p in (entry.get("phases") or []) if isinstance(p, dict)]
            return names, entry.get("separatesWipes")
    return [], None


def kill_line(
    zone_id: int,
    encounter_id: int,
    difficulty: int,
    code: str,
    report: dict,
    fight: dict,
    *,
    at: str,
    run_id: str,
) -> dict:
    """One Stufe 2 line. Every field is what `FIGHT_STRUCTURE_QUERY` returned.

    Nothing here is derived and nothing is interpreted: the catalogue is the material
    an answer is selected from, not the answer. ``report.title`` is dropped (free text
    a person wrote, and zero readers across this package, measured 2026-09-12) and the
    actor names are scrubbed per `scrub_actors`.

    Raises `CatalogueAlarm` when ``masterData`` is absent or a player name survives
    the scrub -- the two states where writing the line is worse than not writing it.
    """
    master = report.get("masterData")
    if not isinstance(master, dict) or not isinstance(master.get("actors"), list):
        raise CatalogueAlarm(
            f"{code} fight {fight.get('id')}: the response carries no masterData.actors, "
            "so a line from it would make the player-aura filter inoperative downstream"
        )
    actors = scrub_actors(master.get("actors"))
    leaked = leaked_player_names(actors, fight.get("friendlyPlayers"))
    if leaked:
        raise CatalogueAlarm(
            f"{code} fight {fight.get('id')}: {len(leaked)} actor(s) the fight calls players "
            f"still carry a name after the scrub (ids {leaked[:5]}) -- the scrub is inoperative"
        )

    report_start = report.get("startTime")
    base = float(report_start) if isinstance(report_start, (int, float)) else 0.0
    start = fight.get("startTime")
    end = fight.get("endTime")
    names, separates = phase_names(report, encounter_id)
    return {
        "v": SCHEMA_VERSION,
        "zoneId": zone_id,
        "encounterId": encounter_id,
        "difficulty": difficulty,
        "code": code,
        "fightId": fight.get("id"),
        "name": fight.get("name"),
        "startedAtMs": int(base + float(start)) if isinstance(start, (int, float)) else None,
        "lengthMs": int(float(end) - float(start))
        if isinstance(start, (int, float)) and isinstance(end, (int, float))
        else None,
        "size": fight.get("size"),
        "friendlyPlayers": fight.get("friendlyPlayers") or [],
        "enemyNPCs": fight.get("enemyNPCs") or [],
        "phaseTransitions": fight.get("phaseTransitions") or [],
        "phases": names,
        "separatesWipes": separates,
        "actors": actors,
        "abilities": [a for a in (master.get("abilities") or []) if isinstance(a, dict)],
        "readAt": at,
        "run": run_id,
    }


def pick_fight(report: dict, fight_id: int, difficulty: int) -> tuple[dict | None, str | None]:
    """``(the fight, refusal reason)`` -- exactly one of the two is set.

    A fight stating a **different** difficulty is refused: the fetch is already scoped
    by difficulty, so a mismatch means the scoping did not hold. A fight stating
    **none** is allowed through -- unknown is not wrong, which is `harvest`'s
    three-way rule and the one `firstkills.kills_from_report` already applies.
    """
    for fight in report.get("fights") or []:
        if not isinstance(fight, dict) or fight.get("id") != fight_id:
            continue
        if not fight.get("kill"):
            return None, "not-a-kill"
        level = fight.get("difficulty")
        if level is not None and level != difficulty:
            return None, "difficulty-mismatch"
        return fight, None
    return None, "structure-error"


# ── files ───────────────────────────────────────────────────────────────────────


def zone_stem(zone_id: int) -> str:
    return f"z{zone_id}"


def pair_stem(zone_id: int, difficulty: int) -> str:
    return f"z{zone_id}-d{difficulty}"


def _index(lines: list[str], key_fields: tuple[str, ...]) -> tuple[dict[tuple, dict], int, int]:
    """``(rows by key, duplicates, unreadable)``. Last line wins, duplicates counted.

    A duplicate is a fact about a past run, not something to repair on the way past --
    the file is append-only at the BYTE level, so the lines themselves are never
    rewritten.
    """
    keyed: dict[tuple, dict] = {}
    duplicates = 0
    unreadable = 0
    for line in lines:
        try:
            document = json.loads(line)
            key = tuple(document[field_name] for field_name in key_fields)
        except (ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        if key in keyed:
            duplicates += 1
        keyed[key] = document
    return keyed, duplicates, unreadable


@dataclass
class FileSet:
    """One append-only file set: lines as read, rows by key, and its state document."""

    stem: str
    stage: int
    rows_path: Path
    refused_path: Path
    state_path: Path
    key_fields: tuple[str, ...]
    row_lines: list[str] = field(default_factory=list)
    refused_lines: list[str] = field(default_factory=list)
    rows: dict[tuple, dict] = field(default_factory=dict)
    refused: dict[tuple, dict] = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    duplicate_rows: int = 0
    unreadable_lines: int = 0

    def done(self) -> set[tuple]:
        """Keys with a stored verdict: rows ∪ refused.

        The **exact** test for "already judged", against which the cursor is only a
        cheap pre-filter. A cursor alone cannot be exact on a tie; a key set is.
        """
        return set(self.rows) | set(self.refused)

    def retryable(self) -> set[tuple]:
        """Keys whose LAST verdict is a transport failure and that have no row.

        A `report-error` is a fact about the network rather than about the report, so
        it is offered again; every other refusal is a fact about the thing and is not.
        """
        return {
            key
            for key, line in self.refused.items()
            if line.get("outcome") in RETRYABLE_OUTCOMES and key not in self.rows
        }


def load_zone(out_dir: Path, zone_id: int) -> FileSet:
    """The Stufe 3 file set for one zone, keyed on the report code."""
    stem = zone_stem(zone_id)
    return _load(
        out_dir,
        stem,
        stage=3,
        key_fields=("code",),
        empty_state={
            "v": SCHEMA_VERSION,
            "zoneId": zone_id,
            "zoneName": None,
            "frozen": None,
            "stage": 3,
            "windows": [],
            "codesKnown": 0,
            "refusedCodes": 0,
        },
    )


def load_pair(out_dir: Path, zone_id: int, difficulty: int) -> FileSet:
    """The Stufe 2 file set for one (zone, difficulty), keyed on ``(code, fightId)``.

    A fight id is unique within a report, so the pair is the natural key and needs no
    encounter in it -- and keying on the encounter as well would let one kill be
    written twice under two encounter ids if a report ever disagreed with itself.
    """
    stem = pair_stem(zone_id, difficulty)
    return _load(
        out_dir,
        stem,
        stage=2,
        key_fields=("code", "fightId"),
        empty_state={
            "v": SCHEMA_VERSION,
            "zoneId": zone_id,
            "difficulty": difficulty,
            "stage": 2,
            "encounters": {},
        },
    )


def _load(
    out_dir: Path, stem: str, *, stage: int, key_fields: tuple[str, ...], empty_state: dict
) -> FileSet:
    rows_name = "reports" if stage == 3 else "kills"
    data = FileSet(
        stem,
        stage,
        out_dir / f"{stem}.{rows_name}.jsonl",
        out_dir / f"{stem}.refused.jsonl",
        out_dir / f"{stem}.state.json",
        key_fields,
    )
    data.row_lines = read_lines(data.rows_path)
    data.refused_lines = read_lines(data.refused_path)
    data.rows, data.duplicate_rows, bad_rows = _index(data.row_lines, key_fields)
    data.refused, _, bad_refused = _index(data.refused_lines, key_fields)
    data.unreadable_lines = bad_rows + bad_refused
    if data.state_path.is_file():
        data.state = json.loads(data.state_path.read_text(encoding="utf-8"))
    else:
        data.state = dict(empty_state)
    return data


def _last_swept_at(state: dict) -> str | None:
    """The newest ``sweptAt`` anywhere in a state document, whichever stage it is."""
    stamps = [
        entry.get("sweptAt")
        for entry in list((state.get("encounters") or {}).values())
        + list(state.get("windows") or [])
        if isinstance(entry, dict) and entry.get("sweptAt")
    ]
    return max(stamps) if stamps else None


def build_manifest(out_dir: Path) -> dict:
    """DERIVED from the directory, never accumulated, and carrying **no run stamp**.

    A run that changes nothing must leave the manifest byte-identical, which is what
    makes "the commit is the change" true. `_PROVENANCE_PATHS` is the same lesson in
    the published datasets: anything read off the clock defeats a settle.
    """
    files = []
    for state_path in sorted(out_dir.glob("z*.state.json")):
        stem = state_path.name[: -len(".state.json")]
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
        stage = state.get("stage")
        rows_name = "reports" if stage == 3 else "kills"
        entry = {
            "path": f"{stem}.{rows_name}.jsonl",
            "zoneId": state.get("zoneId"),
            "stage": stage,
            "lines": len(read_lines(out_dir / f"{stem}.{rows_name}.jsonl")),
            "refused": len(read_lines(out_dir / f"{stem}.refused.jsonl")),
            "lastSweptAt": _last_swept_at(state),
        }
        if stage == 2:
            entry["difficulty"] = state.get("difficulty")
        files.append(entry)
    return {"v": SCHEMA_VERSION, "files": files}


README_TEXT = """# progress-catalogue

Which Warcraft Logs reports hold which kills (Stufe 3), and what each kill looks like
(Stufe 2). No events, no rankings, no names.

Written by `wowdps catalogue` in `Wild-Things-Tools/wow-dps-breakdown` (GitHub
Actions). The contract is `docs/progress-catalogue.md` in that repository.

- `z<zone>.reports.jsonl`: one READ REPORT per line -- its kills, with absolute
  times. Append-only.
- `z<zone>-d<difficulty>.kills.jsonl`: one KILL per line -- its fight metadata, phase
  names, enemy NPCs, and the report's actor and ability tables. Append-only.
- `*.refused.jsonl`: one judged-but-unwritten thing per line, with its reason.
- `*.state.json`: the cursor, rewritten atomically.
- `manifest.json`: per file set, line counts and the newest `sweptAt`. No run
  timestamp, so a run that changes nothing changes nothing.

This folder shares its repository with `progress-cohort/` and shares nothing else:
no file, no manifest, no schema version.

A player-owned actor's NAME never reaches these files. It is dropped at extraction,
and a name that survives is a schema alarm that makes the file set write nothing.
"""


def write_set(data: FileSet, out_dir: Path, new_rows: list[str], new_refused: list[str]) -> None:
    """Append, rewrite state and manifest -- each atomically, through a temp file.

    Rows and refusals are touched only when there is something to append, so a run
    that finds nothing leaves them **byte-identical**. The whole file is rewritten
    because that is what makes the append atomic; the bytes before the appended lines
    are the bytes that were read.
    """
    if new_rows:
        data.row_lines.extend(new_rows)
        atomic_write(data.rows_path, "".join(data.row_lines))
    if new_refused:
        data.refused_lines.extend(new_refused)
        atomic_write(data.refused_path, "".join(data.refused_lines))
    atomic_write(data.state_path, json.dumps(data.state, indent=1, ensure_ascii=False) + "\n")
    readme = out_dir / "README.md"
    if not readme.is_file():
        atomic_write(readme, README_TEXT)
    atomic_write(
        out_dir / "manifest.json",
        json.dumps(build_manifest(out_dir), indent=1, ensure_ascii=False) + "\n",
    )


# ── the runner ──────────────────────────────────────────────────────────────────


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ZONE_SKIPPED = 2
EXIT_SCHEMA_ALARM = 3


class _StopRun(Exception):
    """Budget or deadline: write what was read, stop the run, exit 0."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CatalogueOptions:
    zones: tuple[int, ...]
    difficulties: tuple[int, ...] = DEFAULT_DIFFICULTIES
    stages: tuple[int, ...] = (3, 2)
    report_pages: int = DEFAULT_REPORT_PAGES
    report_limit: int = DEFAULT_REPORT_LIMIT
    kills: int = 0
    point_ceiling: float = DEFAULT_POINT_CEILING
    deadline_minutes: float = DEFAULT_DEADLINE_MINUTES
    retry_errors: bool = False


@dataclass
class CatalogueReport:
    exit_code: int = EXIT_OK
    new_reports: int = 0
    new_kills: int = 0
    new_refused: int = 0
    attempted: int = 0
    zones_skipped: list[int] = field(default_factory=list)
    alarmed_sets: list[str] = field(default_factory=list)
    stopped: str | None = None
    failed: str | None = None
    sleeps: int = 0
    slept_seconds: float = 0.0
    points_first: float | None = None
    points_last: float | None = None
    queries: int = 0

    def to_json(self) -> dict:
        spent = (
            round(self.points_last - self.points_first, 1)
            if self.points_first is not None
            and self.points_last is not None
            and self.points_last >= self.points_first
            else None
        )
        return {
            "exitCode": self.exit_code,
            "newReports": self.new_reports,
            "newKills": self.new_kills,
            "newRefused": self.new_refused,
            "attempted": self.attempted,
            "zonesSkipped": self.zones_skipped,
            "alarmedSets": self.alarmed_sets,
            "stopped": self.stopped,
            "failed": self.failed,
            "sleeps": self.sleeps,
            "sleptSeconds": self.slept_seconds,
            # None is UNMEASURED, never zero: a counter that did not move is the
            # absence of a measurement, not a free run.
            "points": spent,
            "queries": self.queries,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _refusal(stem_fields: dict, outcome: str, *, at: str, run_id: str) -> str:
    return dumps_line(
        {"v": SCHEMA_VERSION, **stem_fields, "outcome": outcome, "readAt": at, "run": run_id}
    )


def _zone_block(client, zone_id: int) -> tuple[dict | None, str]:
    """``(the zone, why not)``. A PTR-id zone is refused before anything is written.

    `worldData.zone(id:)` reaches a zone `worldData.zones` never lists, which is what
    makes ``--zones 54`` -- The Venomous Abyss's live-but-unlisted PTR twin -- a
    plausible hand dispatch. Its encounters carry ``5xxxx`` ids, and those are the one
    shape a downstream import cannot catch, because its zone-mismatch guard agrees
    with them. Refusing the whole zone here is cheaper and louder than refusing every
    row it would have produced.
    """
    block = client.zone(zone_id)
    if not isinstance(block, dict) or not block.get("id"):
        return None, f"zone {zone_id}: Warcraft Logs lists no such zone"
    encounters = [
        e
        for e in (block.get("encounters") or [])
        if isinstance(e, dict) and isinstance(e.get("id"), int)
    ]
    ptr = sorted(e["id"] for e in encounters if e["id"] >= PTR_TWIN_ID_FLOOR)
    if ptr:
        return None, (
            f"zone {zone_id} ({block.get('name')}): encounter id(s) {ptr[:4]} are at or above "
            f"{PTR_TWIN_ID_FLOOR}, so this is a PTR zone -- its rows would join a live catalogue"
        )
    if not encounters:
        return None, f"zone {zone_id} ({block.get('name')}): no encounters listed"
    return block, ""


def sweep_zone_reports(
    client,
    out_dir: Path,
    zone: dict,
    options: CatalogueOptions,
    budget: Budget,
    report: CatalogueReport,
    *,
    run_id: str,
) -> None:
    """Stufe 3 for one zone: walk the report list, read each report's kills once.

    Built per zone **once** and then only appended to, never per boss again -- the
    Konzept's decision, and it follows from `REPORT_KILLS_QUERY` taking only
    ``$code``. A report already in the file set is skipped before a query is sent,
    which is what makes a re-run of a settled zone cost its report pages and nothing
    per report.
    """
    zone_id = int(zone["id"])
    data = load_zone(out_dir, zone_id)
    data.state["zoneName"] = zone.get("name")
    data.state["frozen"] = zone.get("frozen")
    seen = data.done() - (data.retryable() if options.retry_errors else set())
    new_rows: list[str] = []
    new_refused: list[str] = []
    codes: list[str] = []
    pages_read = 0
    walled = False

    start_ms, end_ms = firstkills.search_window(0.0, 0.0, 0.0)
    try:
        for page in range(1, options.report_pages + 1):
            budget.check()
            payload = client.reports_in_window(
                zone_id, start_ms, end_ms, page=page, limit=options.report_limit
            )
            rows = firstkills.reports_from_payload(payload)
            pages_read = page
            codes.extend(str(row["code"]) for row in rows)
            if len(rows) < options.report_limit:
                break
            if page == options.report_pages:
                # The page limit, not exhaustion. `fight-probe` already records that
                # difference, because a zone nobody has read would otherwise look
                # like a zone with nothing left to read.
                walled = True

        at = _now()
        for code in codes:
            if (code,) in seen:
                continue
            budget.check()
            report.attempted += 1
            try:
                report_start, fights = client.report_kills(code)
            except WarcraftLogsError as exc:
                log.warning("zone %s report %s: %s", zone_id, code, exc)
                new_refused.append(_refusal({"code": code}, "report-error", at=at, run_id=run_id))
                continue
            kills, refusals = kills_in_report(code, report_start, fights)
            listed = len(fights) if isinstance(fights, list) else 0
            if not report_start:
                new_refused.append(
                    _refusal({"code": code}, "unknown-time-base", at=at, run_id=run_id)
                )
                continue
            new_rows.append(
                dumps_line(
                    report_line(zone_id, code, report_start, kills, listed, at=at, run_id=run_id)
                )
            )
            for refusal in refusals:
                new_refused.append(_refusal(refusal, refusal.pop("outcome"), at=at, run_id=run_id))
    finally:
        data.state["windows"] = [
            *(data.state.get("windows") or []),
            {
                "fromMs": start_ms,
                "toMs": end_ms,
                "pagesRead": pages_read,
                "reportsSeen": len(codes),
                "walled": walled,
                # The ENCOUNTER's own time, not the run's: on a five-hour run the last
                # window would otherwise be stamped five hours stale and re-walked
                # early. Measured on the cohort sweep and fixed there.
                "sweptAt": _now(),
            },
        ]
        data.state["codesKnown"] = len(data.rows) + len(new_rows)
        data.state["refusedCodes"] = len(data.refused) + len(new_refused)
        write_set(data, out_dir, new_rows, new_refused)
        report.new_reports += len(new_rows)
        report.new_refused += len(new_refused)


def known_kills(out_dir: Path, zone_id: int, difficulty: int) -> list[tuple[str, dict]]:
    """``(report code, kill)`` for every kill Stufe 3 recorded at this difficulty.

    Stufe 2 asks no ranking and searches no reports: its whole work list is what the
    directory already holds, which is the point of building the directory once.
    """
    data = load_zone(out_dir, zone_id)
    found: list[tuple[str, dict]] = []
    for line in data.rows.values():
        code = line.get("code")
        for kill in line.get("kills") or []:
            if isinstance(kill, dict) and kill.get("d") == difficulty:
                found.append((str(code), kill))
    found.sort(key=lambda pair: (pair[1].get("sMs") or 0, pair[0], pair[1].get("f") or 0))
    return found


def sweep_pair_kills(
    client,
    out_dir: Path,
    zone: dict,
    difficulty: int,
    options: CatalogueOptions,
    budget: Budget,
    report: CatalogueReport,
    *,
    run_id: str,
) -> None:
    """Stufe 2 for one (zone, difficulty): open each known kill once.

    The cursor is a kill's absolute ``startedAtMs`` and is only a **pre-filter**: the
    exact "already judged" test is the key set, which cannot be wrong on a tie. Two
    kills on the same millisecond would make a cursor alone either skip one or
    re-read one, and only one of those is safe -- so the cursor skips *strictly*
    earlier and the key set catches the rest.
    """
    zone_id = int(zone["id"])
    data = load_pair(out_dir, zone_id, difficulty)
    seen = data.done() - (data.retryable() if options.retry_errors else set())
    encounters = {
        int(e["id"]): e.get("name")
        for e in (zone.get("encounters") or [])
        if isinstance(e, dict) and isinstance(e.get("id"), int)
    }
    new_rows: list[str] = []
    new_refused: list[str] = []
    outcomes: dict[int, dict[str, int]] = {}
    read_count = 0
    alarm: str | None = None

    try:
        for code, kill in known_kills(out_dir, zone_id, difficulty):
            encounter_id = kill.get("e")
            fight_id = kill.get("f")
            if not isinstance(encounter_id, int) or not isinstance(fight_id, int):
                continue
            if encounter_id not in encounters:
                continue
            cursor = (
                (data.state.get("encounters") or {})
                .get(str(encounter_id), {})
                .get("cursorStartedAtMs")
            )
            if isinstance(cursor, (int, float)) and (kill.get("sMs") or 0) < cursor:
                continue
            if (code, fight_id) in seen:
                continue
            if options.kills and read_count >= options.kills:
                break
            budget.check()
            report.attempted += 1
            at = _now()
            tally = outcomes.setdefault(encounter_id, {})
            try:
                block = client.fight_structure(code, encounter_id, difficulty)
            except WarcraftLogsError as exc:
                log.warning("%s fight %s: %s", code, fight_id, exc)
                new_refused.append(
                    _refusal(
                        {"code": code, "fightId": fight_id, "encounterId": encounter_id},
                        "structure-error",
                        at=at,
                        run_id=run_id,
                    )
                )
                tally["structure-error"] = tally.get("structure-error", 0) + 1
                continue
            fight, reason = pick_fight(block, fight_id, difficulty)
            if fight is None:
                new_refused.append(
                    _refusal(
                        {"code": code, "fightId": fight_id, "encounterId": encounter_id},
                        reason or "structure-error",
                        at=at,
                        run_id=run_id,
                    )
                )
                tally[reason or "structure-error"] = tally.get(reason or "structure-error", 0) + 1
                continue
            line = kill_line(
                zone_id, encounter_id, difficulty, code, block, fight, at=at, run_id=run_id
            )
            new_rows.append(dumps_line(line))
            read_count += 1
            tally["read"] = tally.get("read", 0) + 1
            entry = (data.state.setdefault("encounters", {})).setdefault(str(encounter_id), {})
            entry["name"] = encounters.get(encounter_id)
            entry["cursorStartedAtMs"] = kill.get("sMs")
    except CatalogueAlarm as exc:
        # The file set writes NOTHING -- no lines, no state -- and the run goes on.
        # A scrub that fails open is the one failure this folder's privacy cannot
        # absorb, so the answer is to keep the bytes off disk rather than to log it.
        alarm = str(exc)
        raise
    finally:
        if alarm is None:
            stamp = _now()
            for encounter_id, tally in outcomes.items():
                entry = (data.state.setdefault("encounters", {})).setdefault(str(encounter_id), {})
                entry.setdefault("name", encounters.get(encounter_id))
                entry["sweptAt"] = stamp
                merged = dict(entry.get("outcomes") or {})
                for key, count in tally.items():
                    merged[key] = merged.get(key, 0) + count
                entry["outcomes"] = merged
                entry["killsRead"] = sum(
                    1
                    for (_c, _f), row in data.rows.items()
                    if row.get("encounterId") == encounter_id
                ) + tally.get("read", 0)
            write_set(data, out_dir, new_rows, new_refused)
            report.new_kills += len(new_rows)
            report.new_refused += len(new_refused)


def run_catalogue(
    client,
    out_dir: Path,
    options: CatalogueOptions,
    *,
    run_id: str | None = None,
    sleep=None,
    clock=None,
) -> CatalogueReport:
    """Stufe 3 then Stufe 2, zone by zone, in the order ``--zones`` states them.

    **Order of work is the contract's**, and it is not an optimisation: Stufe 3 for
    the live zone first, then Stufe 2 broadly. A report can be set private or deleted
    at any time, and when it goes the directory entry goes with it; Stufe 2 loses
    nothing by waiting, because a kill whose report survives can be opened tomorrow.

    The zone order is taken as priority and never re-sorted. Exit codes follow
    `progresssweep`'s so one workflow shell can read both: 0 clean or stopped on
    budget, 1 a run that could not start, 2 a zone the service would not list, 3 a
    schema alarm.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or os.environ.get("GITHUB_RUN_ID") or "local"
    report = CatalogueReport()
    budget = Budget(
        client=client,
        ceiling=options.point_ceiling,
        deadline_seconds=options.deadline_minutes * 60.0,
        **({"sleep": sleep} if sleep else {}),
        **({"clock": clock} if clock else {}),
    )

    try:
        # The pre-flight reading: an unreadable or refused answer here is a run that
        # could NOT START, and nothing has been paid for -- so it is EXIT_FAILED
        # rather than a green run that catalogued nothing. It may not sleep, because
        # a sleep is a bet that there will be work afterwards and no skip decision
        # has been made yet (#168).
        budget.check(may_sleep=False)
        report.points_first = _spent(client)
    except DeadlineReached as exc:
        report.exit_code = EXIT_OK
        report.stopped = str(exc)
        return report
    except WarcraftLogsError as exc:
        report.exit_code = EXIT_FAILED
        report.failed = str(exc)
        return report

    try:
        for zone_id in options.zones:
            try:
                zone, why = _zone_block(client, zone_id)
            except RateLimited as exc:
                raise _StopRun(str(exc)) from exc
            except WarcraftLogsError as exc:
                log.warning("zone %s: %s", zone_id, exc)
                report.zones_skipped.append(zone_id)
                continue
            if zone is None:
                log.warning("%s", why)
                report.zones_skipped.append(zone_id)
                continue

            try:
                if 3 in options.stages:
                    sweep_zone_reports(
                        client, out_dir, zone, options, budget, report, run_id=run_id
                    )
                if 2 in options.stages:
                    for difficulty in options.difficulties:
                        try:
                            sweep_pair_kills(
                                client,
                                out_dir,
                                zone,
                                difficulty,
                                options,
                                budget,
                                report,
                                run_id=run_id,
                            )
                        except CatalogueAlarm as exc:
                            log.error("%s: %s", pair_stem(zone_id, difficulty), exc)
                            report.alarmed_sets.append(pair_stem(zone_id, difficulty))
            except DeadlineReached as exc:
                raise _StopRun(str(exc)) from exc
            except RateLimited as exc:
                raise _StopRun(str(exc)) from exc
    except _StopRun as exc:
        report.stopped = exc.reason

    report.points_last = _spent(client)
    report.queries = getattr(getattr(client, "ledger", None), "queries", 0) or 0
    report.sleeps = budget.sleeps
    report.slept_seconds = budget.slept_seconds
    if report.alarmed_sets:
        report.exit_code = EXIT_SCHEMA_ALARM
    elif report.zones_skipped:
        report.exit_code = EXIT_ZONE_SKIPPED
    return report


def _spent(client) -> float | None:
    """The absolute hourly counter, or None. **None is UNMEASURED, never zero.**"""
    try:
        reading = client.rate_limit()
    except WarcraftLogsError:
        return None
    value = reading.get("pointsSpentThisHour")
    return float(value) if isinstance(value, (int, float)) else None


def describe(out_dir: Path, zones: Iterable[int], difficulties: Iterable[int]) -> list[str]:
    """What the directory holds, per file set, with no client and no query at all.

    The check to run after a seed and before a cron: it reads the files and prints
    what a sweep would skip, so a wrong path or an empty checkout is visible before a
    point is spent.
    """
    out_dir = Path(out_dir)
    lines: list[str] = []
    for zone_id in zones:
        data = load_zone(out_dir, zone_id)
        windows = data.state.get("windows") or []
        lines.append(
            f"{zone_stem(zone_id)}  stage 3: {len(data.rows)} report(s), "
            f"{len(data.refused)} refused, {len(windows)} window(s)"
            + (", walled" if any(w.get("walled") for w in windows if isinstance(w, dict)) else "")
        )
        if data.duplicate_rows or data.unreadable_lines:
            lines.append(
                f"    {data.duplicate_rows} duplicate line(s), "
                f"{data.unreadable_lines} unreadable line(s)"
            )
        for difficulty in difficulties:
            pair = load_pair(out_dir, zone_id, difficulty)
            entries = pair.state.get("encounters") or {}
            lines.append(
                f"{pair_stem(zone_id, difficulty)}  stage 2: {len(pair.rows)} kill(s), "
                f"{len(pair.refused)} refused, {len(entries)} encounter(s) with a cursor"
            )
    return lines


def validate(out_dir: Path) -> list[str]:
    """Every violation, as a sentence. Empty means the directory may be committed.

    The same four claims the cohort folder's gate makes, over this folder's names:
    every jsonl line is JSON, the manifest's counts equal the files' line counts, no
    tracked file is SHORTER than at HEAD (append-only is the promise a per-file digest
    on the private side would rest on), and no ``.tmp`` file from an interrupted write
    is lying about for ``git add`` to take.

    A **fifth** claim is this folder's own and is the reason the gate is not simply
    reused: no line may carry a name for an actor its own ``friendlyPlayers`` calls a
    player. The scrub runs at extraction and the alarm fires at the writer, and this
    is the third place, because the file is what actually leaves the machine -- and a
    line written by an older build of this module would pass both of the others.
    """
    out_dir = Path(out_dir)
    problems: list[str] = []
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"{manifest_path.name} is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{manifest_path.name} is not JSON: {exc}"]
    listed = {entry.get("path"): entry for entry in manifest.get("files") or []}

    for path in sorted(out_dir.glob("*.tmp")):
        problems.append(f"{path.name}: a temp file from an interrupted write; not committable")
    for path in sorted(out_dir.glob("z*.jsonl")):
        text = path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            problems.append(f"{path.name}: last line is not newline-terminated")
        lines = read_lines(path)
        stem, kind = path.name.rsplit(".", 2)[0], path.name.rsplit(".", 2)[1]
        for number, line in enumerate(lines, start=1):
            try:
                document = json.loads(line)
            except ValueError:
                problems.append(f"{path.name}:{number}: not JSON")
                continue
            leaked = leaked_player_names(
                [a for a in (document.get("actors") or []) if isinstance(a, dict)],
                document.get("friendlyPlayers"),
            )
            if leaked:
                problems.append(
                    f"{path.name}:{number}: {len(leaked)} player actor(s) carry a name "
                    f"(ids {leaked[:5]}); this file must not be committed"
                )
        # A file set is listed in the manifest under its ROWS path, so a refused
        # file has to look its own set up -- and which of the two names that is
        # depends on the stage, which the file name does not carry. Both are tried
        # rather than derived from the stem, because `z53` and `z53-d5` are the
        # stage's only tell and a rule over the stem would be a second place to
        # get it wrong.
        if kind == "refused":
            entry = listed.get(f"{stem}.reports.jsonl") or listed.get(f"{stem}.kills.jsonl")
        else:
            entry = listed.get(f"{stem}.{kind}.jsonl")
        if entry is None:
            problems.append(f"{path.name}: not in manifest.json")
        else:
            expected = entry.get("refused") if kind == "refused" else entry.get("lines")
            if expected != len(lines):
                problems.append(
                    f"{path.name}: manifest says {expected} line(s), file holds {len(lines)}"
                )
        at_head = _git_head_line_count(out_dir, path)
        if at_head is not None and len(lines) < at_head:
            problems.append(f"{path.name}: {len(lines)} line(s), shorter than HEAD's {at_head}")
    for path in sorted(out_dir.glob("z*.state.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: not JSON: {exc}")
    for listed_path in listed:
        if listed_path and not (out_dir / listed_path).is_file():
            stem = str(listed_path).rsplit(".", 2)[0]
            if not (out_dir / f"{stem}.state.json").is_file():
                problems.append(f"manifest lists {listed_path} but no such file set exists")
    return problems
