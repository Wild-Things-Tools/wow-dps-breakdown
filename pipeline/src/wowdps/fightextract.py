"""Reading fight structure out of Warcraft Logs report payloads.

Why the site needs this at all
------------------------------
The logs cross-check currently compares a **Patchwerk single-target** sim against
real Mythic kills, and the residual is dominated by the boss rather than by the
spec. Measured over the 192 published comparisons, the median logs/sim ratio runs
from 0.57 on Belo'ren to 0.86 on Lightblinded Vanguard, *the same ordering for
every one of the 26 specs*. That is not 26 specs being mismodelled; that is one
encounter shape being compared against the wrong simulation. Six comparisons have
the logs winning outright, all of them Mages, almost all on Lightblinded Vanguard --
exactly what you would expect if that fight has permanent extra targets a
single-target sim does not have.

So the comparison needs a per-boss fight shape: how many things are alive when, how
long adds last, where the phases are, when damage is amplified. This module is the
half of that which can be *measured*; ``fightprofile`` is the half that gets
asserted, and the two are kept apart deliberately.

Everything here is a pure function over a payload
-------------------------------------------------
The API credentials live as GitHub Actions secrets, so a development checkout
cannot call Warcraft Logs at all. Nothing in this module touches the network:
every function takes a decoded payload and returns observations, which is what
makes the assumptions testable offline against hand-written fixtures
(``tests/test_fightextract.py``).

What the v2 API does and does not carry, field by field
-------------------------------------------------------
*Extractable.* Fight start/end, ``size`` (the raid's group size) and
``friendlyPlayers``; ``phaseTransitions`` per fight with names from
``report.phases``; enemy death events; damage events per enemy actor *instance*;
aura application and removal on enemies; a damage table broken down by target.

*Extractable with caveats.* An enemy's lifespan. There is no spawn event in the
general case, so an add is first *seen* when it first takes damage -- late by
however long the raid took to reach it -- and an add that despawns rather than
dying has no death event, so it is only known to have stopped taking damage. Both
are recorded as such (``first_seen`` is named for what it is, and ``died`` says
whether the end is a death or a last hit) rather than being smoothed into a
spawn/despawn pair that the data does not contain.

*Not available.* What an aura *does*. The API gives an ability id, a name and a
window; that a given buff is worth +20% damage taken is not in it anywhere, and no
amount of event reading will produce it. Magnitudes are therefore asserted by hand
in ``fightprofile``, never inferred here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Aura events that start a window, and the ones that end it. ``refresh*`` is
#: deliberately absent: a refresh extends a window that is already open, and
#: treating it as a fresh application would report one 20-second window as four.
AURA_START_TYPES = frozenset({"applybuff", "applydebuff"})
AURA_END_TYPES = frozenset({"removebuff", "removedebuff"})

#: An enemy under this share of the fight's damage is present but not a target
#: anyone is fighting -- a totem, an untargetable prop, a stray pet. The timeline
#: is reported both ways, because "three things exist" and "three things matter"
#: are different claims and the second is the one a scenario should be built from.
DEFAULT_SIGNIFICANT_SHARE = 0.01

#: Below this share of a fight read by the event fetch, no statistic averaged over
#: the fight can be trusted, because the unread part is silently counted as empty.
#: 0.95 rather than 1.0: a kill's last enemy dies a fraction of a second before the
#: log closes, so a complete fetch still lands just short.
COMPLETE_EVENT_COVERAGE = 0.95

#: A fight counts as a constant-target fight when it spends at least this much of
#: its length at its peak count. Not 100%: on a kill the targets die one after
#: another over the last second or two, which would otherwise make every fight in
#: the game "variable" for a reason that has nothing to do with its shape.
CONSTANT_TARGET_SHARE = 0.95

#: How much more damage the leading enemy must take than the next one before the
#: damage stream is allowed to nominate it as the priority target. Below this the
#: fight is "several things being hit about equally" -- which is a real encounter
#: shape, not a measurement failure -- and the honest answer is that nothing in
#: the events nominates a boss. Deliberately not a tie-break: picking the larger
#: of two near-equal numbers would turn noise into an assertion about the fight.
PRIORITY_DAMAGE_MARGIN = 1.25

#: Roles an enemy can play relative to the simulation. Only ``priority`` has a
#: simc equivalent for an amplification window (``vulnerable`` without ``target=``
#: lands on ``sim->target``); ``add`` is carried so the gap is nameable.
ROLE_PRIORITY = "priority"
ROLE_ADD = "add"
ROLE_UNKNOWN = "unknown"
#: One aura that landed on a priority target in one pull and an add in another.
ROLE_MIXED = "mixed"


def _seconds(timestamp_ms: float, fight_start_ms: float) -> float:
    """Report-relative milliseconds to fight-relative seconds, rounded to 1ms."""
    return round((float(timestamp_ms) - float(fight_start_ms)) / 1000.0, 3)


@dataclass(frozen=True)
class EnemyLife:
    """One *instance* of one enemy NPC, and the window it was being fought in.

    ``first_seen`` is the first damage the instance took, not its spawn: the API
    has no general spawn event, so an add that stood around for ten seconds before
    anyone hit it is indistinguishable from one that spawned late. ``died`` says
    whether ``last_seen`` is a death event or merely the last hit it took.
    """

    actor_id: int
    instance: int
    name: str
    game_id: int | None
    first_seen: float
    last_seen: float
    died: bool
    damage: float

    @property
    def lifetime(self) -> float:
        return round(self.last_seen - self.first_seen, 3)


@dataclass(frozen=True)
class AuraWindow:
    """One application of one aura to one enemy instance.

    A window whose removal never arrives is closed at the end of the fight and
    flagged ``truncated`` -- which is the normal case for an aura the boss dies
    holding, and also what a paginated event fetch that ran out of pages looks
    like. The flag keeps those from being read as a measured duration.
    """

    ability_id: int
    ability_name: str
    actor_id: int
    instance: int
    start: float
    end: float
    truncated: bool
    #: Actor that applied it, when the event carried one. Needed to tell an aura
    #: the *encounter* puts on its own add from a debuff a player put there --
    #: both land on an enemy, and both arrive in the same event stream.
    source_id: int | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class PhaseWindow:
    id: int
    name: str
    is_intermission: bool
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class TargetCountTimeline:
    """How many enemies were being fought at once, as a step function.

    ``steps`` are ``(start second, count)`` pairs, each running until the next
    one. ``mean`` is time-weighted, which is the number a single ``desired_targets``
    would have to stand for; ``peak_share`` says whether that is even a fair
    summary, by reporting how much of the measured window was spent at the highest
    count.

    Why there are two lengths here, and why it matters
    -------------------------------------------------
    ``duration`` is how long the fight ran. ``observed`` is how much of it the
    event fetch actually reached. They are not the same thing, and treating them
    as the same produced a genuinely wrong number on the first real nine-boss
    pass: enemy damage-taken is paginated and bounded, a twenty-player Mythic pull
    generates events far faster than the budget allows, and the fetch stops
    part-way through. Every enemy's last hit is then the cut point, the step
    function falls to zero there, and a mean divided by the *whole* fight length
    reads as though nothing was alive for the rest of it.

    Measured on the MID2 pass: Midnight Falls was read for 11-22% of its length
    and reported a mean of 0.34 concurrent targets; Vorasius, a single-target
    boss, reported 0.59. A mean below one on a boss that is always there is not a
    property of the encounter, it is a division by the wrong denominator.

    So both statistics are computed over ``observed``, and ``coverage`` publishes
    the ratio so nothing downstream can mistake a fifth of a fight for all of it.
    A caller that wants a whole-fight claim has to check ``coverage`` first --
    ``plan_promotions`` does exactly that and refuses.
    """

    steps: tuple[tuple[float, int], ...]
    duration: float
    #: Seconds of the fight the events actually covered. ``None`` means "assume
    #: the whole fight", which is right for a timeline built from something other
    #: than a bounded event fetch.
    observed: float | None = None

    @property
    def window(self) -> float:
        """The length the statistics are averaged over: what was read, not what ran.

        ``None`` and ``0.0`` are opposite states and used to share a branch, which
        is the "absent is not zero" rule this project applies everywhere else,
        pointing the other way. ``None`` means *no bounded fetch was involved*, so
        the whole fight is the right window. ``0.0`` means *a bounded fetch read
        nothing*, and answering ``duration`` there makes ``coverage`` come out at
        **1.0** -- the maximum -- for the one case the field exists to catch.

        Measured in the committed MID2 data before the fix: The Lost Explorers at
        Mythic carries a pull with a single step at zero targets and
        ``coverage: 1.0``, beside eight real pulls at 0.9994-0.9999.
        """
        if self.observed is None:
            return self.duration
        if self.observed <= 0:
            return 0.0
        return min(self.observed, self.duration)

    @property
    def coverage(self) -> float:
        """Fraction of the fight the events reached.

        1.0 when nothing was cut, and **0.0 when nothing was read** -- see
        ``window``. Those two used to be the same number.
        """
        if self.duration <= 0:
            return 0.0
        return round(min(self.window / self.duration, 1.0), 4)

    def _spans(self):
        for index, (start, count) in enumerate(self.steps):
            end = self.steps[index + 1][0] if index + 1 < len(self.steps) else self.window
            yield count, max(0.0, min(end, self.window) - min(start, self.window))

    @property
    def mean(self) -> float:
        if not self.steps or self.window <= 0:
            return 0.0
        return round(sum(count * span for count, span in self._spans()) / self.window, 3)

    @property
    def peak(self) -> int:
        return max((count for _, count in self.steps), default=0)

    @property
    def peak_share(self) -> float:
        """Fraction of the measured window spent at the peak count."""
        if self.window <= 0:
            return 0.0
        at_peak = sum(span for count, span in self._spans() if count == self.peak)
        return round(at_peak / self.window, 4)

    @property
    def constant(self) -> bool:
        return self.peak_share >= CONSTANT_TARGET_SHARE

    def to_json(self) -> dict:
        return {
            "steps": [[start, count] for start, count in self.steps],
            "mean": self.mean,
            "peak": self.peak,
            "peakShare": self.peak_share,
            "constant": self.constant,
            # Published beside the numbers it qualifies, never in a footnote: a
            # mean over 20% of a fight and a mean over all of it are different
            # claims wearing the same name.
            "observed": round(self.window, 3),
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class AddPattern:
    """What one NPC did across the fight, in the shape a raid event is written in.

    ``cadence`` is the median gap between successive instances first taking
    damage, and is ``None`` for an NPC that only ever appeared once -- one
    appearance is not a rhythm, and a "cooldown" derived from it would be an
    invention.
    """

    name: str
    game_id: int | None
    instances: int
    first_seen: float
    lifetime: float
    cadence: float | None
    present_at_pull: bool
    damage_share: float

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "gameId": self.game_id,
            "instances": self.instances,
            "firstSeen": self.first_seen,
            "lifetime": self.lifetime,
            "cadence": self.cadence,
            "presentAtPull": self.present_at_pull,
            "damageShare": round(self.damage_share, 4),
        }


@dataclass(frozen=True)
class TargetDamage:
    name: str
    actor_id: int | None
    total: float
    share: float


@dataclass(frozen=True)
class PriorityNomination:
    """Which enemy a simulation's priority target would stand for, and on what grounds.

    There is no "is the boss" field anywhere in the API. What there is: the
    encounter's name, and how much damage each enemy took. So this nominates, it
    does not decide, and it carries the sentence a person would need in order to
    disagree with it.

    ``actor_id`` is ``None`` when nothing nominates one, which is the correct
    answer for a fight where three things are hit about equally. An encounter that
    genuinely has no single priority target is a fact about the encounter; guessing
    one there would invent the very thing this exists to establish.
    """

    actor_id: int | None
    name: str | None
    game_id: int | None
    evidence: str

    @property
    def known(self) -> bool:
        return self.actor_id is not None

    def role_of(self, actor_id: int) -> str:
        """``priority``/``add`` for an enemy, or ``unknown`` when nothing was nominated."""
        if not self.known:
            return ROLE_UNKNOWN
        return ROLE_PRIORITY if actor_id == self.actor_id else ROLE_ADD

    def to_json(self) -> dict:
        return {
            "actorId": self.actor_id,
            "name": self.name,
            "gameId": self.game_id,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class FightObservation:
    """Everything one fight in one report says about its own shape."""

    report_code: str
    fight_id: int
    encounter_id: int
    encounter_name: str
    difficulty: int | None
    kill: bool
    duration: float
    #: ``ReportFight.size`` -- the group size the log declares.
    raid_size: int | None
    #: How many friendly player actors the fight actually lists. The two disagree
    #: when someone joins or leaves mid-pull, and both are reported.
    players: int
    timeline: TargetCountTimeline
    significant_timeline: TargetCountTimeline
    enemies: tuple[EnemyLife, ...]
    adds: tuple[AddPattern, ...]
    phases: tuple[PhaseWindow, ...]
    auras: tuple[AuraWindow, ...]
    damage_by_target: tuple[TargetDamage, ...]
    active_time_fraction: float | None
    #: True when an event fetch stopped at its page limit, so anything derived
    #: from the tail of the fight is incomplete rather than absent.
    truncated: bool = False
    #: When the kill happened, epoch milliseconds, from the ranking row that
    #: nominated it. 0 when the row carried no timestamp. This is what makes
    #: "are these actually the first kills?" an answerable question instead of an
    #: assumption -- see ``select_report_fights``, where the sample is the earliest
    #: kills *among the damage-sorted pages gathered*, which is not the same thing.
    started_at: float = 0.0
    warnings: tuple[str, ...] = ()
    #: Actor ids on the raid's side: the players and everything they own. Auras
    #: applied by one of these are player effects on an enemy, not something the
    #: encounter does, and they must not be offered as candidates for an
    #: encounter's own amplification window. See ``friendly_source_ids``.
    friendly_ids: frozenset[int] = frozenset()
    #: Which enemy this pull nominates as the priority target, and why. Never a
    #: guess: an encounter where nothing stands out reports nothing.
    priority: PriorityNomination = PriorityNomination(None, None, None, "not computed")
    #: Report-local actor id to name, so an aura on an enemy that never took a hit
    #: still has something to be called. Out of equality and hashing: it is a
    #: lookup table, not part of what the fight *is*.
    actor_names: dict[int, str] = field(default_factory=dict, compare=False, repr=False)
    actor_game_ids: dict[int, int] = field(default_factory=dict, compare=False, repr=False)

    @property
    def event_coverage(self) -> float:
        """Fraction of this pull the event fetch reached. See ``TargetCountTimeline``."""
        return self.timeline.coverage

    def is_encounter_aura(self, aura: AuraWindow) -> bool:
        """Whether one aura window is something the *encounter* did to an enemy.

        Two independent tests, because the first one on its own has already
        failed in production twice:

        1. **The aura must not be on a player.** A self-buff and a boss's buff on
           its own add arrive in the same stream, and a paladin's Avenging Wrath
           on himself is not an encounter mechanic on anything. This test needs
           nothing but the target id, so it holds even when the event carried no
           source at all -- which is exactly the case the source test lets
           through, and exactly how Avenging Wrath reached the published MID2
           dataset a second time after being fixed once.
        2. **The aura must not have been applied by the raid.** This is the older
           test and it is still the one that catches a debuff a player put on an
           enemy. "The raid" rather than "a player" on purpose: a pet's debuff is
           its owner's, and reading only ``type: "Player"`` out of the master data
           let Mirror Image's Frostbolt through onto the published MID2 aura list.
           An aura whose event carried no source at all is kept: unknown is not
           the same as "the raid did it".
        """
        if aura.actor_id in self.friendly_ids:
            return False
        return not (aura.source_id is not None and aura.source_id in self.friendly_ids)

    def enemy_name(self, actor_id: int) -> str:
        """A name for one enemy actor, from the report's master data or its damage."""
        named = self.actor_names.get(actor_id)
        if named:
            return named
        return next(
            (enemy.name for enemy in self.enemies if enemy.actor_id == actor_id),
            f"actor {actor_id}",
        )

    def to_json(self) -> dict:
        return {
            "reportCode": self.report_code,
            "fightId": self.fight_id,
            "encounterId": self.encounter_id,
            "encounterName": self.encounter_name,
            "difficulty": self.difficulty,
            "kill": self.kill,
            # When the kill happened, epoch milliseconds from the ranking row, and
            # 0 when that row carried none. Pooled as `killedBetween` already; here
            # per fight because it is the one piece of evidence that could settle
            # the duplicate question *before* a kill is paid for. Two uploads of one
            # kill should carry the same absolute start, which would let
            # `select_report_fights` drop the duplicates for the price of a ranking
            # row rather than seven event queries. Nobody has checked that -- it is
            # not in any payload written so far, which is exactly why it is here.
            "startedAt": self.started_at,
            "durationSeconds": self.duration,
            "raidSize": self.raid_size,
            "playersListed": self.players,
            "targetCount": self.timeline.to_json(),
            "significantTargetCount": self.significant_timeline.to_json(),
            "adds": [add.to_json() for add in self.adds],
            "phases": [
                {
                    "id": phase.id,
                    "name": phase.name,
                    "isIntermission": phase.is_intermission,
                    "start": phase.start,
                    "duration": phase.duration,
                }
                for phase in self.phases
            ],
            "priorityEnemy": self.priority.to_json(),
            "auras": [
                {
                    "abilityId": aura.ability_id,
                    "ability": aura.ability_name,
                    "actorId": aura.actor_id,
                    "instance": aura.instance,
                    # Which enemy carried it, by name and by role. The owner was
                    # being asked to say which of three targets an amplification
                    # sits on; this is where the answer comes from instead.
                    "actorName": self.enemy_name(aura.actor_id),
                    "actorGameId": self.actor_game_ids.get(aura.actor_id),
                    "role": self.priority.role_of(aura.actor_id),
                    "start": aura.start,
                    "duration": aura.duration,
                    "truncated": aura.truncated,
                }
                for aura in self.auras
            ],
            "damageByTarget": [
                {
                    "name": entry.name,
                    "actorId": entry.actor_id,
                    "total": entry.total,
                    "share": round(entry.share, 4),
                }
                for entry in self.damage_by_target
            ],
            "activeTimeFraction": self.active_time_fraction,
            "truncated": self.truncated,
            # The single number that says how far any of the counts above can be
            # read. A fight fetched to 20% is not a fight with few targets.
            "eventCoverage": self.event_coverage,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------------
# Event readers
# --------------------------------------------------------------------------------


def _actor_key(event: dict) -> tuple[int, int] | None:
    actor_id = event.get("targetID")
    if not isinstance(actor_id, int):
        return None
    instance = event.get("targetInstance")
    return actor_id, instance if isinstance(instance, int) else 0


def enemy_lives(
    damage_events: list[dict],
    death_events: list[dict],
    fight_start_ms: float,
    fight_end_ms: float,
    actor_names: dict[int, str] | None = None,
    actor_game_ids: dict[int, int] | None = None,
) -> list[EnemyLife]:
    """Per enemy instance: when it was first hit, when it stopped, whether it died.

    Keyed on ``(targetID, targetInstance)`` rather than on ``targetID`` alone,
    because five copies of one add share a single actor id and differ only by
    instance. Collapsing them would report a wave of five as one enemy that lived
    for the union of their lifetimes.
    """
    names = actor_names or {}
    game_ids = actor_game_ids or {}
    duration = _seconds(fight_end_ms, fight_start_ms)

    first: dict[tuple[int, int], float] = {}
    last: dict[tuple[int, int], float] = {}
    damage: dict[tuple[int, int], float] = {}
    for event in damage_events:
        key = _actor_key(event)
        if key is None:
            continue
        when = _seconds(event.get("timestamp", fight_start_ms), fight_start_ms)
        if key not in first or when < first[key]:
            first[key] = when
        if key not in last or when > last[key]:
            last[key] = when
        amount = event.get("amount")
        if isinstance(amount, (int, float)):
            damage[key] = damage.get(key, 0.0) + float(amount)

    deaths: dict[tuple[int, int], float] = {}
    for event in death_events:
        # A death event names the dying unit as its target; some log versions put
        # it in sourceID instead, so both are accepted rather than silently
        # dropping every death.
        key = _actor_key(event)
        if key is None:
            source = event.get("sourceID")
            if not isinstance(source, int):
                continue
            instance = event.get("sourceInstance")
            key = (source, instance if isinstance(instance, int) else 0)
        when = _seconds(event.get("timestamp", fight_end_ms), fight_start_ms)
        if key not in deaths or when < deaths[key]:
            deaths[key] = when

    lives: list[EnemyLife] = []
    for key, start in sorted(first.items(), key=lambda item: (item[1], item[0])):
        actor_id, instance = key
        death = deaths.get(key)
        if death is not None:
            end, died = death, True
        else:
            # No death event: the enemy either despawned or was alive at the end.
            # Either way the honest end is the last time it was demonstrably
            # there, and for something alive at the kill that is the kill itself.
            end, died = min(last.get(key, start), duration), False
        lives.append(
            EnemyLife(
                actor_id=actor_id,
                instance=instance,
                name=names.get(actor_id, f"actor {actor_id}"),
                game_id=game_ids.get(actor_id),
                first_seen=start,
                last_seen=max(end, start),
                died=died,
                damage=damage.get(key, 0.0),
            )
        )
    return lives


def observed_window(lives: list[EnemyLife], duration: float) -> float:
    """How much of the fight the damage events actually reached.

    The last moment any enemy was demonstrably being fought. On a complete fetch
    that is the kill; on a fetch that hit its page limit it is the cut point, and
    everything after it is unread rather than empty. See ``TargetCountTimeline``
    for what goes wrong when the two are conflated.
    """
    if not lives:
        return 0.0
    return round(min(max(life.last_seen for life in lives), duration), 3)


def target_count_timeline(
    lives: list[EnemyLife],
    duration: float,
    observed: float | None = None,
) -> TargetCountTimeline:
    """Concurrent enemy count as a step function over the part of the fight that was read."""
    if not lives:
        return TargetCountTimeline(steps=((0.0, 0),), duration=duration, observed=observed)

    deltas: dict[float, int] = {}
    for life in lives:
        deltas[life.first_seen] = deltas.get(life.first_seen, 0) + 1
        deltas[life.last_seen] = deltas.get(life.last_seen, 0) - 1

    steps: list[tuple[float, int]] = []
    count = 0
    for when in sorted(deltas):
        count += deltas[when]
        if steps and steps[-1][1] == count:
            continue
        if steps and steps[-1][0] == when:
            steps[-1] = (when, count)
            continue
        steps.append((round(when, 3), count))

    if not steps or steps[0][0] > 0:
        steps.insert(0, (0.0, 0))
    return TargetCountTimeline(steps=tuple(steps), duration=duration, observed=observed)


def add_patterns(lives: list[EnemyLife], total_damage: float) -> list[AddPattern]:
    """Group instances by NPC and describe each group the way a raid event reads.

    ``present_at_pull`` is the distinction that decides the scenario: an NPC whose
    instances are all up in the first seconds is a *target count*, and one that
    arrives later is an *add wave*.
    """
    by_actor: dict[int, list[EnemyLife]] = {}
    for life in lives:
        by_actor.setdefault(life.actor_id, []).append(life)

    patterns: list[AddPattern] = []
    for actor_id, group in by_actor.items():
        group.sort(key=lambda life: life.first_seen)
        firsts = [life.first_seen for life in group]
        gaps = [round(b - a, 3) for a, b in zip(firsts, firsts[1:], strict=False)]
        damage = sum(life.damage for life in group)
        patterns.append(
            AddPattern(
                name=group[0].name,
                game_id=group[0].game_id,
                instances=len(group),
                first_seen=firsts[0],
                lifetime=round(statistics.median(life.lifetime for life in group), 3),
                cadence=round(statistics.median(gaps), 3) if gaps else None,
                # "At the pull" is generous by two seconds: the raid does not hit
                # everything on the same tick, and a target that was standing
                # there from the start can easily take its first damage at 1.8s.
                present_at_pull=firsts[0] <= 2.0,
                damage_share=(damage / total_damage) if total_damage else 0.0,
            )
        )
        _ = actor_id
    patterns.sort(key=lambda pattern: (-pattern.damage_share, pattern.first_seen))
    return patterns


def nominate_priority_enemy(
    lives: list[EnemyLife],
    encounter_name: str = "",
) -> PriorityNomination:
    """Which of the enemies present is the one a sim's priority target stands for.

    Two signals, in order, and a refusal:

    1. **An enemy named for the encounter.** Warcraft Logs names the fight after
       its boss, and the boss NPC almost always carries that name, so an exact or
       containing match is the strongest thing available. It is also the only
       signal that survives a fight where the adds out-damage the boss.
    2. **The enemy that took clearly the most damage**, by a stated margin. A boss
       with a five-minute health bar is not usually within 25% of an add.
    3. Otherwise **nothing is nominated**. Three enemies hit about equally is the
       shape of a permanent multi-target fight, and answering "the first one" there
       would manufacture the fact this function exists to establish.

    Nothing here decides what an aura *does* -- that is never in the API. It
    decides which enemy an aura landed on can be *called*, which is what turns
    "an amplification on one of the three" into something simc can or cannot be
    given a ``target=`` for.
    """
    if not lives:
        return PriorityNomination(None, None, None, "no enemy took damage: nothing to nominate")

    by_actor: dict[int, list[EnemyLife]] = {}
    for life in lives:
        by_actor.setdefault(life.actor_id, []).append(life)

    wanted = (encounter_name or "").strip().casefold()
    if wanted:
        for actor_id, group in sorted(by_actor.items()):
            name = (group[0].name or "").strip().casefold()
            if name and (name == wanted or name in wanted or wanted in name):
                return PriorityNomination(
                    actor_id,
                    group[0].name,
                    group[0].game_id,
                    f"named for the encounter: {group[0].name!r} in {encounter_name!r}",
                )

    ranked = sorted(
        (
            (sum(life.damage for life in group), actor_id, group)
            for actor_id, group in by_actor.items()
        ),
        key=lambda row: (-row[0], row[1]),
    )
    total = sum(damage for damage, _, _ in ranked)
    top_damage, top_actor, top_group = ranked[0]
    if len(ranked) == 1:
        return PriorityNomination(
            top_actor, top_group[0].name, top_group[0].game_id, "the only enemy that took damage"
        )

    runner_up = ranked[1][0]
    ratio = (top_damage / runner_up) if runner_up > 0 else float("inf")
    if top_damage > 0 and ratio >= PRIORITY_DAMAGE_MARGIN:
        share = (top_damage / total) if total else 0.0
        margin = "no other enemy was hit" if runner_up <= 0 else f"{ratio:.2g}x the next enemy"
        return PriorityNomination(
            top_actor,
            top_group[0].name,
            top_group[0].game_id,
            f"took {share:.0%} of the damage dealt to enemies, {margin}",
        )

    return PriorityNomination(
        None,
        None,
        None,
        f"{len(ranked)} enemies took comparable damage (the top two within "
        f"{ratio:.2g}x of each other): nothing in the events nominates one as the "
        f"priority target",
    )


def aura_windows(
    events: list[dict],
    fight_start_ms: float,
    fight_end_ms: float,
    ability_names: dict[int, str] | None = None,
) -> list[AuraWindow]:
    """Pair aura applications with their removals, per ability per enemy instance.

    Note which query these come from: an aura the *encounter* puts on an enemy is
    a **buff** on that enemy, and only one players apply is a **debuff**. A
    damage-amplification window can be either, so a probe that only asks for
    debuffs on enemies will miss exactly the case this exists to find.
    """
    names = ability_names or {}
    duration = _seconds(fight_end_ms, fight_start_ms)

    open_windows: dict[tuple[int, int, int], tuple[float, int | None]] = {}
    windows: list[AuraWindow] = []
    for event in sorted(events, key=lambda e: e.get("timestamp", 0)):
        kind = event.get("type")
        ability = event.get("abilityGameID")
        key_actor = _actor_key(event)
        if not isinstance(ability, int) or key_actor is None:
            continue
        actor_id, instance = key_actor
        key = (ability, actor_id, instance)
        when = _seconds(event.get("timestamp", fight_start_ms), fight_start_ms)
        source = event.get("sourceID")
        source = source if isinstance(source, int) else None

        if kind in AURA_START_TYPES:
            open_windows.setdefault(key, (when, source))
        elif kind in AURA_END_TYPES:
            opened = open_windows.pop(key, None)
            if opened is None:
                # Removal with no application: the aura was already up when the
                # fight (or the event page) started. Its start is unknown, so the
                # window runs from zero and is flagged rather than dropped.
                windows.append(
                    AuraWindow(
                        ability,
                        names.get(ability, str(ability)),
                        actor_id,
                        instance,
                        0.0,
                        when,
                        True,
                        source,
                    )
                )
                continue
            start, opened_by = opened
            windows.append(
                AuraWindow(
                    ability,
                    names.get(ability, str(ability)),
                    actor_id,
                    instance,
                    start,
                    when,
                    False,
                    opened_by if opened_by is not None else source,
                )
            )

    for (ability, actor_id, instance), (start, opened_by) in open_windows.items():
        windows.append(
            AuraWindow(
                ability,
                names.get(ability, str(ability)),
                actor_id,
                instance,
                start,
                duration,
                True,
                opened_by,
            )
        )

    windows.sort(key=lambda window: (window.start, window.ability_id, window.instance))
    return windows


def phase_windows(
    transitions: list[dict],
    metadata: list[dict],
    duration: float,
) -> list[PhaseWindow]:
    """Fight-relative phase windows, named from the encounter's phase metadata.

    ``phaseTransitions`` carries ids and start times but no names; ``report.phases``
    carries names but no times. Neither alone is a phase list.
    """
    by_id = {
        entry["id"]: entry
        for entry in metadata
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
    }
    ordered = sorted(
        (
            t
            for t in transitions
            if isinstance(t, dict) and isinstance(t.get("startTime"), (int, float))
        ),
        key=lambda t: t["startTime"],
    )
    if not ordered:
        return []

    # phaseTransitions timestamps are report-relative like everything else, and
    # the first transition is the start of the fight, so it is the origin.
    origin = float(ordered[0]["startTime"])
    windows: list[PhaseWindow] = []
    for index, transition in enumerate(ordered):
        start = round((float(transition["startTime"]) - origin) / 1000.0, 3)
        end = (
            round((float(ordered[index + 1]["startTime"]) - origin) / 1000.0, 3)
            if index + 1 < len(ordered)
            else duration
        )
        phase_id = transition.get("id")
        meta = by_id.get(phase_id, {})
        windows.append(
            PhaseWindow(
                id=phase_id if isinstance(phase_id, int) else index + 1,
                name=meta.get("name") or f"Phase {phase_id}",
                is_intermission=bool(meta.get("isIntermission")),
                start=start,
                end=end,
            )
        )
    return windows


def damage_by_target(table: dict | None) -> list[TargetDamage]:
    """Read a ``table(dataType: DamageDone, viewBy: Target)`` payload.

    This is the answer to "which targets actually mattered", as opposed to which
    ones merely existed. The table's shape is an untyped JSON scalar in the
    schema, so it is read defensively and an unrecognised shape yields nothing
    rather than a wrong number.
    """
    entries = _table_entries(table)
    totals = [
        (str(entry.get("name") or "?"), entry.get("id"), float(entry.get("total") or 0.0))
        for entry in entries
        if isinstance(entry, dict)
    ]
    grand = sum(total for _, _, total in totals)
    return [
        TargetDamage(
            name=name,
            actor_id=actor_id if isinstance(actor_id, int) else None,
            total=total,
            share=(total / grand) if grand else 0.0,
        )
        for name, actor_id, total in sorted(totals, key=lambda row: -row[2])
    ]


def active_time_fraction(table: dict | None, duration: float) -> float | None:
    """Median player ``activeTime`` over the fight length, from a damage-done table.

    This is the closest thing the API has to "downtime", and it is not the same
    thing: Warcraft Logs counts a player active while they are doing *something*,
    so a player who spent thirty seconds running while keeping a damage-over-time
    effect rolling does not read as idle. It is published as what it is -- an
    upper bound on uptime, useful for ranking bosses against each other, not for
    setting a movement raid event.
    """
    entries = _table_entries(table)
    fractions = [
        float(entry["activeTime"]) / 1000.0 / duration
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("activeTime"), (int, float))
    ]
    if not fractions or duration <= 0:
        return None
    return round(statistics.median(fractions), 4)


