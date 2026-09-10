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

#: How much wider an area's widest empty wedge must be than its next widest gap
#: before the places on that arc are numbered at all.
#:
#: **Calibrated against a null rather than fitted to this encounter.** Ten points
#: scattered with no arc still produce a widest gap, so the question is how big a
#: ratio chance supplies. Measured over 120,000 trials across four blob families --
#: uniform disc, annulus, gaussian, and this encounter's own radii with the angles
#: randomised -- taking angles about each sample's own mean, which is what the rule
#: does: the 5%-false-positive floor at ten points is 1.766 / 1.803 / 1.818 /
#: 1.794-1.805 and the median ratio is 1.21. MID2's three areas measure 2.394,
#: 2.116 and 2.160, i.e. p = 0.0017 to 0.0124. The gate does not loosen as a ring
#: thins: P(>= 1.80) is 0.007 at four points, 0.034 at eight, 0.043 at ten.
#:
#: ``addspawns.find_break``'s 3.0 is NOT the same number and cannot be borrowed --
#: that one separates two populations of distances, this one compares two gaps in
#: one ring, and applied here it would refuse all three of MID2's areas.
MIN_WEDGE_DOMINANCE = 1.80

#: How far the areas' rings may disagree, in degrees, before place N of one area
#: stops being the same position as place N of another -- and with it, before the
#: pooled view may add their counts together.
#:
#: Per place, the angle from that area's own place 1; the disagreement is the spread
#: across the areas, averaged over the places. MID2 measures **6.2 degrees** (per
#: place 0.0, 1.6, 10.8, 9.3, 8.7, 7.2, 4.0, 5.6, 8.5, 6.3). The null -- three areas
#: at the observed radii with random angles, each anchored by the same wedge rule,
#: 20,000 trials -- has a MINIMUM of 13.69 degrees and a median of 35.07. So the
#: floor sits in a band with nothing in it: 1.6x above the observation and 1.4x
#: below the best of twenty thousand rings that are not the same ring.
MAX_PLACE_DISAGREEMENT_DEGREES = 10.0

#: Places are read off in pairs, the way the owner reads them: (1,2) is group 1,
#: (3,4) group 2, and so on. A property of the numbering rather than a measurement.
PLACES_PER_GROUP = 2


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
    #: Where this spot sits on its area's arc, 1..n clockwise from the widest empty
    #: wedge -- see ``place_ring``, including why the direction is a convention.
    #: ``None`` where the area's ring has no dominant wedge to start from, which is
    #: a refusal rather than a gap: unnumbered is not place zero.
    place: int | None
    #: The pair the place falls in, (1,2) -> 1. ``None`` whenever ``place`` is.
    group: int | None
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
            "place": self.place,
            "group": self.group,
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
    #: What the place numbering measured: how many areas it numbered, how dominant
    #: their wedges were, and how far the rings disagree. Empty until
    #: ``build_encounter`` has run.
    places: dict = field(default_factory=dict)
    per_wave: dict = field(default_factory=dict)
    tolerance: dict | None = None
    refusal: str | None = None


def _centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """The mean of some points, summed EXACTLY.

    ``math.fsum`` rather than ``sum`` throughout this module, and it is not a
    micro-optimisation -- it is what makes the published document the same document
    on every interpreter. ``sum`` over floats accumulates rounding error, and CPython
    changed how much: 3.12 sums with Neumaier compensation where 3.11 does not.

    Measured on this encounter, republishing the committed payload: spot 15's x is
    **6649.5 on Python 3.11 and 6649.6 on 3.12**, from the same six numbers. CI runs
    3.12 (`spawn-probe.yml`) so the committed value is the 3.12 one, and a maintainer
    republishing on 3.11 would produce a one-digit diff that reads as the encounter
    having moved. Five of the 60 published spot coordinates sit on an exact decimal
    tie and so could flip that way; one of them does today.

    ``fsum`` is exact, so it agrees with itself on every interpreter and on neither
    side of that split by accident.
    """
    return (
        math.fsum(p[0] for p in points) / len(points),
        math.fsum(p[1] for p in points) / len(points),
    )


def _widest(points: Sequence[tuple[float, float]]) -> float:
    return max(
        (math.hypot(a[0] - b[0], a[1] - b[1]) for a in points for b in points),
        default=0.0,
    )


