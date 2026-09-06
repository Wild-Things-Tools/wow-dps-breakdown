"""Where an encounter's adds appear, read out of Warcraft Logs event positions.

The question this exists for: an encounter spawns N copies of one NPC per wave but
at only M distinct places, so some places take a second copy. Which places, how
often, and can one take a third? Nothing in Warcraft Logs answers that directly --
there is no spawn event and no spawn-point table -- so the answer has to be built
out of *where each copy was when it was first observed*.

Read the three refusals below before using any number this module produces.

FIRST OBSERVED IS NOT SPAWNED, AND THIS MODULE CANNOT CLOSE THAT GAP
--------------------------------------------------------------------
``CLAUDE.md`` already records the general form: "there is no general spawn event.
``first_seen`` is *first damaged*". The same is true one level down for position. An
add that walks before anything touches it is first observed somewhere other than
where it appeared, and no amount of event reading recovers the difference.

What *can* be done is to make the gap measurable rather than silent:

* the earliest positioned event per copy is taken, never a later one;
* ``SpawnSighting.delay_ms`` records how long after the fight's start that was, and
  ``dispersion`` over a cluster records how far apart the sightings of one place
  are. Tight clusters with short delays are evidence that copies are observed near
  where they appeared; a smear is evidence that they are not, and then the honest
  answer is that logs cannot site this encounter's spawns.

So this module reports a distribution and its width. It does not report "the spawn
points" as though they were read off a table.

WHOSE POSITION IT IS, IS MEASURED RATHER THAN ASSUMED
-----------------------------------------------------
Two shapes are in circulation for the same data and this project has met both:

* the **Scripting API** nests it, ``event.sourceResources`` / ``event.targetResources``
  each being a ``ResourceData`` (``x``, ``y``, ``facing``, ``hitPoints``,
  ``maxHitPoints``) -- see ``wcl-components/definitions/RpgLogs.d.ts``;
* the **v2 API** was measured by wtt-frontend to send the fields **flat on the
  event** with a ``resourceActor`` discriminator, 1 = source and 2 = target.

Reading one and calling the other absent is exactly how a feature ends up reporting
nothing while every number beside it looks healthy. ``event_position`` therefore
accepts both, and ``describe_event_shapes`` prints which one actually turned up,
with the ``resourceActor`` tally beside it -- because on a damage event the source
is whoever swung and the target is the add, and a position taken from the wrong end
is a plausible-looking measurement of a player's feet.

A COPY IS ``(id, instance)``, NEVER ``id``
------------------------------------------
Fourteen copies of one add share one actor id and are separated only by
``targetInstance`` / ``sourceInstance``. ``CLAUDE.md`` states it as a rule because
collapsing them "reports a wave of five as one enemy alive for the union of their
lifetimes"; here it would report fourteen spawn points as one.

Warcraft Logs also carries a second axis, ``instanceGroup`` -- ``RpgLogs.d.ts``
declares ``instanceCountForNpc`` *and* ``instanceGroupCountForNpc``, and
``FIGHT_STRUCTURE_QUERY`` already asks for ``enemyNPCs { instanceCount groupCount }``
and reads neither. ``enemy_npc_counts`` reads it, because "how many copies and how
many groups" is half the question and costs nothing.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The v2 discriminator, measured by wtt-frontend against live responses: a flat
#: resource block belongs to the event's source (1) or its target (2). Named rather
#: than written as bare integers at the comparison, because "2" at a call site is
#: the kind of literal that gets read as "the second one" a year later.
RESOURCE_ACTOR_SOURCE = 1
RESOURCE_ACTOR_TARGET = 2

#: Which end of an event a caller wants the position of.
END_SOURCE = "source"
END_TARGET = "target"


@dataclass(frozen=True)
class Position:
    """One reading of where an actor was, and where the reading came from.

    ``whose`` is the end of the event the coordinates describe, and it is carried
    rather than assumed because the two payload shapes disagree about how they say
    it. ``shape`` is ``"nested"`` or ``"flat"`` -- kept so a run can report which
    dialect the service is speaking rather than only that it found something.
    """

    x: float
    y: float
    whose: str
    shape: str
    facing: float | None = None


def _coerce(value: Any) -> float | None:
    """A coordinate, or None -- and ``0`` is a coordinate.

    Written out rather than using truthiness because an add standing at x=0 is a
    real reading, and ``if not value`` would drop it. The origin is the one place a
    plausible-looking hole would sit exactly where a spawn point might be.
    """
    if isinstance(value, bool):  # bool is an int; a flag is not a coordinate
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _from_block(block: Any, whose: str, shape: str) -> Position | None:
    if not isinstance(block, dict):
        return None
    x = _coerce(block.get("x"))
    y = _coerce(block.get("y"))
    if x is None or y is None:
        return None
    return Position(x, y, whose, shape, _coerce(block.get("facing")))


def event_position(event: dict, want: str) -> Position | None:
    """The position of one end of an event, in whichever shape the payload uses.

    ``want`` is ``END_SOURCE`` or ``END_TARGET``. Returns None when the event
    carries no position for that end -- which is an ordinary state, not a failure:
    resources ride on some event types and not others, and only when the query asked
    for them.

    The nested shape is tried first and answers unambiguously. The flat shape has to
    be interrogated: its single block belongs to whichever end ``resourceActor``
    names, so a flat event whose ``resourceActor`` is the *other* end has no position
    for the end being asked about, and saying so is the whole point. An absent
    ``resourceActor`` on a flat block is **refused** rather than guessed at, because
    guessing here silently returns a player's coordinates for an add.
    """
    nested_key = "sourceResources" if want == END_SOURCE else "targetResources"
    nested = _from_block(event.get(nested_key), want, "nested")
    if nested is not None:
        return nested

    flat = _from_block(event, want, "flat")
    if flat is None:
        return None
    actor = event.get("resourceActor")
    wanted = RESOURCE_ACTOR_SOURCE if want == END_SOURCE else RESOURCE_ACTOR_TARGET
    if actor == wanted:
        return flat
    return None


@dataclass(frozen=True)
class SpawnSighting:
    """The earliest positioned observation of one copy of one NPC.

    ``instance`` is what separates the copies; ``instance_group`` is Warcraft Logs'
    second axis and is None when the payload did not carry one. ``delay_ms`` is
    measured from the fight's start, and it is the number that says whether "first
    observed" is close enough to "spawned" to be worth drawing.
    """

    game_id: int
    actor_id: int
    instance: int | None
    instance_group: int | None
    x: float
    y: float
    timestamp_ms: float
    delay_ms: float
    event_type: str
    whose: str
    shape: str


def _instance_of(event: dict, end: str) -> int | None:
    key = "sourceInstance" if end == END_SOURCE else "targetInstance"
    value = event.get(key)
    return value if isinstance(value, int) else None


def _instance_group_of(event: dict, end: str) -> int | None:
    key = "sourceInstanceGroup" if end == END_SOURCE else "targetInstanceGroup"
    value = event.get(key)
    return value if isinstance(value, int) else None


def _actor_of(event: dict, end: str) -> int | None:
    key = "sourceID" if end == END_SOURCE else "targetID"
    value = event.get(key)
    return value if isinstance(value, int) else None


def spawn_sightings(
    events: Iterable[dict],
    actor_game_ids: dict[int, int],
    game_id: int,
    fight_start_ms: float,
) -> list[SpawnSighting]:
    """The earliest positioned sighting of every copy of ``game_id`` in a fight.

    ``actor_game_ids`` maps the report-local actor id (what events carry) to the
    game id (what identifies an NPC across reports) -- the mapping
    ``masterData.actors`` provides. Events naming an actor the map does not know are
    skipped rather than guessed at; an unknown actor is precisely the case where a
    guess would attribute another NPC's position to this one.

    **Both ends of every event are considered.** An add is the *target* of the damage
    it takes and the *source* of the spells it casts, and which of those a given
    stream carries is a property of the stream rather than something to assume. The
    earliest sighting wins regardless of which end produced it, and
    ``SpawnSighting.whose`` records which one did, so a caller can tell a wave sited
    from its own casts from one sited from the raid hitting it.

    Sorting is by ``(timestamp, actor, instance)`` rather than by timestamp alone, so
    two events on the same millisecond resolve the same way on a re-run. A pass whose
    answer depends on dict ordering is not reproducible, and this project publishes
    its numbers.
    """
    best: dict[tuple[int, int | None], SpawnSighting] = {}
    rows = [event for event in events if isinstance(event, dict)]
    rows.sort(
        key=lambda e: (
            e.get("timestamp") if isinstance(e.get("timestamp"), (int, float)) else math.inf,
            _actor_of(e, END_TARGET) or _actor_of(e, END_SOURCE) or 0,
            _instance_of(e, END_TARGET) or _instance_of(e, END_SOURCE) or 0,
        )
    )

    for event in rows:
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        for end in (END_TARGET, END_SOURCE):
            actor_id = _actor_of(event, end)
            if actor_id is None or actor_game_ids.get(actor_id) != game_id:
                continue
            position = event_position(event, end)
            if position is None:
                continue
            instance = _instance_of(event, end)
            key = (actor_id, instance)
            if key in best:
                continue
            best[key] = SpawnSighting(
                game_id=game_id,
                actor_id=actor_id,
                instance=instance,
                instance_group=_instance_group_of(event, end),
                x=position.x,
                y=position.y,
                timestamp_ms=float(timestamp),
                delay_ms=float(timestamp) - fight_start_ms,
                event_type=str(event.get("type") or "?"),
                whose=position.whose,
                shape=position.shape,
            )

    return sorted(best.values(), key=lambda s: (s.timestamp_ms, s.actor_id, s.instance or 0))


@dataclass
class ShapeReport:
    """What one event stream turned out to contain. The diagnostic half of this module.

    Every field here answers a question that, left unasked, produces a confident
    wrong answer somewhere downstream. ``positioned`` being 0 while ``events`` is
    large is the signature of ``includeResources`` not having been sent; a
    ``resource_actors`` tally of only ``{1: n}`` on a damage stream means the
    coordinates on offer are the attacker's, not the add's.
    """

    data_type: str
    events: int = 0
    with_flat_position: int = 0
    with_nested_position: int = 0
    resource_actors: Counter = field(default_factory=Counter)
    event_types: Counter = field(default_factory=Counter)
    keys_seen: Counter = field(default_factory=Counter)
    npc_events: int = 0
    npc_positioned: int = 0
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None

    @property
    def positioned(self) -> int:
        return self.with_flat_position + self.with_nested_position


def describe_event_shapes(
    events: Iterable[dict],
    data_type: str,
    actor_game_ids: dict[int, int],
    game_id: int,
) -> ShapeReport:
    """Measure what a stream actually carries, without deciding anything from it.

    This is the function a first live run exists for. The project's rule is that an
    absence is only a measurement once the thing that would have answered has been
    named -- so this counts events, positions, dialects, ``resourceActor`` values and
    the observed coordinate ranges, and leaves every conclusion to a human reading
    the printout.

    The coordinate RANGE matters as much as the presence: nothing in Warcraft Logs'
    schema or in ``RpgLogs.d.ts`` documents the units, so whether these are world
    yards, map hundredths or something else is decided by looking at the numbers
    against the encounter's known extent. Publishing a plot before that is settled
    would be drawing an unlabelled axis.
    """
    report = ShapeReport(data_type=data_type)
    xs: list[float] = []
    ys: list[float] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        report.events += 1
        report.event_types[str(event.get("type") or "?")] += 1
        for key in event:
            report.keys_seen[key] += 1

        actor = event.get("resourceActor")
        if actor is not None:
            report.resource_actors[actor] += 1

        npc_ends = [
            end
            for end in (END_TARGET, END_SOURCE)
            if actor_game_ids.get(_actor_of(event, end) or -1) == game_id
        ]
        if npc_ends:
            report.npc_events += 1

        # Counted per EVENT, not per end: an event carrying a position for both
        # ends is one positioned event, and counting it twice would make the
        # dialect tallies disagree with `events` for no reason a reader could
        # follow. The first end that answers decides the shape.
        for end in (END_TARGET, END_SOURCE):
            position = event_position(event, end)
            if position is None:
                continue
            if position.shape == "flat":
                report.with_flat_position += 1
            else:
                report.with_nested_position += 1
            xs.append(position.x)
            ys.append(position.y)
            break

        # ...but THIS one asks a narrower question, and the difference is the
        # whole point of the diagnostic: a damage stream where every position
        # belongs to the attacker has `positioned == events` and
        # `npc_positioned == 0`, which is the state that would otherwise be read
        # as "we have coordinates for the adds".
        if any(event_position(event, end) is not None for end in npc_ends):
            report.npc_positioned += 1

    if xs:
        report.x_range = (min(xs), max(xs))
    if ys:
        report.y_range = (min(ys), max(ys))
    return report


def enemy_npc_counts(fight: dict) -> list[dict]:
    """``enemyNPCs`` for one fight: how many copies, and how many groups.

    ``FIGHT_STRUCTURE_QUERY`` has always asked for
    ``enemyNPCs { id gameID instanceCount groupCount }`` and nothing has ever read it.
    It is the cheapest answer in this whole area -- it costs no extra query, it is
    already cached for every fight this project has ever probed, and it answers "how
    many of this add were up, in how many groups" without touching an event stream.

    It does **not** answer where they were, and it does not say how the copies are
    distributed across the groups. Those need the events.
    """
    rows = fight.get("enemyNPCs")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "actorId": row.get("id"),
                "gameId": row.get("gameID"),
                "instanceCount": row.get("instanceCount"),
                "groupCount": row.get("groupCount"),
            }
        )
    return out


def actor_game_id_map(master_actors: Iterable[dict]) -> dict[int, int]:
    """Report-local actor id -> game id, from ``masterData.actors``.

    Report-local ids are what events carry and they mean nothing across reports;
    the game id is the NPC. Every consumer here joins through this map rather than
    matching names, because a name is localised and a game id is not.
    """
    out: dict[int, int] = {}
    for actor in master_actors or []:
        if not isinstance(actor, dict):
            continue
        actor_id = actor.get("id")
        game_id = actor.get("gameID")
        if isinstance(actor_id, int) and isinstance(game_id, int):
            out[actor_id] = game_id
    return out


# --------------------------------------------------------------------------------
# The probe. Everything above is pure and tested without credentials; everything
# below needs them and is deliberately thin, because a rule that lives in a command
# body cannot be tested and cannot be reused.
# --------------------------------------------------------------------------------

#: Streams worth asking for positions from, and why each is here rather than a
#: default-everything sweep. An add is the TARGET of the damage it takes and the
#: SOURCE of what it casts, so the two answer the same question from opposite ends
#: and either may be the one that carries a resource block. `Deaths` is cheap and
#: bounds the other end of a copy's life, which is what says whether a wave died in
#: place or was dragged.
DEFAULT_STREAMS = ("DamageTaken", "Casts", "Deaths")


def add_arguments(parser) -> None:
    parser.add_argument(
        "--encounter",
        type=int,
        required=True,
        help="Warcraft Logs encounter id. A PTR id (a live id with a 5 in front) is "
        "resolved to its live twin only when the schema gives both the same name",
    )
    parser.add_argument("--difficulty", type=int, default=5, help="5 = Mythic, 4 = Heroic")
    parser.add_argument(
        "--npc",
        type=int,
        required=True,
        help="game id of the NPC whose copies are being sited, e.g. 270898",
    )
    parser.add_argument(
        "--reports",
        type=int,
        default=1,
        help="distinct kills to read. One is enough to settle the payload shape; more "
        "is what a distribution needs",
    )
    parser.add_argument(
        "--report",
        action="append",
        help="read this report code instead of searching the rankings; repeatable",
    )
    parser.add_argument(
        "--streams",
        nargs="+",
        default=list(DEFAULT_STREAMS),
        help=f"event streams to ask for positions from (default: {' '.join(DEFAULT_STREAMS)})",
    )
    parser.add_argument("--max-pages", type=int, default=4, help="event pages per stream per fight")
    parser.add_argument("--events-limit", type=int, default=10000)
    parser.add_argument(
        "--point-ceiling",
        type=float,
        default=0.5,
        help="abort once this fraction of the hourly point budget is spent",
    )
    parser.add_argument("--cache", help="directory for the response cache")
    parser.add_argument("--out", help="write the observations as JSON to this path")


def _kill_candidates(client, encounter_id: int, difficulty: int, limit: int):
    """(report code, fight id) pairs from the character rankings, newest page first.

    `characterRankings` rows are measured to carry `report.code` and
    `report.fightID` -- note the capital ID, which is the sort of thing that reads
    as a typo and is not. One row per player means many rows per kill, so the pairs
    are deduplicated on the report and the fight together: two players in one kill
    are one kill.
    """
    from .warcraftlogs import RANKINGS_QUERY

    data = client.query(
        RANKINGS_QUERY,
        {
            "encounterId": encounter_id,
            "difficulty": difficulty,
            "metric": "dps",
            "className": None,
            "specName": None,
            "page": 1,
        },
        label=f"rankings:{encounter_id}:d{difficulty}",
    )
    encounter = ((data.get("worldData") or {}).get("encounter")) or {}
    rankings = encounter.get("characterRankings") or {}
    if isinstance(rankings, str):
        import json as _json

        rankings = _json.loads(rankings)
    rows = (rankings or {}).get("rankings") or []

    seen: list[tuple[str, int]] = []
    for row in rows:
        report = (row or {}).get("report") or {}
        code = report.get("code")
        fight_id = report.get("fightID")
        if isinstance(code, str) and isinstance(fight_id, int):
            pair = (code, fight_id)
            if pair not in seen:
                seen.append(pair)
        if len(seen) >= limit:
            break
    return seen, len(rows)


def run(args) -> int:
    """Measure whether -- and how -- this API answers "where did that add appear".

    Prints; writes only when ``--out`` is given. Nothing here decides anything about
    spawn points: the printout is the evidence a human reads before a pattern is
    claimed, which is the same posture ``fight-probe --probe`` and
    ``harvest-builds --probe`` already take.
    """
    import logging

    from . import fightprobe, harvest
    from .warcraftlogs import Credentials, WarcraftLogsClient, WarcraftLogsError

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        logging.error("%s", exc)
        return 1

    out: dict[str, Any] = {"requestedEncounter": args.encounter, "npc": args.npc}
    with WarcraftLogsClient(
        credentials, cache_dir=Path(args.cache) if args.cache else None
    ) as client:
        client.rate_limit()

        encounter_id = int(args.encounter)
        pairs: list[tuple[str, int]] = []
        ranked_rows = 0

        if args.report:
            pairs = [(code, -1) for code in args.report]
            print(f"reading the reports named on the command line: {', '.join(args.report)}")
        else:
            pairs, ranked_rows = _kill_candidates(
                client, encounter_id, args.difficulty, args.reports
            )
            print(f"encounter {encounter_id}: {ranked_rows} ranking row(s), {len(pairs)} kill(s)")

            if not pairs:
                # The PTR/live split. `harvest.choose_encounter_id` owns this rule
                # and its refusals -- a twin is taken ONLY when the schema gives both
                # ids the same name -- and is reused rather than re-derived, because
                # a second copy of a rule is how two answers to one question appear.
                choice = harvest.choose_encounter_id(
                    encounter_id,
                    client.encounter_name(encounter_id),
                    False,
                    client.encounter_name,
                )
                print(f"  {choice.reason}")
                out["idChoice"] = {
                    "requested": choice.requested,
                    "used": choice.used,
                    "reason": choice.reason,
                    "substituted": choice.substituted,
                }
                if choice.substituted and choice.used:
                    encounter_id = int(choice.used)
                    pairs, ranked_rows = _kill_candidates(
                        client, encounter_id, args.difficulty, args.reports
                    )
                    print(
                        f"encounter {encounter_id}: {ranked_rows} ranking row(s), "
                        f"{len(pairs)} kill(s)"
                    )

        out["usedEncounter"] = encounter_id
        if not pairs:
            print("no kills to read: nothing here says anything about spawn positions")
            out["fights"] = []
            _finish(client, out, args)
            return 3

        fights_out = []
        for code, wanted_fight in pairs:
            try:
                fightprobe.check_budget(client, args.point_ceiling)
            except fightprobe.PointBudgetExhausted as exc:
                # Return what was read rather than losing it. This project has thrown
                # away paid-for work four times by letting this escape one frame above
                # where the work happened; the fix is always to catch it around the
                # unit of work, not around the call.
                print(f"stopped on the point ceiling: {exc}")
                out["stoppedBy"] = str(exc)
                break

            # `fight_structure` returns the REPORT, not the envelope -- it unwraps
            # `reportData.report` itself, exactly as `fightprobe._probe_fight` reads
            # it. Unwrapping a second time here produced `{}`, so every fight was
            # "not in the report's fights" while the payload plainly held it. Measured
            # on 2026-09-06, run 34030745850: the cached response carries fights 16-22
            # of `M98z37nZ21AYrQVK`, all encounter 3421 difficulty 5, and the run
            # printed "no fight 22 at difficulty 5".
            report = client.fight_structure(code, encounter_id, args.difficulty)
            master = (report.get("masterData") or {}).get("actors") or []
            actor_map = actor_game_id_map(master)
            names = {
                a.get("gameID"): a.get("name")
                for a in master
                if isinstance(a, dict) and isinstance(a.get("gameID"), int)
            }
            fights = report.get("fights") or []
            if wanted_fight >= 0:
                fights = [f for f in fights if f.get("id") == wanted_fight]
            if not fights:
                print(f"  {code}: no fight {wanted_fight} at difficulty {args.difficulty}")
                continue
            fight = fights[0]

            start = float(fight.get("startTime") or 0)
            end = float(fight.get("endTime") or 0)
            print(
                f"\n=== report {code} fight {fight.get('id')} "
                f"({(end - start) / 1000:.1f}s, size {fight.get('size')})"
            )

            npcs = enemy_npc_counts(fight)
            print("  enemyNPCs (already in the payload, read by nothing until now):")
            for row in sorted(npcs, key=lambda r: -(r.get("instanceCount") or 0))[:12]:
                mark = "  <-- asked about" if row.get("gameId") == args.npc else ""
                name = str(names.get(row.get("gameId")))[:26]
                print(
                    f"    gameId {row.get('gameId'):>7}  {name:<26} "
                    f"instances {str(row.get('instanceCount')):>4}  "
                    f"groups {str(row.get('groupCount')):>4}{mark}"
                )
            if not any(r.get("gameId") == args.npc for r in npcs):
                print(f"    NPC {args.npc} is NOT among this fight's enemyNPCs")

            fight_row: dict[str, Any] = {
                "reportCode": code,
                "fightId": fight.get("id"),
                "durationSeconds": round((end - start) / 1000, 3),
                "raidSize": fight.get("size"),
                "enemyNpcs": npcs,
                "streams": [],
                "sightings": [],
            }

            for stream in args.streams:
                try:
                    fightprobe.check_budget(client, args.point_ceiling)
                except fightprobe.PointBudgetExhausted as exc:
                    print(f"  stopped on the point ceiling before {stream}: {exc}")
                    out["stoppedBy"] = str(exc)
                    break
                try:
                    events, truncated = client.fight_events(
                        code,
                        int(fight.get("id")),
                        stream,
                        "Enemies",
                        start,
                        end,
                        limit=args.events_limit,
                        max_pages=args.max_pages,
                        include_resources=True,
                    )
                except WarcraftLogsError as exc:
                    print(f"  {stream}: refused by the service: {exc}")
                    fight_row["streams"].append({"dataType": stream, "error": str(exc)})
                    continue

                shape = describe_event_shapes(events, stream, actor_map, args.npc)
                sightings = spawn_sightings(events, actor_map, args.npc, start)
                print(
                    f"  {stream:<11} {shape.events:>6} events, "
                    f"{shape.positioned:>6} positioned "
                    f"(flat {shape.with_flat_position}, nested {shape.with_nested_position}), "
                    f"npc {shape.npc_events} of which {shape.npc_positioned} positioned"
                    f"{'  [TRUNCATED]' if truncated else ''}"
                )
                if shape.resource_actors:
                    print(f"    resourceActor tally: {dict(shape.resource_actors)}")
                if shape.x_range:
                    print(
                        f"    x {shape.x_range[0]:.1f}..{shape.x_range[1]:.1f}   "
                        f"y {shape.y_range[0]:.1f}..{shape.y_range[1]:.1f}"
                    )
                if shape.positioned == 0 and shape.events:
                    print(
                        "    NO POSITIONS. Either includeResources did not take on this "
                        "stream, or this event type carries no resource block."
                    )
                print(f"    keys: {', '.join(sorted(shape.keys_seen)[:24])}")
                print(f"    {len(sightings)} distinct copy/copies of npc {args.npc} sited")
                for s in sightings[:20]:
                    print(
                        f"      inst {str(s.instance):>4} grp {str(s.instance_group):>4}  "
                        f"({s.x:9.2f}, {s.y:9.2f})  +{s.delay_ms / 1000:7.2f}s  "
                        f"{s.event_type}/{s.whose}/{s.shape}"
                    )

                fight_row["streams"].append(
                    {
                        "dataType": stream,
                        "events": shape.events,
                        "positioned": shape.positioned,
                        "flat": shape.with_flat_position,
                        "nested": shape.with_nested_position,
                        "npcEvents": shape.npc_events,
                        "npcPositioned": shape.npc_positioned,
                        "resourceActors": dict(shape.resource_actors),
                        "xRange": list(shape.x_range) if shape.x_range else None,
                        "yRange": list(shape.y_range) if shape.y_range else None,
                        "truncated": truncated,
                        "keys": sorted(shape.keys_seen),
                    }
                )
                fight_row["sightings"].extend(
                    {
                        "dataType": stream,
                        "actorId": s.actor_id,
                        "instance": s.instance,
                        "instanceGroup": s.instance_group,
                        "x": s.x,
                        "y": s.y,
                        "delayMs": s.delay_ms,
                        "eventType": s.event_type,
                        "whose": s.whose,
                        "shape": s.shape,
                    }
                    for s in sightings
                )

            fights_out.append(fight_row)
            if "stoppedBy" in out:
                break

        out["fights"] = fights_out

    _finish(client, out, args)
    return 0


def _finish(client, out: dict, args) -> None:
    import json

    ledger = client.ledger
    spent = ledger.spent
    out["cost"] = ledger.to_json() if hasattr(ledger, "to_json") else None
    reading = "UNMEASURED (the hourly counter did not move)" if not spent else f"{spent:.1f} points"
    print(f"\ncost: {reading}, {len(ledger.entries)} query/queries")
    if args.out:
        import pathlib

        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path}")