def _table_entries(table: dict | None) -> list[dict]:
    """``table`` payloads nest their rows one or two levels deep depending on view."""
    if not isinstance(table, dict):
        return []
    node = table.get("data", table)
    if isinstance(node, dict):
        for key in ("entries", "series", "targets"):
            value = node.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(node, list):
        return [entry for entry in node if isinstance(entry, dict)]
    return []


# --------------------------------------------------------------------------------
# One fight, assembled
# --------------------------------------------------------------------------------


def friendly_source_ids(actors: list[dict]) -> frozenset[int]:
    """Report-local actor ids on the raid's side: players and everything they own.

    ``masterData.actors`` types a hunter's pet, a mage's Mirror Image and a boss's
    summoned add all as ``Pet``, so the type alone separates nothing -- what
    separates them is whose ``petOwner`` they carry. Ownership is followed
    transitively (a pet of a pet is still the raid's) and is cycle-safe, because a
    malformed report must not hang the probe.

    Reading only ``type == "Player"``, which is what this replaced, let every
    pet-sourced aura through the encounter-aura filter. Mirror Image's Frostbolt
    reached the published MID2 aura list that way, listed as something Lightblinded
    Vanguard does to its own adds.
    """
    players = {
        actor["id"]
        for actor in actors
        if isinstance(actor.get("id"), int) and actor.get("type") == "Player"
    }
    owners = {
        actor["id"]: actor["petOwner"]
        for actor in actors
        if isinstance(actor.get("id"), int) and isinstance(actor.get("petOwner"), int)
    }

    friendly = set(players)
    for actor_id in owners:
        seen: set[int] = set()
        current: int | None = actor_id
        while current is not None and current not in seen:
            seen.add(current)
            if current in players:
                friendly |= seen
                break
            current = owners.get(current)
    return frozenset(friendly)


