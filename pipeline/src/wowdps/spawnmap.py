"""Pooling one boss's spawn sightings across kills into a map the site can draw.

``addspawns`` answers for **one kill**: which places that kill's copies appeared at,
in which waves, and how often a place took a second copy. That is the honest unit of
its measurement and it is not the unit a map wants. A single kill of The Twin Fangs
yields thirty clusters; ten kills yield the same thirty *places*, and only the
pooling says they are the same thirty.

So this module is the second fold, and the whole of its value is that the thing it
publishes is checkable:

* every spot carries **how many kills it was seen in** and **how far its sightings
  scattered**, so a reader can see the grid is a grid rather than being told;
* the tolerance that decides "these are the same place" is **found in the data**,
  the same way ``addspawns.find_break`` finds the per-kill one, and a sample with no
  hole in it publishes no spots at all;
* "which places take a second copy" is published as a **test**, not as a sentence:
  the pooled rate, the chi-square against "every spot has its own rate", and the
  degrees of freedom, so the claim that no spot is preferred can be disagreed with.

WHAT A SPOT IS EVIDENCE OF
--------------------------
The same thing a sighting is evidence of, and no more. ``addspawns`` states it at
length: ``first_seen`` is *first damaged*, so a copy that walked before anything hit
it is sited where it was found rather than where it appeared. Pooling does not close
that gap -- it **measures** it. A spot's ``spread`` is the widest distance between
two of its sightings across every kill, and on MID2's Twin Fangs that is 75-188
units against 620 to the nearest neighbouring spot. The grid survives the slack; a
claim about spawn *order* still does not, and ``perWave.distinctBeforeFirstRepeat``
is published as an observation with that stated beside it.

WHY THE AREAS ARE NOT COMPASS DIRECTIONS
----------------------------------------
The three sets of ten on this encounter sit around (0, 67500), (-5500, 58000) and
(+5400, 58000), which reads as north, south-west and south-east. Naming them that
way would be *my* reading of a coordinate system nobody here has established the
orientation of. They are numbered instead, in the order the fight visits them, which
is derived: an area is a connected component of the wave-to-spot graph, and the
number follows the earliest wave that used it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import addspawns

#: Bumped when a reader would have to change. 1 is the first shape anything reads.
SPAWNS_SCHEMA_VERSION = 1

#: Fields describing *when the file was written* rather than what is in it, so a
#: quiet re-run leaves nothing to commit. Nested paths matter -- `fightdataset`
#: recorded the version of this that listed only the top-level stamp, where
#: `measurement.generatedAt` still differed every run and NEITHER stamp settled.
#: `measurement.cost` is in here for the same reason and it is the one that is easy
#: to miss: it is a reading of Warcraft Logs' hourly meter taken when the pass ran,
#: so five of its fields differ on every run by construction. It stays IN the
#: document -- what a pass costs is the open question behind every budget decision
#: here and this is the only measurement of it -- and out of the comparison.
_PROVENANCE_PATHS: tuple[tuple[str, ...], ...] = (
    ("generatedAt",),
    ("measurement", "generatedAt"),
    ("measurement", "cost"),
)

#: A pooled spot needs sightings from more than one kill before "the same place"
#: means anything. One kill's cluster is already an ``addspawns`` answer; pooling a
#: single kill would re-publish it under a word that promises more.
MIN_KILLS_FOR_A_MAP = 2


@dataclass(frozen=True)
class Sighting:
    """One kill's reading of one spot: where its cluster's centre was, and when."""

    kill: int
    x: float
    y: float
    #: The cluster's own spread within that kill, kept so pooling cannot hide it.
    spread: float
    #: Delay from the pull of the earliest copy at this cluster, seconds.
    first_seconds: float
    #: How many of that kill's waves used it, and in how many a second copy landed.
    waves_present: int
    waves_with_second: int


@dataclass(frozen=True)
class Spot:
    """A place the encounter puts copies, pooled over every kill that showed it."""

    spot: int
    area: int
    x: float
    y: float
    #: Distinct kills this spot was seen in. Below the kill count is ordinary -- a
    #: truncated event fetch or an add nobody damaged costs a sighting, not a spot.
    seen_in_kills: int
    #: Widest distance between two of this spot's sightings, over all kills. This
    #: is the "first damaged is not spawned" slack, measured.
    spread: float
    first_seconds: float
    waves_present: int
    waves_with_second: int

    def to_json(self) -> dict:
        return {
            "spot": self.spot,
            "area": self.area,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "seenInKills": self.seen_in_kills,
            "spread": round(self.spread, 1),
            "firstSeconds": round(self.first_seconds, 1),
            "wavesPresent": self.waves_present,
            "wavesWithSecondCopy": self.waves_with_second,
        }


@dataclass(frozen=True)
class RepeatTest:
    """Whether the spots that take a second copy are the same ones every wave.

    The question the owner asked -- *are the four repeat positions fixed, or can some
    never take a second?* -- has three possible answers and only one of them is
    "no difference measured". Publishing the rate alone would leave a reader unable
    to tell a settled answer from a thin sample, so the test travels with it.

    ``chi_square`` is over "every spot repeats at its own rate" against the pooled
    rate. ``z`` is the Wilson-Hilferty normal approximation, which is what makes an
    odd number of degrees of freedom readable without a table.

    **The test is ONE-SIDED, and the two-sided version of it was wrong.** ``|z|``
    fires on a chi-square that is too SMALL as well as too large -- and too small
    means the spots repeated *more evenly than chance*, which is the opposite of
    "some spot is preferred". Measured on a fixture where ten places take exactly
    two second copies each over ten waves: chi-square 0.0, z **-6.21**, and a
    two-sided rule reports the most uniform sample constructible as a detectable
    difference between spots. Do not restore ``abs``.
    """

    spot_appearances: int
    with_second_copy: int
    chi_square: float
    degrees_of_freedom: int
    z: float

    @property
    def rate(self) -> float:
        return self.with_second_copy / self.spot_appearances if self.spot_appearances else 0.0

    @property
    def separates(self) -> bool:
        """True when some spot is detectably preferred over the others.

        One-sided: only an EXCESS of variation answers the question being asked.
        See the class docstring for the fixture that made the two-sided version
        report a perfectly uniform sample as a separation.
        """
        return self.z >= 1.96

    def to_json(self) -> dict:
        return {
            "spotAppearances": self.spot_appearances,
            "withSecondCopy": self.with_second_copy,
            "rate": round(self.rate, 4),
            "chiSquare": round(self.chi_square, 2),
            "degreesOfFreedom": self.degrees_of_freedom,
            "z": round(self.z, 2),
            "separates": self.separates,
        }


@dataclass
class Kill:
    """One kill as this module reads it: the pattern plus how completely it was read."""

    report_code: str | None
    fight_id: int | None
    duration_seconds: float | None
    raid_size: int | None
    truncated: bool
    pattern: dict | None = None
    # What the FIGHT stated, which need not be what the run asked for. `None` means
    # the payload carried no such field -- written before the probe recorded it, or a
    # fight the service answered without one -- and unknown is not the same as wrong.
    difficulty: int | None = None
    started_at: float | None = None


@dataclass
class EncounterMap:
    """Everything published about one boss's spawn map."""

    encounter_id: int
    filed_as: int | None
    npc_game_id: int
    difficulty: int
    kills: list[Kill] = field(default_factory=list)
    spots: list[Spot] = field(default_factory=list)
    areas: list[dict] = field(default_factory=list)
    repeat: RepeatTest | None = None
    per_wave: dict = field(default_factory=dict)
    tolerance: dict | None = None
    refusal: str | None = None