def place_ring(
    members: Sequence[tuple[int, float, float]],
    centre: tuple[float, float],
) -> dict[int, int] | None:
    """``{spot number -> place 1..n}`` around one area, or ``None`` when it refuses.

    An area's places sit on an ARC rather than a closed ring: MID2's three areas
    each hold ten places spanning about 230 degrees with one empty wedge of 126 to
    132 degrees, where the next widest gap is 52 to 61. An arc has an end, and
    numbering from that end is the whole point -- it is what makes place 3 of one
    area the same question as place 3 of another, which a per-area spot number
    (3, 15, 26) cannot be.

    The rule is: sort the members by angle around ``centre`` and start at the first
    one after the widest empty wedge, going CLOCKWISE. It reproduces the owner's
    hand-written numbering for MID2's Twin Fangs **30 of 30**, all three areas, no
    exceptions -- which is the only reason this is a derivation rather than his
    table typed into a file.

    **Clockwise is a CONVENTION and not a measurement.** Both directions are
    equally derivable from the geometry and nothing in the data prefers one; the
    axes' own orientation is not established either (see ``dps-spawn-map.ts``).
    ``first_seconds`` cannot break the tie: it is a ``min`` over the area's
    sightings, so every place in an area carries the same value -- 36.0, 191.1,
    191.1 on MID2 -- and orders nothing. So the direction is picked to match the
    owner's reading, and the published block says so rather than letting the
    numbering be taken for a claim about how the encounter places its copies.

    **The refusal.** An arc has an end only while one wedge dominates. Measured on
    MID2 the widest beats the second widest by 2.39 / 2.12 / 2.16; on a ring with
    no dominant gap that ratio approaches 1.0 and the start -- with it every number
    in the area -- moves between runs on noise. Below ``MIN_WEDGE_DOMINANCE``
    nothing is published for that area, because ``None`` is not ``1``.

    Fewer than three members also refuses: two points have one gap either way round
    and no wedge to be widest.
    """
    if len(members) < 3:
        return None
    by_angle = sorted(
        (math.degrees(math.atan2(y - centre[1], x - centre[0])) % 360.0, spot)
        for spot, x, y in members
    )
    count = len(by_angle)
    gaps = [((by_angle[(i + 1) % count][0] - by_angle[i][0]) % 360.0, i) for i in range(count)]
    dominance = wedge_dominance(members, centre)
    if dominance is None or dominance < MIN_WEDGE_DOMINANCE:
        return None
    # The widest gap runs from `at` counter-clockwise to `at + 1`, so the arc's two
    # ends are those two members. Going clockwise means starting at `at` and
    # stepping DOWN the sorted angles.
    _, at = max(gaps)
    return {by_angle[(at - step) % count][1]: step + 1 for step in range(count)}


def wedge_dominance(
    members: Sequence[tuple[int, float, float]],
    centre: tuple[float, float],
) -> float | None:
    """How much wider the widest gap in this ring is than the next widest.

    The number ``place_ring`` gates on, exposed so the document can publish what it
    measured rather than only whether it passed. ``None`` under three members, where
    there is no second gap to compare against.
    """
    if len(members) < 3:
        return None
    by_angle = sorted(
        math.degrees(math.atan2(y - centre[1], x - centre[0])) % 360.0 for _, x, y in members
    )
    count = len(by_angle)
    gaps = sorted((by_angle[(i + 1) % count] - by_angle[i]) % 360.0 for i in range(count))
    return None if gaps[-2] <= 0 else gaps[-1] / gaps[-2]


def place_group(place: int) -> int:
    """Which pair a place belongs to: (1,2) -> 1, (3,4) -> 2, and so on."""
    return (place - 1) // PLACES_PER_GROUP + 1


def place_disagreement(rings: Sequence[dict[int, float]]) -> float | None:
    """Mean spread, in degrees, of where place N sits across the areas.

    Each ring is ``{place -> angle about that area's own place 1}``. Place N of one
    area is only the same position as place N of another while those angles agree,
    and this is the number that says whether they do. ``None`` for fewer than two
    rings -- one area cannot disagree with itself, and reporting 0.0 would read as
    perfect agreement measured.

    Only places every ring holds are compared. A place one ring lacks says nothing
    about whether the rings line up, and pooling over a partial set would make the
    disagreement look smaller the more incomplete the data got.
    """
    if len(rings) < 2:
        return None
    shared = set(rings[0])
    for ring in rings[1:]:
        shared &= set(ring)
    if not shared:
        return None
    spreads = []
    for place in shared:
        angles = [ring[place] for ring in rings]
        spreads.append(max(angles) - min(angles))
    return math.fsum(spreads) / len(spreads)