def observe_fight(
    *,
    report_code: str,
    fight: dict,
    damage_events: list[dict],
    death_events: list[dict],
    aura_events: list[dict],
    phase_metadata: list[dict],
    actor_names: dict[int, str] | None = None,
    actor_game_ids: dict[int, int] | None = None,
    ability_names: dict[int, str] | None = None,
    damage_table: dict | None = None,
    player_table: dict | None = None,
    truncated: bool = False,
    friendly_ids: frozenset[int] = frozenset(),
    significant_share: float = DEFAULT_SIGNIFICANT_SHARE,
    started_at: float = 0.0,
) -> FightObservation:
    """Assemble one fight's observations from the payloads that describe it."""
    start_ms = float(fight.get("startTime") or 0.0)
    end_ms = float(fight.get("endTime") or start_ms)
    duration = _seconds(end_ms, start_ms)

    lives = enemy_lives(damage_events, death_events, start_ms, end_ms, actor_names, actor_game_ids)
    total_damage = sum(life.damage for life in lives)
    significant = [
        life
        for life in lives
        if total_damage <= 0 or (life.damage / total_damage) >= significant_share
    ]

    friendly = fight.get("friendlyPlayers") or []
    # How far into the fight the events reached. Both timelines are averaged over
    # this rather than over the fight length, because a bounded event fetch that
    # stopped at 20% would otherwise report the other 80% as an empty room.
    window = observed_window(lives, duration)
    warnings: list[str] = []
    if not lives:
        warnings.append("no enemy damage events: target counts could not be measured")
    if not any(life.died for life in lives):
        warnings.append("no enemy deaths seen: every lifespan ends at its last hit, not a death")
    if aura_events and not friendly_ids:
        # Without a player list neither aura test can fire, and every player
        # cooldown in the stream becomes a candidate for the encounter's own
        # amplification window. Silent until now; the failure is invisible in the
        # output because the extra auras look exactly like real ones.
        warnings.append(
            "no player actors were listed for this report, so auras players applied "
            "cannot be told from auras the encounter applied: treat every aura here "
            "as unfiltered"
        )
    if duration > 0 and window / duration < COMPLETE_EVENT_COVERAGE:
        warnings.append(
            f"the event fetch reached {window:.0f}s of a {duration:.0f}s fight "
            f"({window / duration:.0%}): every target count here describes that "
            f"prefix, not the whole pull"
        )

    return FightObservation(
        report_code=report_code,
        fight_id=int(fight.get("id") or 0),
        encounter_id=int(fight.get("encounterID") or 0),
        encounter_name=str(fight.get("name") or ""),
        difficulty=fight.get("difficulty") if isinstance(fight.get("difficulty"), int) else None,
        kill=bool(fight.get("kill")),
        duration=duration,
        raid_size=fight.get("size") if isinstance(fight.get("size"), int) else None,
        players=len(friendly),
        timeline=target_count_timeline(lives, duration, window),
        # The significant timeline shares the *fight's* window, not its own: an
        # add under the floor still proves the fetch was alive at that second, and
        # recomputing the window from the survivors alone would shorten it for a
        # reason that has nothing to do with the fetch.
        significant_timeline=target_count_timeline(significant, duration, window),
        enemies=tuple(lives),
        adds=tuple(add_patterns(significant, total_damage)),
        phases=tuple(phase_windows(fight.get("phaseTransitions") or [], phase_metadata, duration)),
        auras=tuple(aura_windows(aura_events, start_ms, end_ms, ability_names)),
        damage_by_target=tuple(damage_by_target(damage_table)),
        active_time_fraction=active_time_fraction(player_table, duration),
        truncated=truncated,
        started_at=started_at,
        warnings=tuple(warnings),
        friendly_ids=friendly_ids,
        # Nominated from the enemies that mattered rather than from everything
        # that took a hit: a totem under the significance floor cannot be the
        # boss, and letting it into the ranking only adds noise to the margin.
        priority=nominate_priority_enemy(significant or lives, str(fight.get("name") or "")),
        actor_names=dict(actor_names or {}),
        actor_game_ids=dict(actor_game_ids or {}),
    )