def _centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def _widest(points: Sequence[tuple[float, float]]) -> float:
    return max(
        (math.hypot(a[0] - b[0], a[1] - b[1]) for a in points for b in points),
        default=0.0,
    )


def read_kill(fight: dict, npc_game_id: int) -> Kill:
    """One payload fight, folded into a kill with its pattern recomputed.

    The pattern is **recomputed from the sightings** rather than read out of the
    payload's own ``pattern`` block, for the reason ``fightdataset`` recomputes
    rather than trusting: a payload written before that block existed carries none,
    and a publisher that read it would answer "this kill has no pattern" for a kill
    that plainly does. The sightings are the measurement; the block is a printout.
    """
    rows = [r for r in (fight.get("sightings") or []) if isinstance(r, dict)]
    sightings = [
        addspawns.SpawnSighting(
            game_id=npc_game_id,
            actor_id=row["actorId"],
            instance=row.get("instance"),
            instance_group=row.get("instanceGroup"),
            x=float(row["x"]),
            y=float(row["y"]),
            timestamp_ms=float(row["delayMs"]),
            delay_ms=float(row["delayMs"]),
            event_type=str(row.get("eventType") or "?"),
            whose=str(row.get("whose") or "?"),
            shape=str(row.get("shape") or "?"),
        )
        for row in rows
        if isinstance(row.get("x"), (int, float)) and isinstance(row.get("y"), (int, float))
    ]
    merged = addspawns.merge_sightings(sightings)
    pattern = addspawns.describe_pattern(merged) if len(merged) >= 3 else None
    # A stream that stopped at its page limit read a prefix of the fight, which is
    # how a wave comes back nine copies wide instead of fourteen. It is not a reason
    # to drop the kill -- the spots it did see are real -- so it is counted instead.
    truncated = any(bool(s.get("truncated")) for s in (fight.get("streams") or []))
    stated = fight.get("difficulty")
    started = fight.get("startedAt")
    return Kill(
        report_code=fight.get("reportCode"),
        fight_id=fight.get("fightId"),
        duration_seconds=fight.get("durationSeconds"),
        raid_size=fight.get("raidSize"),
        truncated=truncated,
        pattern=pattern,
        difficulty=stated if isinstance(stated, int) else None,
        started_at=float(started) if isinstance(started, (int, float)) else None,
    )


