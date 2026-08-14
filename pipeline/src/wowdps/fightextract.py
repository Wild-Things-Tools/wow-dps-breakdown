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

#: A fight counts as a constant-target fight when it spends at least this much of
#: its length at its peak count. Not 100%: on a kill the targets die one after
#: another over the last second or two, which would otherwise make every fight in
#: the game "variable" for a reason that has nothing to do with its shape.
CONSTANT_TARGET_SHARE = 0.95


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
    """How many enemies were alive at once, as a step function.

    ``steps`` are ``(start second, count)`` pairs, each running until the next
    one. ``mean`` is time-weighted, which is the number a single ``desired_targets``
    would have to stand for; ``peak_share`` says whether that is even a fair
    summary, by reporting how much of the fight was spent at the highest count.
    """

    steps: tuple[tuple[float, int], ...]
    duration: float

    def _spans(self):
        for index, (start, count) in enumerate(self.steps):
            end = self.steps[index + 1][0] if index + 1 < len(self.steps) else self.duration
            yield count, max(0.0, end - start)

    @property
    def mean(self) -> float:
        if not self.steps or self.duration <= 0:
            return 0.0
        return round(sum(count * span for count, span in self._spans()) / self.duration, 3)

    @property
    def peak(self) -> int:
        return max((count for _, count in self.steps), default=0)

    @property
    def peak_share(self) -> float:
        """Fraction of the fight spent at the peak count."""
        if self.duration <= 0:
            return 0.0
        at_peak = sum(span for count, span in self._spans() if count == self.peak)
        return round(at_peak / self.duration, 4)

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
    warnings: tuple[str, ...] = ()
    #: Actor ids the report lists as players. Auras applied by one of these are
    #: player debuffs on an enemy, not something the encounter does, and they must
    #: not be offered as candidates for an encounter's own amplification window.
    player_ids: frozenset[int] = frozenset()

    def to_json(self) -> dict:
        return {
            "reportCode": self.report_code,
            "fightId": self.fight_id,
            "encounterId": self.encounter_id,
            "encounterName": self.encounter_name,
            "difficulty": self.difficulty,
            "kill": self.kill,
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
            "auras": [
                {
                    "abilityId": aura.ability_id,
                    "ability": aura.ability_name,
                    "actorId": aura.actor_id,
                    "instance": aura.instance,
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


def target_count_timeline(lives: list[EnemyLife], duration: float) -> TargetCountTimeline:
    """Concurrently-alive enemy count as a step function over the fight."""
    if not lives:
        return TargetCountTimeline(steps=((0.0, 0),), duration=duration)

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
    return TargetCountTimeline(steps=tuple(steps), duration=duration)


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
    player_ids: frozenset[int] = frozenset(),
    significant_share: float = DEFAULT_SIGNIFICANT_SHARE,
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
    warnings: list[str] = []
    if not lives:
        warnings.append("no enemy damage events: target counts could not be measured")
    if not any(life.died for life in lives):
        warnings.append("no enemy deaths seen: every lifespan ends at its last hit, not a death")

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
        timeline=target_count_timeline(lives, duration),
        significant_timeline=target_count_timeline(significant, duration),
        enemies=tuple(lives),
        adds=tuple(add_patterns(significant, total_damage)),
        phases=tuple(phase_windows(fight.get("phaseTransitions") or [], phase_metadata, duration)),
        auras=tuple(aura_windows(aura_events, start_ms, end_ms, ability_names)),
        damage_by_target=tuple(damage_by_target(damage_table)),
        active_time_fraction=active_time_fraction(player_table, duration),
        truncated=truncated,
        warnings=tuple(warnings),
        player_ids=player_ids,
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


@dataclass
class EncounterObservation:
    """What several fights of one encounter agree and disagree about."""

    encounter_id: int
    encounter_name: str
    difficulty: int | None
    fights: list[FightObservation] = field(default_factory=list)

    def _values(self, pick) -> list[float]:
        return [value for value in (pick(fight) for fight in self.fights) if value is not None]

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

    def pooled_adds(self) -> list[dict]:
        """Per NPC, what the fights agreed on -- keyed by game id where there is one.

        Report-local actor ids differ between reports, so pooling has to key on
        the game id. NPCs without one are pooled by name, which is the same
        compromise the site's own tooltips make.
        """
        buckets: dict[object, list[AddPattern]] = {}
        for fight in self.fights:
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

        A one-off is dropped by default because a single fight cannot separate
        "this encounter does this" from "this pull went badly". Raising the floor
        is the knob; inventing a window from one observation is not.

        ``encounter_only`` drops auras a *player* applied. Both a boss buffing its
        own add and a warrior landing Colossus Smash put an aura on an enemy and
        arrive in the same stream, so without this the nearest-window search
        happily nominates a player cooldown as the encounter's amplification --
        it nominated Avenging Wrath on the first real run. An aura whose event
        carried no source is kept: unknown is not the same as "a player did it".
        """
        buckets: dict[int, list[tuple[str, AuraWindow]]] = {}
        for fight in self.fights:
            for aura in fight.auras:
                if (
                    encounter_only
                    and aura.source_id is not None
                    and aura.source_id in fight.player_ids
                ):
                    continue
                buckets.setdefault(aura.ability_id, []).append((fight.report_code, aura))

        pooled: list[dict] = []
        for ability_id, group in buckets.items():
            reports = {code for code, _ in group}
            if len(reports) < min_fights:
                continue
            windows = [aura for _, aura in group]
            pooled.append(
                {
                    "abilityId": ability_id,
                    "ability": windows[0].ability_name,
                    "seenInFights": len(reports),
                    "applications": len(windows),
                    "start": _pooled(windows, lambda aura: aura.start),
                    "duration": _pooled(windows, lambda aura: aura.duration),
                    "distinctTargets": len({(a.actor_id, a.instance) for a in windows}),
                    # A truncated window has no measured end, so its duration is a
                    # floor rather than a value.
                    "anyTruncated": any(aura.truncated for aura in windows),
                }
            )
        pooled.sort(key=lambda entry: (entry["start"] or {}).get("median", 0.0))
        return pooled

    def pooled_phases(self) -> list[dict]:
        buckets: dict[int, list[PhaseWindow]] = {}
        for fight in self.fights:
            for phase in fight.phases:
                buckets.setdefault(phase.id, []).append(phase)
        return [
            {
                "id": phase_id,
                "name": group[0].name,
                "isIntermission": group[0].is_intermission,
                "start": _pooled(group, lambda phase: phase.start),
                "duration": _pooled(group, lambda phase: phase.duration),
                "seenInFights": len(group),
            }
            for phase_id, group in sorted(buckets.items())
        ]

    def to_json(self) -> dict:
        return {
            "encounterId": self.encounter_id,
            "encounterName": self.encounter_name,
            "difficulty": self.difficulty,
            "fightsSampled": len(self.fights),
            "reports": sorted({fight.report_code for fight in self.fights}),
            "durationSeconds": _json(self.duration),
            "raidSize": _json(self.raid_size),
            "playersListed": _json(self.players),
            "meanTargets": _json(self.mean_targets),
            "peakTargets": _json(self.peak_targets),
            "peakTargetShare": _json(self.peak_share),
            "activeTimeFraction": _json(self.uptime),
            "adds": self.pooled_adds(),
            "auras": self.pooled_auras(),
            "phases": self.pooled_phases(),
            "fights": [fight.to_json() for fight in self.fights],
        }


def _pooled(group: list, pick) -> dict | None:
    spread = _spread([pick(item) for item in group])
    return spread.to_json() if spread else None


def _json(spread: Spread | None) -> dict | None:
    return spread.to_json() if spread else None