# --------------------------------------------------------------------------------
# Several fights, pooled
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spread:
    """A pooled number with the range it was pooled from.

    The project's rule is that a figure must not read as more precise than it is,
    and a fight profile built from five logs is a small sample of guilds having
    different pulls. So nothing is ever reported as a bare median: ``low`` and
    ``high`` are the observed extremes, and a caller that wants to know whether
    five logs agreed can see it.
    """

    median: float
    low: float
    high: float
    n: int

    @property
    def agrees(self) -> bool:
        return self.low == self.high

    def to_json(self) -> dict:
        return {"median": self.median, "low": self.low, "high": self.high, "n": self.n}


def _spread(values: list[float]) -> Spread | None:
    numeric = [float(v) for v in values if v is not None]
    if not numeric:
        return None
    return Spread(
        median=round(statistics.median(numeric), 3),
        low=round(min(numeric), 3),
        high=round(max(numeric), 3),
        n=len(numeric),
    )


#: Two sampled pulls are one kill uploaded twice when their lengths agree to within
#: this many seconds **and** their target-count curves agree step for step.
#:
#: Warcraft Logs indexes uploads, not raid nights: six people in one raid who each
#: run a logger produce six reports carrying the same kills, and the sampler picks
#: kills per report. Measured over the committed MID2 document on 2026-08-31, 145
#: rows across ten (encounter, difficulty) pairs: sorting each pair's pulls by
#: length, the 65 closest consecutive gaps are all under **0.082 s** and the next
#: smallest is **1.074 s**. The threshold sits in that break -- six times the widest
#: duplicate spread observed, half the distance to the closest pair of genuinely
#: different kills. Calibrated, not derived; redo it when the sample grows.
DUPLICATE_UPLOAD_SECONDS = 0.5