def sightings_of_spots(kills: Sequence[Kill]) -> list[Sighting]:
    """Every kill's clusters, flattened, with that kill's own reading attached."""
    out: list[Sighting] = []
    for index, kill in enumerate(kills):
        pattern = kill.pattern
        if not pattern:
            continue
        waves = pattern.get("waves") or []
        present: dict[int, int] = {}
        second: dict[int, int] = {}
        for row in waves:
            order = row.get("order") or []
            for position in set(order):
                present[position] = present.get(position, 0) + 1
                if order.count(position) >= 2:
                    second[position] = second.get(position, 0) + 1
        for entry in pattern.get("positions") or []:
            number = entry["position"]
            out.append(
                Sighting(
                    kill=index,
                    x=float(entry["x"]),
                    y=float(entry["y"]),
                    spread=float(entry.get("spread") or 0.0),
                    first_seconds=_first_seconds(pattern, number),
                    waves_present=present.get(number, 0),
                    waves_with_second=second.get(number, 0),
                )
            )
    return out


def _first_seconds(pattern: dict, position: int) -> float:
    """When the wave that first used this position began, in that kill."""
    for row in pattern.get("waves") or []:
        if position in (row.get("order") or []):
            return float(row.get("startSeconds") or 0.0)
    return 0.0