def ring_angles(
    members: Sequence[tuple[int, float, float]],
    centre: tuple[float, float],
    ring: dict[int, int],
) -> dict[int, float]:
    """``{place -> angle in degrees}``, measured from that area's own place 1.

    Signed into (-180, 180] so two areas whose place 1 points different ways in the
    room are still comparable: the shape is what is being compared, not the bearing.
    """
    by_spot = {spot: (x, y) for spot, x, y in members}
    anchor = next(spot for spot, place in ring.items() if place == 1)
    ax, ay = by_spot[anchor]
    base = math.atan2(ay - centre[1], ax - centre[0])
    out: dict[int, float] = {}
    for spot, place in ring.items():
        x, y = by_spot[spot]
        turn = math.degrees(math.atan2(y - centre[1], x - centre[0]) - base)
        out[place] = (turn + 180.0) % 360.0 - 180.0
    return out


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
    # `fsum` for the same reason as `_centroid`: `chiSquare` and `z` are published,
    # and 30 float terms is where the two interpreters' `sum` can disagree. Measured
    # bit-identical on 3.11 and 3.12 for THIS data (31.805618539955724), so the fix
    # is against the hazard rather than against an observed flip.
    chi = math.fsum(
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

    # The place index is a property of the AREA's ring, so it needs every member of
    # that area and the area's own centre -- which is why it is derived here, after
    # `pool_areas`, rather than inside the loop that builds the rows.
    by_area_rows: dict[int, list[dict]] = {}
    for row in provisional:
        by_area_rows.setdefault(areas.get(row["spot"], 0), []).append(row)

    numbered: dict[int, dict[int, int]] = {}
    angles: dict[int, dict[int, float]] = {}
    dominance: dict[int, float] = {}
    # Named, never counted. `areasRefused` was a number, and a number says an area was
    # refused without saying WHICH -- the same trade `withoutSpots` two functions down
    # already refuses, and `gear.json`'s `staleRows` refuses one document across. The
    # two reasons are also different findings: a ring with no dominant wedge has no
    # start at all, a short ring has one and would number 1..9 off by one after the
    # gap. Collapsed into a count, a reader cannot tell them apart or go and look.
    refused: list[dict] = []
    for area, rows in sorted(by_area_rows.items()):
        members = [(r["spot"], r["x"], r["y"]) for r in rows]
        centre = _centroid([(r["x"], r["y"]) for r in rows])
        ring = place_ring(members, centre)
        if ring is None:
            measured = wedge_dominance(members, centre)
            refused.append(
                {
                    "area": area,
                    "places": len(rows),
                    "why": (
                        f"no dominant empty wedge to start from: widest gap is "
                        f"{measured:.2f}x the next widest against a floor of "
                        f"{MIN_WEDGE_DOMINANCE}"
                        if measured is not None
                        else "no dominant empty wedge to start from, and none could be measured"
                    ),
                }
            )
            continue
        numbered[area] = ring
        angles[area] = ring_angles(members, centre, ring)
        dominance[area] = wedge_dominance(members, centre) or 0.0

    # **An area is numbered only while it holds as many places as the fullest ring.**
    # A short area's own wedge still gives it an order, and numbering it 1..9 would
    # silently shift every place after the missing one -- so place 6 of that area
    # would be drawn beside place 6 of another and be a different position. The
    # measured alternative is to MATCH the short ring onto the reference by a rigid
    # fit, which leaves a hole instead of a shift and is right about 84% of the time
    # at six of ten places; it is not built, because no area of this encounter is
    # short and a matcher nothing exercises is a guess with the authority of code.
    # Refusing is the honest half of it, and it is the half that cannot mislabel.
    widest = max((len(ring) for ring in numbered.values()), default=0)
    refused.extend(
        {
            "area": area,
            "places": len(ring),
            "why": (
                f"holds {len(ring)} place(s) where the fullest area holds {widest}, so "
                f"numbering it would shift every place after the missing one"
            ),
        }
        for area, ring in sorted(numbered.items())
        if len(ring) != widest
    )
    numbered = {area: ring for area, ring in numbered.items() if len(ring) == widest}

    place_of: dict[int, int] = {}
    for ring in numbered.values():
        place_of.update(ring)
    disagreement = place_disagreement([angles[area] for area in sorted(numbered)])
    result.places = {
        "areasNumbered": len(numbered),
        "areasRefused": len(by_area_rows) - len(numbered),
        # The same number as `areasRefused`, said in a way a reader can act on. Kept
        # BESIDE the count rather than instead of it: the count is what a caption
        # prints and the list is what somebody goes and looks at.
        "refused": sorted(refused, key=lambda entry: entry["area"]),
        "perArea": widest if numbered else 0,
        "wedgeDominance": (
            {
                "min": round(min(dominance[a] for a in numbered), 2),
                "max": round(max(dominance[a] for a in numbered), 2),
                "floor": MIN_WEDGE_DOMINANCE,
            }
            if numbered
            else None
        ),
        "disagreementDegrees": None if disagreement is None else round(disagreement, 2),
        "maxDisagreementDegrees": MAX_PLACE_DISAGREEMENT_DEGREES,
        # A boolean beside its own number, the way `RepeatTest.separates` is: what it
        # licenses is ADDING the areas' counts together, which is the one thing a
        # pooled view does that a per-area view does not.
        "poolable": (disagreement is not None and disagreement <= MAX_PLACE_DISAGREEMENT_DEGREES),
    }

    result.spots = [
        Spot(
            spot=row["spot"],
            area=areas.get(row["spot"], 0),
            place=place_of.get(row["spot"]),
            group=(place_group(place_of[row["spot"]]) if row["spot"] in place_of else None),
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
    # Place order within an area, which is the order the owner reads them in and the
    # order the table twin shows. `first_seconds` stays in the key as the fallback
    # for an area that refused a ring -- it is what ordered these rows before places
    # existed, and it is a `min` over the area's sightings, so inside one area it is
    # constant and orders nothing on its own.
    result.spots.sort(
        key=lambda s: (s.area, 999 if s.place is None else s.place, s.first_seconds, s.spot)
    )

    by_area: dict[int, list[Spot]] = {}
    for spot in result.spots:
        by_area.setdefault(spot.area, []).append(spot)
    result.areas = [
        {
            "area": area,
            "spots": [s.spot for s in members],
            # `fsum`, for `_centroid`'s reason -- an area's centre is published.
            "x": round(math.fsum(s.x for s in members) / len(members), 1),
            "y": round(math.fsum(s.y for s in members) / len(members), 1),
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
    numbered = [s for s in result.spots if s.place is not None]
    if numbered:
        areas_numbered = len({s.area for s in numbered})
        out.append(
            f"Places are numbered 1-{max(s.place for s in numbered)} within each of "
            f"{areas_numbered} area(s), clockwise from the widest empty wedge in that "
            "area's ring, so place N of one area is the same position on the arc as "
            "place N of another. CLOCKWISE IS A CONVENTION: both directions fit the "
            "geometry equally and nothing measured here prefers one."
        )
    unnumbered = {s.area for s in result.spots if s.place is None}
    if unnumbered and result.spots:
        out.append(
            f"{len(unnumbered)} area(s) got no place numbers: their ring has no wedge "
            "clearly wider than the next, so the arc has no end to count from and any "
            "numbering would move between runs."
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
    block["places"] = result.places or None
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


def pooled_measurement(payloads: Sequence[dict]) -> dict | None:
    """The measurement block for a publish that read one payload, or several.

    **One payload is the ordinary case and produces exactly what
    ``measurement_block`` produces**, so nothing published moves.

    Several is where this exists. `cmd_spawn_map` takes `--payload` repeatedly and
    built the block from `payload` -- the *loop variable*, i.e. whichever file was
    read last -- so a two-boss publish stamped the whole document with one run's
    difficulty, encounter ids, `stoppedBy` and cost. Every field would be real, and
    all but one boss's worth of them would be about the wrong pass: the shape
    `gear.json` shipped for weeks with one provenance block over three slots.

    The honest pooled answer is to drop what belongs to a single run rather than
    pick one. `difficulty`, `requestedEncounter` and `usedEncounter` are on each
    ENCOUNTER block already, so nothing is lost that a reader cannot reach; `cost`
    genuinely cannot be pooled, because two payloads are two ledgers and neither is
    the document's. `streams` is a union, which is what was read.
    """
    if not payloads:
        return None
    if len(payloads) == 1:
        return measurement_block(payloads[0])
    return {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "payloads": len(payloads),
        "difficulty": None,
        "requestedEncounter": None,
        "usedEncounter": None,
        "idChoice": None,
        "streams": sorted(
            {
                str(stream.get("dataType"))
                for payload in payloads
                for fight in payload.get("fights") or []
                for stream in fight.get("streams") or []
                if stream.get("dataType")
            }
        ),
        "stoppedBy": None,
        "cost": None,
        "note": (
            "This document pooled several payloads, so the fields that describe one "
            "run are absent rather than taken from one of them. Each encounter block "
            "carries its own difficulty and encounter ids; the point cost of each "
            "pass is in that pass's own artifact."
        ),
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