def curves_agree(left, right, tolerance: float = DUPLICATE_UPLOAD_SECONDS) -> bool:
    """Do two pulls' target-count curves describe the same events?

    Length alone is circumstantial -- two guilds can kill a boss in the same number
    of seconds -- so the step function has to agree too: the same number of
    transitions, each to exactly the same count, each within ``tolerance`` of the
    other's time. Two uploads of one kill differ only by the recording clients'
    clocks, about 30 ms across MID2 Vashnik's six; two different kills do not line
    up at all.

    The count test is equality rather than a tolerance, deliberately: it is an
    integer, and a curve reaching three targets where the other reaches two is a
    different fight however well the times match.
    """
    left = list(left)
    right = list(right)
    if len(left) != len(right):
        return False
    for step, other in zip(left, right, strict=True):
        if len(step) < 2 or len(other) < 2:
            return False
        if abs(float(step[0]) - float(other[0])) > tolerance:
            return False
        if float(step[1]) != float(other[1]):
            return False
    return True


def group_uploads(items, *, duration_of, steps_of, report_of) -> list[list]:
    """Partition sampled pulls into one group per distinct kill.

    Accessor-driven so **one rule** serves both shapes this repository carries a
    pull in: ``FightObservation`` objects here, and the payload dicts the document
    builder reads. A second implementation is exactly the thing that drifts.

    A kill logged once is a group of one, so the result is always a partition of
    the input and a sample with no duplicates comes back unchanged in shape.

    The grouping is anchored rather than transitive -- an item joins a group by
    matching that group's **first** member -- so a chain of near-misses cannot walk
    a group across a gap wider than the threshold. Items are sorted by length
    first, so the answer does not depend on the order they arrived in.

    One structural guard: **a report cannot contain the same kill twice**, so two
    items sharing a report code are two pulls however alike they look. Checked
    against the whole committed MID2 document -- over 34 multi-member groups
    spanning 93 rows it never fired once, which makes it an independent
    confirmation of the length-and-curve rule rather than a guard doing work. It
    also means an item with no report code never merges, which is the safe
    direction: under-claiming a duplicate costs a count, over-claiming loses a kill.
    """
    ordered = sorted(items, key=lambda item: (duration_of(item), report_of(item)))
    groups: list[list] = []
    for item in ordered:
        duration = duration_of(item)
        for group in groups:
            anchor = group[0]
            if abs(duration - duration_of(anchor)) > DUPLICATE_UPLOAD_SECONDS:
                continue
            if not curves_agree(steps_of(item), steps_of(anchor)):
                continue
            if report_of(item) in {report_of(member) for member in group}:
                continue
            group.append(item)
            break
        else:
            groups.append([item])
    return groups