def pool_spots(
    sightings: Sequence[Sighting],
) -> tuple[list[list[int]], addspawns.Break | None, float | None]:
    """`(spots as index lists, the break that separated them, the closest two came)`.

    The same single-linkage fold ``addspawns.cluster_positions`` runs, one level up
    and over a different population: there it groups a kill's *copies* into that
    kill's places, here it groups every kill's *places* into the encounter's places.

    Sharing the code was considered and not done. The two answer different questions
    over differently-shaped inputs -- a copy is one observation and a cluster centre
    is already an average of several -- and the honest consequence of sharing would
    be one function whose docstring had to describe two populations. What IS shared
    is ``find_break``, which is where the rule lives.

    A sample with **no hole** publishes no spots. That is the refusal that matters:
    with no separation every centroid becomes its own "place", and the map would then
    show one point per kill per cluster -- a plausible picture of nothing.
    """
    points = [(s.x, s.y) for s in sightings]
    if len(points) < 3:
        return [[i] for i in range(len(points))], None, None
    pairs = [
        math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(points) for b in points[i + 1 :]
    ]
    # Half the POINTS, as in `cluster_positions`, and for the same reason: the
    # premise is that kills agree about where the places are, so at least half the
    # sightings must sit inside one. Measured on MID2's ten kills the real break is
    # at rank 1,234 of 41,041 pairs with a floor of 143 -- and the floored and
    # unfloored answers agree, which is what a clean hole looks like.
    found = addspawns.find_break(pairs, min_support=len(points) // 2)
    if found is None:
        return [], None, None

    label = list(range(len(points)))
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if (
                math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
                < found.threshold
            ):
                old, new = label[j], label[i]
                label = [new if one == old else one for one in label]
    grouped: dict[int, list[int]] = {}
    for index, lab in enumerate(label):
        grouped.setdefault(lab, []).append(index)
    spots = sorted(grouped.values(), key=lambda g: min(sightings[i].first_seconds for i in g))

    closest = None
    for a_index, a in enumerate(spots):
        for b in spots[a_index + 1 :]:
            for i in a:
                for j in b:
                    d = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
                    closest = d if closest is None or d < closest else closest
    return spots, found, closest


def pool_areas(kills: Sequence[Kill], spot_of: dict[tuple[int, int], int]) -> dict[int, int]:
    """Pooled spot number -> area number, unioned over every kill's own areas.

    Per kill ``describe_pattern`` already groups waves into areas; those numbers are
    per kill and their ORDER varies -- on MID2 some kills visit the south-west area
    third and some second. So the pooled areas are unioned across kills through the
    spots they share, and numbered by the earliest wave that used them.
    """
    parent: dict[int, int] = {}

    def root(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    earliest: dict[int, float] = {}
    for index, kill in enumerate(kills):
        pattern = kill.pattern
        if not pattern:
            continue
        for block in pattern.get("areas") or []:
            members = sorted(
                {
                    spot_of[(index, position)]
                    for position in block.get("positions") or []
                    if (index, position) in spot_of
                }
            )
            if not members:
                continue
            for member in members[1:]:
                parent[root(member)] = root(members[0])
            start = min(
                (
                    float(row.get("startSeconds") or 0.0)
                    for row in pattern.get("waves") or []
                    if row.get("area") == block.get("area")
                ),
                default=0.0,
            )
            here = root(members[0])
            earliest[here] = min(earliest.get(here, start), start)

    order = sorted({root(node) for node in parent}, key=lambda r: earliest.get(r, 0.0))
    number = {component: index for index, component in enumerate(order, 1)}
    return {spot: number[root(spot)] for spot in parent}


def repeat_test(spots: Sequence[Spot]) -> RepeatTest | None:
    """Does any spot take a second copy more often than the others?

    Pooled over every spot's appearances. `None` when nothing was observed -- a rate
    over an empty denominator is the kind of zero this project refuses to print.
    """
    appearances = sum(s.waves_present for s in spots)
    seconds = sum(s.waves_with_second for s in spots)
    if appearances <= 0 or len(spots) < 2:
        return None
    rate = seconds / appearances
    if rate <= 0.0 or rate >= 1.0:
        # Every spot repeats, or none does. The chi-square is undefined and the
        # answer needs no test: it is in the counts.
        return RepeatTest(appearances, seconds, 0.0, len(spots) - 1, 0.0)
    chi = sum(
        (s.waves_with_second - s.waves_present * rate) ** 2 / (s.waves_present * rate * (1 - rate))
        for s in spots
        if s.waves_present > 0
    )
    df = len(spots) - 1
    # Wilson-Hilferty: turns a chi-square on any number of degrees of freedom into a
    # z, so the reading needs no table and the published number can be compared with
    # the 1.96 every other uncertainty statement in this project is stated against.
    z = ((chi / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df)) if df > 0 else 0.0
    return RepeatTest(appearances, seconds, chi, df, z)


def _spread(values: Iterable[float | int]) -> dict | None:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "median": round(median, 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "n": len(ordered),
    }


def per_wave_summary(kills: Sequence[Kill]) -> dict:
    """What a wave looks like, pooled: copies, distinct places, and the repeat tally.

    ``maxPerPosition`` is a **tally rather than a maximum**, because the question it
    answers -- can a place take a third copy -- is answered wrongly by either extreme.
    The maximum over ten kills says "yes, three" and hides that it happened three
    times in fifty-three waves; a modal value says "two" and hides that it ever
    happened at all.
    """
    copies: list[int] = []
    distinct: list[int] = []
    before_repeat: list[int] = []
    tally: dict[int, int] = {}
    for kill in kills:
        for row in (kill.pattern or {}).get("waves") or []:
            copies.append(int(row.get("sightings") or 0))
            distinct.append(int(row.get("distinctPositions") or 0))
            before_repeat.append(int(row.get("distinctBeforeFirstRepeat") or 0))
            most = int(row.get("maxPerPosition") or 0)
            tally[most] = tally.get(most, 0) + 1
    return {
        "waves": len(copies),
        "copies": _spread(copies),
        "distinctPositions": _spread(distinct),
        "distinctBeforeFirstRepeat": _spread(before_repeat),
        "maxPerPosition": {str(k): tally[k] for k in sorted(tally)},
    }


def build_encounter(payload: dict) -> EncounterMap:
    """One spawn-probe payload, folded into everything published about that boss.

    A payload that does not state its **difficulty** is refused rather than assumed:
    a Heroic map published under a Mythic heading is the mislabelling this project
    already refuses one document across, in `fights.json`'s `measuredDifficulty`.
    """
    npc = payload.get("npc")
    used = payload.get("usedEncounter")
    difficulty = payload.get("difficulty")
    if not isinstance(npc, int) or not isinstance(used, int):
        raise ValueError("payload states no npc or no encounter; nothing can be published")
    filed = payload.get("requestedEncounter")
    result = EncounterMap(
        encounter_id=used,
        filed_as=filed if isinstance(filed, int) and filed != used else None,
        npc_game_id=npc,
        difficulty=difficulty if isinstance(difficulty, int) else -1,
    )
    if not isinstance(difficulty, int):
        result.refusal = (
            "the payload states no difficulty, so nothing can be published: a map is "
            "about one difficulty and assuming Mythic is how a Heroic reading gets a "
            "Mythic heading"
        )
        return result

    result.kills = [read_kill(f, npc) for f in payload.get("fights") or []]

    # One difficulty per map, and a foreign one is a REFUSAL rather than a filter.
    # The rankings query and the fight fetch are both already scoped, so a fight
    # arriving at another difficulty means the scoping did not hold -- and dropping
    # it quietly would hide that while still publishing a plausible map. A fight
    # stating NO difficulty is allowed through: unknown is not the same as wrong,
    # which is `harvest`'s and `firstkills`'s three-way rule.
    foreign = sorted(
        {
            k.difficulty
            for k in result.kills
            if k.difficulty is not None and k.difficulty != difficulty
        }
    )
    if foreign:
        result.refusal = (
            f"this run asked for difficulty {difficulty} and {len(foreign)} other "
            f"difficulty/difficulties came back ({', '.join(str(d) for d in foreign)}): "
            "the fetch is already scoped, so that means the scoping did not hold, and "
            "a map pooled over two difficulties is two encounters drawn as one"
        )
        return result

    described = [k for k in result.kills if k.pattern]
    if len(described) < MIN_KILLS_FOR_A_MAP:
        result.refusal = (
            f"{len(described)} kill(s) yielded a pattern, and pooling needs "
            f"{MIN_KILLS_FOR_A_MAP}: one kill's clusters are already what "
            "`wowdps spawn-probe` prints, and re-publishing them as 'the encounter's "
            "places' would promise a repetition nothing observed"
        )
        return result

    sightings = sightings_of_spots(result.kills)
    groups, found, closest = pool_spots(sightings)
    if found is None or not groups:
        result.refusal = (
            "these kills' clusters do not fall into shared places -- the pooled "
            "distances carry no hole -- so every kill's reading stands on its own and "
            "no map is published"
        )
        return result

    result.tolerance = {
        "below": round(found.below, 1),
        "above": round(found.above, 1),
        "ratio": round(found.ratio, 2),
        "threshold": round(found.threshold, 1),
        "closestTwoSpotsCame": None if closest is None else round(closest, 1),
    }

    spot_of: dict[tuple[int, int], int] = {}
    provisional: list[dict] = []
    for number, members in enumerate(groups, 1):
        rows = [sightings[i] for i in members]
        x, y = _centroid([(r.x, r.y) for r in rows])
        provisional.append(
            {
                "spot": number,
                "x": x,
                "y": y,
                "kills": len({r.kill for r in rows}),
                "spread": max(
                    _widest([(r.x, r.y) for r in rows]),
                    max((r.spread for r in rows), default=0.0),
                ),
                "first": min(r.first_seconds for r in rows),
                "present": sum(r.waves_present for r in rows),
                "second": sum(r.waves_with_second for r in rows),
            }
        )
    # The per-kill position numbers have to be joined back to the pooled spot, which
    # is what lets the areas be unioned. Rebuilding it from the same order the
    # sightings were flattened in is exact; matching on coordinates would not be.
    flat_index = 0
    for index, kill in enumerate(result.kills):
        for entry in (kill.pattern or {}).get("positions") or []:
            for number, members in enumerate(groups, 1):
                if flat_index in members:
                    spot_of[(index, entry["position"])] = number
                    break
            flat_index += 1

    areas = pool_areas(result.kills, spot_of)
    result.spots = [
        Spot(
            spot=row["spot"],
            area=areas.get(row["spot"], 0),
            x=row["x"],
            y=row["y"],
            seen_in_kills=row["kills"],
            spread=row["spread"],
            first_seconds=row["first"],
            waves_present=row["present"],
            waves_with_second=row["second"],
        )
        for row in provisional
    ]
    result.spots.sort(key=lambda s: (s.area, s.first_seconds, s.spot))

    by_area: dict[int, list[Spot]] = {}
    for spot in result.spots:
        by_area.setdefault(spot.area, []).append(spot)
    result.areas = [
        {
            "area": area,
            "spots": [s.spot for s in members],
            "x": round(sum(s.x for s in members) / len(members), 1),
            "y": round(sum(s.y for s in members) / len(members), 1),
        }
        for area, members in sorted(by_area.items())
    ]

    result.repeat = repeat_test(result.spots)
    result.per_wave = per_wave_summary(result.kills)
    return result


def caveats(result: EncounterMap) -> list[str]:
    """Everything a reader has to weigh the map against, as sentences.

    They are computed from the same numbers the document publishes rather than
    written down, so a caveat cannot outlive the condition that produced it.
    """
    out: list[str] = []
    described = [k for k in result.kills if k.pattern]
    out.append(
        "A spot is where a copy was FIRST DAMAGED, not where it spawned: Warcraft "
        "Logs has no spawn event. Each spot's spread is that slack, measured."
    )
    if result.spots:
        widest = max(s.spread for s in result.spots)
        closest = (result.tolerance or {}).get("closestTwoSpotsCame")
        if closest:
            out.append(
                f"The widest spot spreads {widest:.0f} units where the nearest two spots "
                f"are {closest:.0f} apart, so the places separate by a factor of "
                f"{closest / widest:.1f}."
            )
    truncated = [k for k in described if k.truncated]
    if truncated:
        out.append(
            f"{len(truncated)} of {len(described)} kills had an event fetch that stopped "
            "at its page limit, so a wave read short is a copy missed rather than a "
            "place that does not exist."
        )
    if result.spots:
        thin = [s for s in result.spots if s.seen_in_kills < len(described)]
        if thin:
            out.append(
                f"{len(thin)} of {len(result.spots)} spots were not seen in every kill "
                f"(lowest {min(s.seen_in_kills for s in thin)} of {len(described)})."
            )
    tally = (result.per_wave or {}).get("maxPerPosition") or {}
    over_two = {k: v for k, v in tally.items() if int(k) > 2}
    if over_two:
        total = sum(tally.values())
        count = sum(over_two.values())
        out.append(
            f"A spot took more than two copies in {count} of {total} waves. Those are "
            "not clear of the resolution: the spots involved are among the widest, so "
            "two adjacent places the pooling merged would look the same."
        )
    out.append(
        "The order within a wave is FIRST-DAMAGED order, not spawn order, so "
        "'ten distinct places, then the repeats' is an observation about how the raid "
        "picked the copies up and not about how the encounter placed them."
    )
    return out


def to_json(result: EncounterMap, *, name: str | None = None, npc_name: str | None = None) -> dict:
    """The published block for one encounter."""
    described = [k for k in result.kills if k.pattern]
    block: dict[str, Any] = {
        "encounterId": result.encounter_id,
        "name": name,
        "difficulty": result.difficulty,
        "npc": {"gameId": result.npc_game_id, "name": npc_name},
        "killsRead": len(result.kills),
        "killsDescribed": len(described),
        "killsTruncated": sum(1 for k in described if k.truncated),
        "sampledFrom": [
            {"reportCode": k.report_code, "fightId": k.fight_id, "truncated": k.truncated}
            for k in result.kills
        ],
    }
    if result.filed_as is not None:
        block["filedAs"] = result.filed_as
    if result.refusal:
        block["refusal"] = result.refusal
        block["spots"] = []
        block["areas"] = []
        return block
    block["tolerance"] = result.tolerance
    block["areas"] = result.areas
    block["spots"] = [s.to_json() for s in result.spots]
    block["perWave"] = result.per_wave
    block["repeat"] = result.repeat.to_json() if result.repeat else None
    block["caveats"] = caveats(result)
    return block


def _dates(kills: Sequence[Kill]) -> dict | None:
    """When the sampled kills happened, or nothing when none of them says.

    Published for the reason `fightprobe.killed_between` publishes it: a sample is
    "the kills we saw", and without their dates a reader cannot tell a week of
    progression from one raid night. A payload written before the probe recorded
    `startedAt` states none, and that is `None` rather than a span around the epoch.
    """
    stamps = sorted(k.started_at for k in kills if k.started_at is not None)
    if not stamps:
        return None
    return {
        "first": stamps[0],
        "last": stamps[-1],
        "spanDays": round((stamps[-1] - stamps[0]) / 86_400_000, 2),
        "stamped": len(stamps),
        "of": len(kills),
    }


def encounter_block(payload: dict, *, name: str | None = None) -> dict:
    """One probe payload -> one published encounter block."""
    result = build_encounter(payload)
    block = to_json(result, name=name, npc_name=payload.get("npcName"))
    by_key = {(k.report_code, k.fight_id): k for k in result.kills}
    for row in block.get("sampledFrom") or []:
        kill = by_key.get((row.get("reportCode"), row.get("fightId")))
        row["startedAt"] = kill.started_at if kill else None
    block["killedBetween"] = _dates(result.kills)
    return block


def merge_documents(documents: Sequence[dict]) -> dict:
    """Fold spawn documents oldest-first; a later one replaces only what it covers.

    Union semantics on `(encounterId, difficulty, npc.gameId)`, and the published
    document joins the merge as the OLDEST -- the rule `merge_gear_shards` arrived at
    after a single-slot run published an empty array over the two slots it had not
    swept. A spawn run is per encounter and per npc by construction, so a document
    that replaced its input wholesale would delete every boss the run did not read,
    and the deletion would look exactly like a boss nobody has probed.

    A block's key includes the DIFFICULTY, so a Heroic pass sits beside a Mythic one
    rather than over it. That is `fights.json`'s `measurements[]` rule; pooling them
    would be the mislabelling `build_encounter` already refuses one layer down.
    """
    merged: dict[tuple[Any, Any, Any], dict] = {}
    for document in documents:
        for block in document.get("encounters") or []:
            npc = (block.get("npc") or {}).get("gameId")
            merged[(block.get("encounterId"), block.get("difficulty"), npc)] = block
    encounters = sorted(
        merged.values(),
        key=lambda b: (
            b.get("encounterId") or 0,
            b.get("difficulty") or 0,
            (b.get("npc") or {}).get("gameId") or 0,
        ),
    )
    return {"encounters": encounters}


def measurement_block(payload: dict) -> dict:
    """The run's own provenance, lifted off the payload rather than recomputed.

    `cost` is carried even though it is a stamp: what a pass costs is the open
    question behind every budget decision in this repository, and this is the only
    measurement of it. It is excluded from the settle instead -- see
    `_PROVENANCE_PATHS`.
    """
    return {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "difficulty": payload.get("difficulty"),
        "requestedEncounter": payload.get("requestedEncounter"),
        "usedEncounter": payload.get("usedEncounter"),
        "idChoice": payload.get("idChoice"),
        "streams": sorted(
            {
                str(stream.get("dataType"))
                for fight in payload.get("fights") or []
                for stream in fight.get("streams") or []
                if stream.get("dataType")
            }
        ),
        "stoppedBy": payload.get("stoppedBy"),
        "cost": payload.get("cost"),
    }


def document(
    blocks: Sequence[dict],
    *,
    tier: str,
    published: Sequence[dict] = (),
    measurement: dict | None = None,
) -> dict:
    """The whole `<tier>/spawns.json`, this run's blocks folded over the published one."""
    out = merge_documents([*published, {"encounters": list(blocks)}])
    out["schemaVersion"] = SPAWNS_SCHEMA_VERSION
    out["generatedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
    out["tier"] = tier
    # `None` rather than absent when this run built no block of its own: a document
    # that is only a carried-forward merge has no measurement to describe, and an
    # invented one would claim this run read what an earlier one paid for.
    out["measurement"] = measurement
    out["coverage"] = {
        "encounters": len({b.get("encounterId") for b in out["encounters"]}),
        "blocks": len(out["encounters"]),
        # Named rather than counted, the `staleRows` rule: a block that published no
        # spots is in the document and a count of blocks cannot say which one, so a
        # reader who wants to go and look is told where.
        "withoutSpots": [
            {
                "encounterId": b.get("encounterId"),
                "difficulty": b.get("difficulty"),
                "npc": (b.get("npc") or {}).get("gameId"),
                "why": b.get("refusal") or "no refusal stated",
            }
            for b in out["encounters"]
            if not b.get("spots")
        ],
    }
    return out


def _without_stamps(doc: dict) -> dict:
    """A copy with every provenance stamp removed, for comparison only."""
    stripped = json.loads(json.dumps(doc))
    for path in _PROVENANCE_PATHS:
        node = stripped
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return stripped


def _carry_stamps(doc: dict, published: dict) -> dict:
    """The new document wearing the published run's stamps."""
    settled = json.loads(json.dumps(doc))
    for path in _PROVENANCE_PATHS:
        source, target = published, settled
        for key in path[:-1]:
            source = source.get(key) if isinstance(source, dict) else None
            target = target.get(key) if isinstance(target, dict) else None
            if source is None or target is None:
                break
        if isinstance(source, dict) and isinstance(target, dict) and path[-1] in source:
            target[path[-1]] = source[path[-1]]
    return settled


class SpotsWouldBeLost(RuntimeError):
    """Refusal: this write would replace published spots with none."""


def write_spawns(out_dir: Path, doc: dict, *, force: bool = False) -> Path:
    """Write `<out_dir>/spawns.json`, refusing a write that loses spots and settling.

    Three things in one order, and the order is `write_fights`'s because getting it
    wrong there cost months of restamped manifests: **read the published file, refuse
    a loss, settle last.**

    The refusal is whole-document (`coverage.blocks with spots` going from N to 0) and
    the per-block half is `merge_documents`, which is why this function never has to
    carry a block forward itself.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "spawns.json"

    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        published = None

    def with_spots(one: dict | None) -> int:
        return sum(1 for b in (one or {}).get("encounters") or [] if b.get("spots"))

    if published is not None and not force:
        had, has = with_spots(published), with_spots(doc)
        if had and not has:
            raise SpotsWouldBeLost(
                f"{path} carries spots for {had} encounter(s) and this document has "
                "none, so writing it would discard them. Pass the payload that "
                "measured them, or --force if dropping them is what you mean."
            )

    settled = doc
    if published is not None and _without_stamps(published) == _without_stamps(doc):
        settled = _carry_stamps(doc, published)

    path.write_text(json.dumps(settled, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def publish(
    out_dir: Path,
    blocks: Sequence[dict],
    *,
    tier: str,
    measurement: dict | None = None,
) -> dict:
    """This run's blocks folded over whatever `<out_dir>/spawns.json` already holds.

    Reading the published file HERE rather than in the writer is what makes the
    settle able to fire: the merge has to be part of the document the comparison
    sees, or a run that changed nothing still writes a new timestamp -- the exact
    ordering defect `dataset.publish_manifest` exists to make unrepeatable.
    """
    published: list[dict] = []
    path = out_dir / "spawns.json"
    try:
        published.append(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return document(blocks, tier=tier, published=published, measurement=measurement)