@dataclass
class EncounterObservation:
    """What several fights of one encounter agree and disagree about."""

    encounter_id: int
    encounter_name: str
    difficulty: int | None
    fights: list[FightObservation] = field(default_factory=list)
    #: True when the selection walked the whole candidate set rather than a window
    #: of it -- only the report search can claim that. It is what lets an encounter
    #: with genuinely few kills count as finished instead of being re-opened every
    #: hour forever.
    search_exhausted: bool = False
    #: Kills the search saw of this encounter, per difficulty, keyed by the raw
    #: value (``None`` for a row that stated none). It is what separates "nobody
    #: has killed this boss yet" from "the kills are at a difficulty this run did
    #: not ask for", and those look identical on a view that only has a count.
    difficulties_seen: dict[int | None, int] = field(default_factory=dict)

    def distinct_fights(self) -> list[FightObservation]:
        """The sampled pulls, one row per kill: the upload of each read furthest.

        ``fights`` is what the probe read and is what its cost is measured in.
        Everything *pooled* is an observation of a kill, and one kill logged by six
        people is one observation -- so every median, spread and "seen in N fights"
        below goes through here. Measured on MID2: Vashnik at Mythic is six rows of
        one 434.78 s pull, and its Heroic sample is 30 rows of 18 kills.

        Uploads of one kill are near-identical by construction, so which one is kept
        barely moves a number -- but it must be deterministic, and it must prefer the
        copy whose event fetch got furthest, because that is the one whose tail is
        real.
        """
        groups = group_uploads(
            self.fights,
            duration_of=lambda fight: float(fight.duration or 0.0),
            steps_of=lambda fight: fight.significant_timeline.steps,
            report_of=lambda fight: str(fight.report_code or ""),
        )
        return [
            max(
                group,
                key=lambda fight: (
                    not fight.truncated,
                    float(fight.event_coverage or 0.0),
                    str(fight.report_code or ""),
                ),
            )
            for group in groups
        ]

    def _values(self, pick) -> list[float]:
        return [
            value
            for value in (pick(fight) for fight in self.distinct_fights())
            if value is not None
        ]

    # `fights_sampled` and `reports` are the two things a promotion decision needs
    # that are not a Spread. They are named to match the published document's keys
    # so one planner reads a live observation and a downloaded artifact alike.

    @property
    def fights_sampled(self) -> int:
        return len(self.fights)

    @property
    def reports(self) -> list[str]:
        return sorted({fight.report_code for fight in self.fights})

    @property
    def killed_between(self) -> tuple[float, float] | None:
        """Earliest and latest sampled kill, epoch ms, or None when no row had a date.

        Published so the sample can be argued with. ``--order first`` takes the
        earliest kills *among the ranking pages gathered*, and those pages are
        sorted by damage -- so a narrow gather quietly returns kills from well after
        the raid opened while still being, truthfully, "the earliest ones we saw".
        The only way to notice is to look at the dates, which means they have to be
        in the output.
        """
        stamps = [fight.started_at for fight in self.fights if fight.started_at]
        return (min(stamps), max(stamps)) if stamps else None

    @property
    def duration(self) -> Spread | None:
        return _spread(self._values(lambda fight: fight.duration))

    @property
    def raid_size(self) -> Spread | None:
        return _spread(self._values(lambda fight: fight.raid_size))

    @property
    def players(self) -> Spread | None:
        return _spread(self._values(lambda fight: float(fight.players)))

    @property
    def mean_targets(self) -> Spread | None:
        return _spread(self._values(lambda fight: fight.significant_timeline.mean))

    @property
    def peak_targets(self) -> Spread | None:
        return _spread(self._values(lambda fight: float(fight.significant_timeline.peak)))

    @property
    def peak_share(self) -> Spread | None:
        """How much of each fight was spent at its peak target count."""
        return _spread(self._values(lambda fight: fight.significant_timeline.peak_share))

    @property
    def uptime(self) -> Spread | None:
        return _spread(self._values(lambda fight: fight.active_time_fraction))

    @property
    def event_coverage(self) -> Spread | None:
        """How much of each sampled fight the event fetch reached, pooled.

        The gate on every whole-fight claim. On the first real nine-boss pass this
        ran from 0.11 to 1.0 depending on the encounter, and nothing downstream had
        any way to know.
        """
        return _spread(self._values(lambda fight: fight.event_coverage))

    def pooled_adds(self) -> list[dict]:
        """Per NPC, what the fights agreed on -- keyed by game id where there is one.

        Report-local actor ids differ between reports, so pooling has to key on
        the game id. NPCs without one are pooled by name, which is the same
        compromise the site's own tooltips make.
        """
        buckets: dict[object, list[AddPattern]] = {}
        # Kills, not rows. `seenInFights` and every spread below would otherwise
        # count a kill once per person who uploaded it -- MID2's Vashnik at Heroic
        # is 30 rows and 18 kills, so "in 30 of 30 fights" was a row count wearing
        # a kill count's name.
        for fight in self.distinct_fights():
            for add in fight.adds:
                buckets.setdefault(add.game_id or add.name, []).append(add)

        pooled: list[dict] = []
        for key, group in buckets.items():
            instances = _spread([float(add.instances) for add in group])
            pooled.append(
                {
                    "key": key,
                    "name": group[0].name,
                    "gameId": group[0].game_id,
                    # A fight count, and correctly so: `add_patterns` emits exactly
                    # one AddPattern per (fight, NPC), so the group's length is the
                    # number of fights the NPC appeared in and `instances` carries
                    # how many copies. This is the control for the three siblings
                    # that got it wrong -- phases counted windows, auras and their
                    # carriers counted reports. Do not "simplify" it to match them.
                    "seenInFights": len(group),
                    "instances": instances.to_json() if instances else None,
                    "firstSeen": _pooled(group, lambda add: add.first_seen),
                    "lifetime": _pooled(group, lambda add: add.lifetime),
                    "cadence": _pooled(group, lambda add: add.cadence),
                    "damageShare": _pooled(group, lambda add: round(add.damage_share, 4)),
                    "presentAtPull": all(add.present_at_pull for add in group),
                }
            )
        pooled.sort(key=lambda entry: -((entry["damageShare"] or {}).get("median", 0.0)))
        return pooled

    def pooled_auras(self, min_fights: int = 2, encounter_only: bool = True) -> list[dict]:
        """Auras on enemies seen in at least ``min_fights`` of the sampled fights.

        Fights, counted as ``(report, fight id)`` -- the gate and the published
        ``seenInFights`` both used to count distinct *reports*, which is the same
        number under today's sampler and a smaller one under any sampler that
        takes two kills from one log. ``applications`` is the window count and
        always was.

        A one-off is dropped by default because a single fight cannot separate
        "this encounter does this" from "this pull went badly". Raising the floor
        is the knob; inventing a window from one observation is not.

        ``encounter_only`` drops auras that are not the encounter's own, by both
        of the tests in ``FightObservation.is_encounter_aura``: not on a player,
        and not applied by one. The first real probe run nominated Avenging Wrath
        as an encounter mechanic with only the source test in place, and the first
        real *nine-boss* run did it again -- self-buffs frequently carry no source
        id at all, so only the target test catches them.
        """
        buckets: dict[int, list[tuple[tuple[str, int], AuraWindow]]] = {}
        # Kills, not rows -- and here it also moves the `min_fights` gate, which is
        # the point of the gate: an aura seen in one kill that six people uploaded
        # used to clear a floor of two on its own.
        for fight in self.distinct_fights():
            for aura in fight.auras:
                if encounter_only and not fight.is_encounter_aura(aura):
                    continue
                key = (fight.report_code, fight.fight_id)
                buckets.setdefault(aura.ability_id, []).append((key, aura))

        pooled: list[dict] = []
        for ability_id, group in buckets.items():
            # Fights, not reports. The docstring above says "fights" and the gate
            # said reports, so two sampled kills out of one guild's log counted
            # once -- an under-count, the safe direction, and still a field whose
            # name promised more than its computation delivered.
            #
            # No published number moves: `select_report_fights` takes the earliest
            # N *distinct reports*, so the two counts are equal by construction of
            # today's sampler. Checked against the committed MID2 fights.json --
            # zero duplicated report codes in any of its ten measurements. That is
            # a property of the sampler, not of the format, and `--order public`
            # already reads a zone's reports rather than one kill each.
            seen_in = {key for key, _ in group}
            if len(seen_in) < min_fights:
                continue
            windows = [aura for _, aura in group]
            carriers = self._aura_carriers(ability_id, encounter_only=encounter_only)
            pooled.append(
                {
                    "abilityId": ability_id,
                    "ability": windows[0].ability_name,
                    "seenInFights": len(seen_in),
                    "applications": len(windows),
                    "start": _pooled(windows, lambda aura: aura.start),
                    "duration": _pooled(windows, lambda aura: aura.duration),
                    "distinctTargets": len({(a.actor_id, a.instance) for a in windows}),
                    # *Which* enemy carried it, pooled by game id because actor ids
                    # are report-local. This is the answer to "which of the three
                    # targets does the amplification sit on", read out of the data
                    # instead of asked of a person.
                    "carriedBy": carriers,
                    "role": _combined_role(carriers),
                    "roleEvidence": self._role_evidence(carriers),
                    # A truncated window has no measured end, so its duration is a
                    # floor rather than a value.
                    "anyTruncated": any(aura.truncated for aura in windows),
                    # How many of these windows named who applied them. The source
                    # test can only fire on those; the rest are kept because unknown
                    # is not "the raid did it", and a list full of them means this
                    # encounter's aura filter is running on the target test alone.
                    # Published so the next run measures that instead of it being
                    # inferred from which ability names look out of place.
                    "sourced": sum(1 for aura in windows if aura.source_id is not None),
                }
            )
        pooled.sort(key=lambda entry: (entry["start"] or {}).get("median", 0.0))
        return pooled

    def _aura_carriers(self, ability_id: int, *, encounter_only: bool = True) -> list[dict]:
        """The enemies one aura landed on, pooled across reports.

        Keyed on the NPC's game id where there is one and on its name otherwise,
        for the reason already established for adds: a report-local actor id means
        nothing in the next report, so pooling on it reports the same NPC three
        times. ``role`` is per fight, because which enemy a pull nominated as its
        priority target is a property of that pull's own damage.
        """
        carriers: dict[object, dict] = {}
        for fight in self.distinct_fights():
            for aura in fight.auras:
                if aura.ability_id != ability_id:
                    continue
                if encounter_only and not fight.is_encounter_aura(aura):
                    continue
                name = fight.enemy_name(aura.actor_id)
                game_id = fight.actor_game_ids.get(aura.actor_id)
                entry = carriers.setdefault(
                    game_id if game_id is not None else name,
                    {
                        "name": name,
                        "gameId": game_id,
                        "applications": 0,
                        "instances": set(),
                        # Fights, not reports -- see `pooled_auras`. Two kills out
                        # of one log are two observations of who carried the aura.
                        "fights": set(),
                        "roles": set(),
                    },
                )
                entry["applications"] += 1
                entry["instances"].add(aura.instance)
                entry["fights"].add((fight.report_code, fight.fight_id))
                entry["roles"].add(fight.priority.role_of(aura.actor_id))

        resolved = [
            {
                "name": entry["name"],
                "gameId": entry["gameId"],
                "applications": entry["applications"],
                "instances": len(entry["instances"]),
                "seenInFights": len(entry["fights"]),
                "role": _one_role(entry["roles"]),
            }
            for entry in carriers.values()
        ]
        resolved.sort(key=lambda entry: (-entry["applications"], entry["name"]))
        return resolved

    def _role_evidence(self, carriers: list[dict]) -> str:
        """Why the carriers were called what they were called, in one sentence."""
        if not carriers:
            return "no enemy carried this aura in the sampled fights"
        nominations = sorted({fight.priority.evidence for fight in self.fights})
        named = ", ".join(f"{entry['name']} ({entry['role']})" for entry in carriers[:4])
        return f"carried by {named}. Priority target nominated because: " + "; ".join(nominations)

    def pooled_phases(self) -> list[dict]:
        """Per phase, what the fights agreed on -- and in how many of them.

        ``seenInFights`` counts **fights**, and ``windows`` counts the windows
        those fights contributed. A phase recurs within one pull, so the two are
        not the same number and the second used to be published as the first.

        Measured against the committed MID2 ``fights.json`` (2026-08-30), which
        is the cleanest possible demonstration because it needs no probe payload:
        the *representative pull alone* of Entombed Sentinels carries four
        ``Stage One`` windows and three ``Intermission`` windows, and the pooled
        spreads read n=8 and n=6. So a reader was told "seen in 8 of 8 fights"
        about a phase **two** of the eight kills observed, and the other six say
        nothing about it -- Warcraft Logs does not return ``phaseTransitions`` on
        every fight. The Lost Explorers is the same shape: 4 + 1 + 1 + 1 in the
        representative against pooled 8 + 2 + 2 + 2, so two of nine.

        This matters beyond the label. #115 wants the intermission windows
        promoted into ``invulnerable`` events on the boss scenarios, and a
        promotion gate that checks sample size would be reading a four-fold
        overstatement of it. Input first, reader second.

        The spreads' own ``n`` is unchanged and still counts windows, which is
        what it has always been -- honest arithmetic that was merely unreadable
        beside a fights count that lied. With ``windows`` published it can be
        read for what it is.
        """
        buckets: dict[int, list[PhaseWindow]] = {}
        fights_seen: dict[int, set[tuple[str, int]]] = {}
        for fight in self.distinct_fights():
            for phase in fight.phases:
                buckets.setdefault(phase.id, []).append(phase)
                fights_seen.setdefault(phase.id, set()).add((fight.report_code, fight.fight_id))
        return [
            {
                "id": phase_id,
                "name": group[0].name,
                "isIntermission": group[0].is_intermission,
                "start": _pooled(group, lambda phase: phase.start),
                "duration": _pooled(group, lambda phase: phase.duration),
                "seenInFights": len(fights_seen[phase_id]),
                "windows": len(group),
            }
            for phase_id, group in sorted(buckets.items())
        ]

    def to_json(self) -> dict:
        return {
            "encounterId": self.encounter_id,
            "encounterName": self.encounter_name,
            "difficulty": self.difficulty,
            "fightsSampled": len(self.fights),
            # How many kills those rows are. Published beside the row count and
            # never instead of it: the row count is what the run's cost is measured
            # in, the kill count is what every pooled number above is an
            # observation of.
            "distinctKills": len(self.distinct_fights()),
            "reports": sorted({fight.report_code for fight in self.fights}),
            "durationSeconds": _json(self.duration),
            "raidSize": _json(self.raid_size),
            "playersListed": _json(self.players),
            "meanTargets": _json(self.mean_targets),
            "peakTargets": _json(self.peak_targets),
            "peakTargetShare": _json(self.peak_share),
            "activeTimeFraction": _json(self.uptime),
            "eventCoverage": _json(self.event_coverage),
            "searchExhausted": self.search_exhausted,
            # Only when there is something to say. An encounter that was read fine
            # publishes the bytes it did before this existed, so a quiet re-probe
            # still leaves nothing to commit.
            **(
                {
                    "difficultiesSeen": {
                        str(k): v
                        for k, v in sorted(
                            self.difficulties_seen.items(), key=lambda kv: (kv[0] is None, kv[0])
                        )
                    }
                }
                if self.difficulties_seen
                else {}
            ),
            # When the sampled kills happened. Published so `--order first` can be
            # checked rather than believed: it takes the earliest kills *among the
            # damage-sorted ranking pages gathered*, so a narrow gather returns
            # kills from well after the raid opened while still being, truthfully,
            # the earliest ones seen. Dates in the output are the only way to notice.
            "killedBetween": _killed_between(self.killed_between),
            "adds": self.pooled_adds(),
            "auras": self.pooled_auras(),
            "phases": self.pooled_phases(),
            "fights": [fight.to_json() for fight in self.fights],
        }


def _killed_between(span: tuple[float, float] | None) -> dict | None:
    """Epoch-millisecond kill span as ISO dates, or None when no row carried one."""
    if not span:
        return None
    first, last = span
    return {
        "first": datetime.fromtimestamp(first / 1000, UTC).isoformat(timespec="seconds"),
        "last": datetime.fromtimestamp(last / 1000, UTC).isoformat(timespec="seconds"),
        "spanDays": round((last - first) / 86_400_000, 1),
    }


def _one_role(roles: set[str]) -> str:
    """One enemy's role across the pulls it was seen in.

    An enemy that was the priority target in one pull and an add in another is
    ``mixed``, which is a finding about the extraction rather than about the
    encounter -- the same NPC cannot be both. ``unknown`` anywhere wins over a
    resolved role, because a pull that nominated nothing is not evidence that the
    other pulls were right.
    """
    known = {role for role in roles if role != ROLE_UNKNOWN}
    if not known:
        return ROLE_UNKNOWN
    if ROLE_UNKNOWN in roles or len(known) > 1:
        return ROLE_MIXED if len(known) > 1 else ROLE_UNKNOWN
    return next(iter(known))


def _combined_role(carriers: list[dict]) -> str:
    """One role for a whole aura, across every enemy that carried it."""
    roles = {entry["role"] for entry in carriers}
    if not roles:
        return ROLE_UNKNOWN
    if roles == {ROLE_PRIORITY}:
        return ROLE_PRIORITY
    if roles == {ROLE_ADD}:
        return ROLE_ADD
    if roles == {ROLE_UNKNOWN}:
        return ROLE_UNKNOWN
    return ROLE_MIXED


def _pooled(group: list, pick) -> dict | None:
    spread = _spread([pick(item) for item in group])
    return spread.to_json() if spread else None


def _json(spread: Spread | None) -> dict | None:
    return spread.to_json() if spread else None
