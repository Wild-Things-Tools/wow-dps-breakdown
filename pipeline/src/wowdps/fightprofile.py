"""Per-boss fight profiles: what shape an encounter has, and where that is known from.

The problem this exists to fix
------------------------------
The site's Warcraft Logs cross-check compares a Patchwerk single-target sim against
Mythic kills, and the result is dominated by *which boss* rather than by which spec.
Across the 192 published comparisons the median logs/sim ratio runs 0.57 on
Belo'ren to 0.86 on Lightblinded Vanguard, in the same order for all 26 specs, and
the only comparisons where the logs beat the sim are casters on the boss with
permanent extra targets. Comparing one fight style against nine encounters cannot
be fixed by tuning the sim; it needs a sim per encounter.

A *fight profile* is the shape of one encounter: how many targets are up and when,
what the adds do, where the phases are, when damage is amplified, how long a kill
takes. ``to_plan`` turns one into SimulationCraft options.

Facts carry their provenance, and the two kinds are not interchangeable
----------------------------------------------------------------------
Every fact records where it came from:

``hand``
    A person asserted it. The owner plays these fights, and "one of the three takes
    about 20% extra damage for the first twenty seconds" is knowledge no event
    stream contains -- the API can show an aura's id and window, never what it
    does. Hand facts are first-class here, not a placeholder for a measurement
    that has not happened.

``logs``
    ``wowdps fight-probe`` measured it from a named set of reports, which are
    recorded in the fact. A profile pooled from five kills is five guilds' pulls,
    so measured facts carry the spread they were pooled from and not just a
    median.

``default``
    Nothing is known and the value is the project's fallback. Kept explicit so a
    scenario built from it says so instead of looking like a measurement.

The two disagreeing is a *finding*, not a merge conflict: if the probe says two
targets on a fight the owner knows has three, the extraction is wrong, and
``compare_to_measurement`` is what makes that visible instead of quietly
overwriting one with the other.

What SimulationCraft can and cannot represent
---------------------------------------------
Target counts and add waves map cleanly onto ``desired_targets`` and the ``adds``
raid event. A damage-amplification window maps onto the ``vulnerable`` raid event,
and a phase in which the boss cannot be hit maps onto ``invulnerable`` with
``timestamps=`` -- see ``ImmunityWindow``, which carries the measurement that
replaced this file's earlier claim that no such event existed.

Both of those land on the priority target and **cannot be aimed at an add**, which
is measured rather than inferred (simc ``b0ea612``, 2026-08-30): ``target=`` is
resolved at option-parse time against ``sim->target_list``, and a raid-event add
does not exist yet then. An unresolved name does not fail -- it falls back to
``sim->target``, exits 0, and prints one "Trivial" log line, so a scenario claiming
"the add takes 20% more" would silently publish "the boss takes 20% more" and
nothing downstream could tell. That is why those cases stay unrepresented rather
than being aimed somewhere plausible.

Anything that cannot be expressed is returned in ``ScenarioPlan.unrepresented``
rather than dropped: a scenario that silently models three quarters of an encounter
is worse than one that says which quarter is missing.

And the trap that has already cost this project a run once: a scenario carrying its
own raid events **must not name a fight style**. ``sim_t::init_fight_style`` calls
``raid_events_str.clear()`` for Patchwerk, so ``fight_style=Patchwerk`` plus
``raid_events=`` silently yields a plain single-target sim. ``to_scenario`` leaves
``fight_style`` as ``None``; simc defaults to Patchwerk anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .scenarios import DEFAULT_TARGET_COUNTS, Scenario

#: A fact measured from Warcraft Logs by ``wowdps fight-probe``.
SOURCE_LOGS = "logs"
#: A fact a person asserted, typically from playing the encounter.
SOURCE_HAND = "hand"
#: No fact: the project's fallback, recorded so it cannot pass for a measurement.
SOURCE_DEFAULT = "default"

VALID_SOURCES = frozenset({SOURCE_LOGS, SOURCE_HAND, SOURCE_DEFAULT})

#: Where a damage-amplification window lands. Only ``priority`` has a simc
#: equivalent; the others are carried so the gap is visible.
TARGET_PRIORITY = "priority"
TARGET_ADD = "add"
TARGET_UNKNOWN = "unknown"

#: simc's ``vulnerable`` raid event has no "fire once" switch, so a one-shot window
#: is written as a cooldown longer than any fight will run.
_NEVER_AGAIN = 100000


class FightProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Provenance:
    """Where one fact came from, in enough detail to argue with."""

    source: str
    detail: str
    #: For measured facts: how many fights it was pooled from.
    sample: int | None = None
    #: For measured facts: the report codes, so a number can be re-checked.
    reports: tuple[str, ...] = ()
    #: For hand facts: who said so.
    stated_by: str | None = None
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise FightProfileError(
                f"unknown provenance source {self.source!r}; expected one of "
                + ", ".join(sorted(VALID_SOURCES))
            )

    @property
    def measured(self) -> bool:
        return self.source == SOURCE_LOGS

    def summary(self) -> str:
        if self.source == SOURCE_HAND:
            who = f" ({self.stated_by})" if self.stated_by else ""
            return f"asserted by hand{who}"
        if self.source == SOURCE_LOGS:
            n = f" from {self.sample} fight(s)" if self.sample else ""
            return f"measured from logs{n}"
        return "project default, nothing measured or asserted"

    @classmethod
    def from_json(cls, raw: dict) -> Provenance:
        return cls(
            source=raw.get("source", SOURCE_DEFAULT),
            detail=raw.get("detail", ""),
            sample=raw.get("sample"),
            reports=tuple(raw.get("reports") or ()),
            stated_by=raw.get("statedBy"),
            observed_at=raw.get("observedAt"),
        )

    def to_json(self) -> dict:
        payload: dict = {"source": self.source, "detail": self.detail}
        if self.sample is not None:
            payload["sample"] = self.sample
        if self.reports:
            payload["reports"] = list(self.reports)
        if self.stated_by:
            payload["statedBy"] = self.stated_by
        if self.observed_at:
            payload["observedAt"] = self.observed_at
        return payload


@dataclass(frozen=True)
class Fact:
    """One value plus where it came from. Never one without the other."""

    value: object
    provenance: Provenance

    @classmethod
    def from_json(cls, raw: dict) -> Fact:
        return cls(
            value=raw.get("value"), provenance=Provenance.from_json(raw.get("provenance") or {})
        )

    def to_json(self) -> dict:
        return {"value": self.value, "provenance": self.provenance.to_json()}


@dataclass(frozen=True)
class AddWave:
    """A group of adds that arrives during the fight rather than standing at the pull."""

    name: str
    count: int
    first: float
    duration: float
    #: Seconds between waves. ``None`` means it happens once.
    cadence: float | None = None

    def simc_option(self) -> str:
        parts = [f"adds,count={self.count}", f"first={self.first:g}", f"duration={self.duration:g}"]
        # simc reschedules on `cooldown` unconditionally, so a single wave is a
        # cooldown longer than the fight rather than a missing option.
        parts.append(f"cooldown={self.cadence:g}" if self.cadence else f"cooldown={_NEVER_AGAIN}")
        return "raid_events+=/" + ",".join(parts)

    @classmethod
    def from_json(cls, raw: dict) -> AddWave:
        return cls(
            name=raw.get("name", "adds"),
            count=int(raw["count"]),
            first=float(raw["first"]),
            duration=float(raw["duration"]),
            cadence=float(raw["cadence"]) if raw.get("cadence") is not None else None,
        )


@dataclass(frozen=True)
class ImmunityWindow:
    """A stretch in which the priority target cannot be damaged.

    An intermission is the ordinary case: the boss leaves, or shields, and the raid
    has nothing to hit. MID2's Entombed Sentinels is the measured example -- three
    windows of *Vitriolic Stasis* at 46.4-74.4s, 165.4-191.4s and 282.4-292.4s on
    the representative pull, published twice over in ``fights.json`` (as phase
    windows and, agreeing to about ten milliseconds, as aura windows on the
    carriers).

    **This used to be published as unrepresentable and that was wrong.**
    ``to_plan`` said "no raid event expresses a target being unattackable for part
    of a fight without also moving the players". Measured on simc ``b0ea612``,
    2026-08-30, MID2 Shadow Priest at two targets, 1000 deterministic iterations:

        baseline                                    342.265 DPS
        two 25s windows, both targets immune        291.615 DPS   -14.8%

    So the event exists, it takes the measured times directly, and the claim cost
    a real 15% of a boss cell's shape.

    Two decisions in the mapping, and the second is a refusal:

    - **``timestamps=``, not ``first``/``cooldown``.** simc's ``invulnerable`` event
      takes a colon-separated list of absolute seconds, which is exactly the shape a
      measured phase list already has. Expressing three unequal windows as a cadence
      would be a claim about regularity nobody measured.
    - **The priority target only.** ``target=`` is resolved at option-parse time
      against ``sim->target_list``, and a raid-event add does not exist yet then --
      so a name that does not resolve **silently falls back to the boss**, with exit
      0 and one "Trivial" log line. Measured: ``vulnerable,target=stone1`` against an
      ``adds,name=stone`` wave printed ``Unknown vulnerability raid event target
      'stone1'`` and amplified ``sim->target`` instead. A scenario claiming "the add
      was immune" would therefore silently publish "the boss was immune", which is
      indistinguishable from outside. So a window on an add stays ``unrepresented``,
      and the sentence names the reason rather than the old, refuted one.

    ``multiplier`` is deliberately absent. simc's ``invulnerable`` is binary: it
    zeroes the damage, wipes the target's debuffs and interrupts casts. The
    near-miss alternative, ``vulnerable,multiplier=-0.99``, was also run --
    198.811 against 197.670 DPS on Shadow at one target over a single 25s window,
    a 0.6% difference -- so the choice between them is about DoT and cast semantics
    rather than about the number, and this models the semantics an intermission
    actually has.
    """

    name: str
    #: Absolute seconds from the pull at which each window opens.
    starts: tuple[float, ...]
    duration: float
    #: "priority" | "add" | "unknown" -- which target the window applies to.
    target: str = TARGET_PRIORITY

    def simc_option(self) -> str | None:
        """The raid event, or ``None`` when this window cannot be expressed."""
        if self.target != TARGET_PRIORITY or not self.starts or self.duration <= 0:
            return None
        stamps = ":".join(f"{start:g}" for start in self.starts)
        return f"raid_events+=/invulnerable,timestamps={stamps},duration={self.duration:g}"

    @classmethod
    def from_json(cls, raw: dict) -> ImmunityWindow:
        starts = raw.get("starts")
        if starts is None and raw.get("first") is not None:
            starts = [raw["first"]]
        return cls(
            name=raw.get("name", "intermission"),
            starts=tuple(float(value) for value in (starts or ())),
            duration=float(raw.get("duration", 0.0)),
            target=raw.get("target", TARGET_PRIORITY),
        )


@dataclass(frozen=True)
class Amplification:
    """A window in which something takes more damage than usual.

    ``multiplier`` is the one number in a fight profile that Warcraft Logs cannot
    supply at all. The API knows an aura was applied to an enemy for twenty
    seconds; that the aura is worth 20% is a game fact somebody has to state. So
    an amplification whose window was measured and whose magnitude was asserted is
    the normal case, and ``magnitude_provenance`` records that separately from the
    fact's own provenance.
    """

    ability: str
    multiplier: float
    first: float
    duration: float
    #: "priority" | "add" | "unknown" -- which target carries it.
    target: str = TARGET_UNKNOWN
    ability_id: int | None = None
    magnitude_source: str = SOURCE_HAND
    #: Where ``target`` came from, separately from the fact's own provenance. An
    #: amplification whose magnitude a person stated and whose *carrier* the logs
    #: measured is the normal end state, and "hand" over the whole thing would
    #: misdescribe both halves.
    target_source: str | None = None
    #: The sentence behind ``target_source``: which enemy, in how many pulls, and
    #: on what grounds it was called the priority target or an add.
    target_evidence: str | None = None

    def simc_option(self) -> str | None:
        """``None`` when simc has nothing to express this with."""
        if self.target != TARGET_PRIORITY:
            return None
        return (
            f"raid_events+=/vulnerable,first={self.first:g},duration={self.duration:g},"
            f"cooldown={_NEVER_AGAIN},multiplier={self.multiplier:g}"
        )

    @classmethod
    def from_json(cls, raw: dict) -> Amplification:
        return cls(
            ability=raw.get("ability", "unnamed"),
            multiplier=float(raw["multiplier"]),
            first=float(raw["first"]),
            duration=float(raw["duration"]),
            target=raw.get("target", TARGET_UNKNOWN),
            ability_id=raw.get("abilityId"),
            magnitude_source=raw.get("magnitudeSource", SOURCE_HAND),
            target_source=raw.get("targetSource"),
            target_evidence=raw.get("targetEvidence"),
        )

    def to_json(self) -> dict:
        payload: dict = {
            "ability": self.ability,
            "abilityId": self.ability_id,
            "multiplier": self.multiplier,
            "first": self.first,
            "duration": self.duration,
            "target": self.target,
            "magnitudeSource": self.magnitude_source,
        }
        if self.target_source:
            payload["targetSource"] = self.target_source
        if self.target_evidence:
            payload["targetEvidence"] = self.target_evidence
        return payload


@dataclass(frozen=True)
class ScenarioPlan:
    """A fight profile turned into simc options, plus what did not survive the trip."""

    encounter_id: int
    name: str
    targets: int
    max_time: int
    options: tuple[str, ...]
    #: Facts the profile carries that simc cannot express. Listed, never dropped.
    unrepresented: tuple[str, ...]
    #: Facts whose value is asserted rather than measured, so a reader of the
    #: resulting numbers knows which part of the encounter is somebody's word.
    asserted: tuple[str, ...]

    def restates_a_static_sweep(self, sweep_max_time: int = 300) -> bool:
        """Whether running this would reproduce a cell the target sweep already has.

        A profile with no add waves, no representable amplification and the default
        fight length turns into N static targets for 300 seconds -- which is exactly
        Patchwerk at N targets, under a boss's name. The number would be correct and
        the label would be a claim the simulation did not earn, so the caller is told
        rather than left to spend the CPU and find out.

        Lightblinded Vanguard is that case today: three permanent targets and an
        amplification whose target simc cannot name.
        """
        return not self.options and self.max_time == sweep_max_time

    def to_json(self) -> dict:
        return {
            "encounterId": self.encounter_id,
            "name": self.name,
            "targets": self.targets,
            "maxTime": self.max_time,
            "options": list(self.options),
            "unrepresented": list(self.unrepresented),
            "asserted": list(self.asserted),
            "restatesStaticSweep": self.restates_a_static_sweep(),
        }


@dataclass(frozen=True)
class FightProfile:
    """One encounter's shape, as a bag of facts that each know their own origin."""

    tier: str
    encounter_id: int
    name: str
    difficulty: int
    facts: dict[str, Fact] = field(default_factory=dict)

    # -- typed readers over the fact bag ------------------------------------------
    #
    # Each returns the value and the fact, so a caller can report the provenance
    # alongside whatever it does with the number. A missing fact is a default with
    # a `default` provenance rather than a None to be checked for.

    def fact(self, key: str, fallback: object, why: str) -> Fact:
        found = self.facts.get(key)
        if found is not None:
            return found
        return Fact(value=fallback, provenance=Provenance(source=SOURCE_DEFAULT, detail=why))

    @property
    def targets(self) -> Fact:
        return self.fact(
            "targets",
            {"baseline": 1, "constant": True},
            "no target count recorded; one target is what the site already assumes",
        )

    @property
    def baseline_targets(self) -> int:
        value = self.targets.value
        if isinstance(value, dict):
            return int(value.get("baseline", 1))
        return int(value or 1)

    @property
    def fight_length(self) -> Fact:
        return self.fact(
            "fightLengthSeconds",
            300,
            "no measured kill time; 300s is the length every other scenario uses",
        )

    @property
    def raid_size(self) -> Fact:
        return self.fact("raidSize", None, "raid size not recorded")

    @property
    def add_waves(self) -> list[AddWave]:
        value = self.facts.get("addWaves")
        if not value or not isinstance(value.value, list):
            return []
        return [AddWave.from_json(entry) for entry in value.value]

    @property
    def amplifications(self) -> list[Amplification]:
        value = self.facts.get("amplifications")
        if not value or not isinstance(value.value, list):
            return []
        return [Amplification.from_json(entry) for entry in value.value]

    @property
    def phases(self) -> list[dict]:
        value = self.facts.get("phases")
        if not value or not isinstance(value.value, list):
            return []
        return list(value.value)

    @property
    def immunity_windows(self) -> list[ImmunityWindow]:
        """Windows in which a target cannot be damaged, from the ``phases`` fact.

        Read off the same fact the plan already walks rather than a new one: a
        phase with stated ``downtime`` IS an immunity window, and giving it a
        second home would let the two disagree about one fight.

        A phase carrying no ``downtime`` yields nothing -- most phases change the
        target count or the damage taken, and those are already adds and
        amplifications.
        """
        windows: list[ImmunityWindow] = []
        for phase in self.phases:
            downtime = phase.get("downtime")
            if not downtime:
                continue
            starts = phase.get("starts")
            if starts is None and phase.get("first") is not None:
                starts = [phase["first"]]
            if not starts:
                # A stated downtime with no time to put it at. Left to `to_plan`,
                # which reports it unrepresented with that as the reason -- an
                # invented start would be a measurement nobody took.
                continue
            windows.append(
                ImmunityWindow(
                    name=phase.get("name", "intermission"),
                    starts=tuple(float(value) for value in starts),
                    duration=float(downtime),
                    target=phase.get("target", TARGET_PRIORITY),
                )
            )
        return windows

    # -- turning a profile into a simulation ---------------------------------------

    @property
    def has_facts(self) -> bool:
        """Whether anything about this boss is known rather than assumed.

        A profile made entirely of project fallbacks produces a scenario that is
        one target for 300 seconds with no raid events -- Patchwerk at one target
        wearing a boss's name. Running it would publish the boss's name over a
        number that has nothing to do with the boss.
        """
        return any(fact.provenance.source != SOURCE_DEFAULT for fact in self.facts.values())

    def to_plan(self) -> ScenarioPlan:
        options: list[str] = []
        unrepresented: list[str] = []
        asserted: list[str] = []

        for key in ("targets", "fightLengthSeconds", "addWaves", "amplifications", "phases"):
            found = self.facts.get(key)
            if found is not None and found.provenance.source == SOURCE_HAND:
                asserted.append(key)

        for wave in self.add_waves:
            options.append(wave.simc_option())

        for amplification in self.amplifications:
            option = amplification.simc_option()
            if option:
                options.append(option)
            else:
                unrepresented.append(
                    f"amplification {amplification.ability!r} lands on "
                    f"{amplification.target!r}; simc's vulnerable raid event can only "
                    f"target the priority target without naming a generated add"
                )

        # Phases are recorded because they are the reason a boss has a shape at
        # all, but simc has no phase concept: a phase that changes the target
        # count is already expressed as adds, and one that changes the boss's
        # damage taken is already an amplification. A phase with stated downtime
        # is the third kind, and it IS expressible -- see `ImmunityWindow` for the
        # measurement that replaced the sentence which used to sit here.
        for window in self.immunity_windows:
            option = window.simc_option()
            if option:
                options.append(option)
            else:
                unrepresented.append(
                    f"phase {window.name!r} makes {window.target!r} unattackable; "
                    f"simc's invulnerable raid event resolves target= at option-parse "
                    f"time, before a generated add exists, and an unresolved name "
                    f"falls back to the boss silently"
                )
        for phase in self.phases:
            if phase.get("downtime") and not (phase.get("starts") or phase.get("first")):
                unrepresented.append(
                    f"phase {phase.get('name')!r} has {phase['downtime']}s of stated "
                    f"downtime and no stated time to put it at"
                )

        # A timeline is absolute, and simc's fight length is not.
        #
        # `vary_combat_length` defaults to 0.2, so a 396-second cell actually runs
        # 317-475 seconds. Every option above states a time measured from the pull:
        # `adds,first=`, `vulnerable,first=`, `invulnerable,timestamps=`. Left
        # varying, a window scheduled near the measured end lands after the end of a
        # short iteration and never happens, while a repeating wave repeats more
        # often than it was observed to -- so the same scenario would model a
        # different fight in each iteration and average them.
        #
        # It is pinned only where a timeline exists. On a scenario that is N targets
        # for a length, the variance is simc's ordinary behaviour and every published
        # cell in this project has it; switching it off there would move numbers for
        # no reason. MID2 carries no wave, amplification or phase fact today, so this
        # changes nothing published and is in place for the first fact that lands.
        if options:
            options.append("vary_combat_length=0")

        length = self.fight_length.value
        return ScenarioPlan(
            encounter_id=self.encounter_id,
            name=self.name,
            targets=self.baseline_targets,
            max_time=int(length) if isinstance(length, (int, float)) else 300,
            options=tuple(options),
            unrepresented=tuple(unrepresented),
            asserted=tuple(asserted),
        )

    def to_scenario(self) -> Scenario:
        """A ``Scenario`` that runs this encounter's shape.

        ``fight_style`` is deliberately ``None``. Naming one makes simc clear the
        raid events this scenario is built out of -- verified both ways, and it
        shipped as a silent bug once already.
        """
        plan = self.to_plan()
        return Scenario(
            id=f"boss_{self.encounter_id}",
            label=self.name,
            description=(
                f"{self.name} as logged: {plan.targets} target(s) at the pull, "
                f"{len(self.add_waves)} add wave(s), {len(self.amplifications)} damage "
                f"amplification window(s), {len(self.immunity_windows)} window(s) with "
                f"the boss unattackable. Built from a fight profile, not a fight style."
            ),
            fight_style=None,
            target_counts=(plan.targets,),
            max_time=plan.max_time,
            extra_options=plan.options,
            # The boss scenario's funnel baseline is Patchwerk at one target, the
            # same controlled comparison Add Waves uses: same profile, same
            # length, no extra targets.
            funnel_baseline="patchwerk",
            timeline_at=(plan.targets,),
        )

    # -- checking a profile against a measurement ----------------------------------

    def compare_to_measurement(self, observation) -> list[dict]:
        """Asserted facts against what the probe measured, as rows to print.

        Deliberately does not resolve the disagreement. The owner's statement and
        five logs are two independent claims, and when they differ the interesting
        possibility is that the extraction is wrong -- which is invisible if one
        silently overwrites the other.
        """
        rows: list[dict] = []

        peak = observation.peak_targets
        rows.append(
            _row(
                "baseline targets",
                self.baseline_targets,
                peak.median if peak else None,
                self.targets.provenance,
                extra=f"observed {peak.low:g}-{peak.high:g}" if peak else "not measured",
            )
        )

        size = observation.raid_size
        rows.append(
            _row(
                "raid size",
                self.raid_size.value,
                size.median if size else None,
                self.raid_size.provenance,
                extra=f"observed {size.low:g}-{size.high:g}" if size else "not measured",
            )
        )

        duration = observation.duration
        rows.append(
            _row(
                "fight length (s)",
                self.fight_length.value,
                duration.median if duration else None,
                self.fight_length.provenance,
                extra=f"observed {duration.low:g}-{duration.high:g}"
                if duration
                else "not measured",
            )
        )

        for amplification in self.amplifications:
            # The magnitude is never compared -- the API does not carry it, so
            # there is nothing on the other side. The *window* is, and matching
            # is what turns a probe run into a filled-in `abilityId`.
            candidates = observation.pooled_auras()
            match, note = _match_amplification(amplification, candidates)
            rows.append(
                _row(
                    f"amplification {amplification.ability!r} window (s)",
                    f"{amplification.first:g}-{amplification.first + amplification.duration:g}",
                    _window_text(match),
                    Provenance(source=SOURCE_HAND, detail="magnitude cannot be measured"),
                    extra=note,
                )
            )
            # Which of the targets carries it. Asked of a person twice and not
            # answered; it is in the event stream, so this is where it comes from.
            rows.append(
                _row(
                    f"amplification {amplification.ability!r} carried by",
                    amplification.target,
                    carrier_text(match),
                    Provenance(
                        source=amplification.target_source or SOURCE_HAND,
                        detail=amplification.target_evidence
                        or "which target carries it, as recorded in the profile",
                    ),
                    extra=(match or {}).get("roleEvidence")
                    or "no matched aura, so no enemy to name",
                )
            )

        return rows


def _row(name: str, asserted, measured, provenance: Provenance, extra: str) -> dict:
    return {
        "fact": name,
        "profile": asserted,
        "measured": measured,
        "provenance": provenance.summary(),
        "note": extra,
    }


#: How far a measured aura's start and duration may sit from an asserted window and
#: still be offered as the aura the assertion is about. Generous, because the point
#: is to hand a human a shortlist to confirm, not to decide on their behalf: "the
#: first 20 seconds" is a description of a fight, not a stopwatch reading.
_WINDOW_TOLERANCE = 8.0


def _match_amplification(
    amplification: Amplification, candidates: list[dict]
) -> tuple[dict | None, str]:
    """Find the measured aura an asserted amplification is probably about.

    An amplification with an ``abilityId`` is matched exactly; that is the state a
    profile should end up in. Without one -- which is where every hand-written
    profile starts -- the closest window is offered as a *candidate*, in the words
    "looks like", so that a probe run's job is to hand back an ability id for
    somebody to write down rather than to silently adopt one.
    """
    if amplification.ability_id is not None:
        exact = next((a for a in candidates if a["abilityId"] == amplification.ability_id), None)
        if exact is None:
            return None, "no aura with that id in the sampled fights"
        return exact, f"matched on ability id, seen in {exact['seenInFights']} fight(s)"

    def distance(candidate: dict) -> float:
        start = (candidate.get("start") or {}).get("median")
        duration = (candidate.get("duration") or {}).get("median")
        if start is None or duration is None:
            return float("inf")
        return abs(start - amplification.first) + abs(duration - amplification.duration)

    nearest = min(candidates, key=distance, default=None)
    if nearest is None or distance(nearest) > _WINDOW_TOLERANCE:
        return None, "no aura in the sampled fights sits near this window"
    return nearest, (
        f"candidate only: looks like {nearest['ability']!r} (id {nearest['abilityId']}), "
        f"seen in {nearest['seenInFights']} fight(s). Write the id into the profile to pin it."
    )


def _window_text(match: dict | None) -> str | None:
    if not match:
        return None
    start = (match.get("start") or {}).get("median")
    duration = (match.get("duration") or {}).get("median")
    if start is None or duration is None:
        return None
    return f"{start:g}-{start + duration:g}"


def carrier_text(match: dict | None) -> str | None:
    """Which enemy carried a measured aura, named, with its role in brackets.

    The name is the answer to "which of the three"; the role is what decides
    whether simc can be given a ``target=`` for it. They are printed together
    because naming an enemy the simulation cannot address is only half an answer.
    """
    if not match:
        return None
    carriers = match.get("carriedBy") or []
    if not carriers:
        return None
    named = ", ".join(f"{entry['name']} ({entry['role']})" for entry in carriers[:3])
    if len(carriers) > 3:
        named += f", +{len(carriers) - 3} more"
    return named


def measured_target(match: dict | None) -> str | None:
    """A measured aura's role as a value a profile's ``target`` field could take.

    ``mixed`` and ``unknown`` deliberately return ``None``: an aura seen on the
    boss in one pull and an add in another, or on a fight where nothing nominates
    a boss at all, is not a value to write down. It is a reason to look again.
    """
    role = (match or {}).get("role")
    if role == TARGET_PRIORITY:
        return TARGET_PRIORITY
    if role == TARGET_ADD:
        return TARGET_ADD
    return None


# --------------------------------------------------------------------------------
# Turning a measurement into a profile fact
# --------------------------------------------------------------------------------
#
# The point of this section, and the rule it must not break
# ---------------------------------------------------------
# Typing a target count in by hand for nine bosses is exactly the work the probe
# already does, so a measured fact should be able to *become* the profile's value
# with ``source: "logs"`` and the reports it came from. What it must never do is
# overwrite something a person stated. CLAUDE.md's rule stands and is the reason
# this file exists at all: when an assertion and a measurement disagree, the
# likelier culprit is the extraction, and a promotion that quietly resolved that
# would destroy the only signal there is.
#
# So promotion is *proposed* here and *applied* by an explicit command
# (``wowdps fight-promote --write``), and a proposal is a full object -- the value,
# the evidence, whether it is eligible, and what blocks it -- so the decision can be
# read on the site and in a terminal before anything is written.
#
# Three states, three behaviours:
#
# ``default``  nothing recorded. The measurement fills the gap. Eligible.
# ``logs``     an older measurement. A newer one supersedes it. Eligible.
# ``hand``     a person said so. **Never** overwritten, even when the numbers
#              agree; the promotion is reported as blocked, with the difference.
#
# The one exception is not an exception to that rule: an amplification's *carrier*
# and *ability id* are fields a hand fact left blank (``"target": "unknown"``,
# ``"abilityId": null``). Filling a blank is not overwriting a statement, and the
# multiplier -- the one number no log can ever supply -- is never touched.

#: How many sampled fights a measurement needs before it is offered as a fact. A
#: single pull cannot separate "this encounter does this" from "this pull went
#: like that", which is the same floor ``pooled_auras`` applies to auras.
DEFAULT_MIN_FIGHTS = 3

#: Fraction of a fight at its peak target count above which a measured target
#: count is offered as *constant*. Mirrors ``fightextract.CONSTANT_TARGET_SHARE``;
#: duplicated as a number rather than imported to keep this module free of a
#: dependency on the extraction.
_CONSTANT_SHARE = 0.95

#: How much of a fight the event fetch must have reached before a count taken over
#: that fight may become a profile fact.
#:
#: This is not a tuning knob, it is a bug guard with a measurement behind it. The
#: first real nine-boss pass fetched enemy damage-taken with a page budget that a
#: twenty-player Mythic pull exhausts in the first minute or two, so four of nine
#: bosses reported a *mean* concurrent target count below 1.0 -- impossible for a
#: boss that is present throughout. Coverage ran 0.11 on Midnight Falls to 1.00 on
#: Chimaerus, and the reported means tracked it almost exactly. Anything averaged
#: over a fight is meaningless below this line; anything read off the fight's own
#: metadata (its length, its group size) is untouched by it.
MIN_EVENT_COVERAGE = 0.95


@dataclass(frozen=True)
class Promotion:
    """One measured fact, offered as a profile fact, with the case for and against.

    Nothing here writes anything. A ``Promotion`` is a proposal that can be
    printed, published to the site, and applied only by a command a person ran.
    """

    key: str
    label: str
    #: What would be written into ``fight_profiles.json`` under ``facts[key]``.
    value: object
    #: The value in words, for a terminal and for the page.
    summary: str
    #: Why the measurement supports it -- always including the spread, never a
    #: bare median.
    evidence: str
    sample: int
    reports: tuple[str, ...]
    #: True when this may be written. False is not a failure: "a person already
    #: said so" and "two fights is not enough" are both ordinary answers.
    eligible: bool
    reason: str
    #: ``hand`` when a person's statement is what stops it. Never overwritten.
    blocked_by: str | None = None
    #: What the profile says now, when there is something to disagree with.
    current: object = None
    #: True when the measurement and the current value are not the same. On a
    #: hand fact this is the finding CLAUDE.md is about, not a merge conflict.
    disagrees: bool = False

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "sample": self.sample,
            "reports": list(self.reports),
            "eligible": self.eligible,
            "reason": self.reason,
            "blockedBy": self.blocked_by,
            "current": self.current,
            "disagrees": self.disagrees,
        }


def _spread_text(spread, digits: int = 0, unit: str = "") -> str:
    """A pooled number with the range it came from. Never a bare median."""
    if spread is None:
        return "not measured"
    fmt = f"{{:.{digits}f}}{unit}"
    if spread.low == spread.high:
        return f"{fmt.format(spread.median)} in every one of the {spread.n} sampled fights"
    return (
        f"{fmt.format(spread.median)} "
        f"(range {fmt.format(spread.low)}-{fmt.format(spread.high)}, n={spread.n})"
    )


def _share_text(spread) -> str:
    """The same, for a fraction that reads as a percentage."""
    if spread is None:
        return "not measured"
    if spread.low == spread.high:
        return f"{spread.median:.0%} in every one of the {spread.n} sampled fights"
    return f"{spread.median:.0%} (range {spread.low:.0%}-{spread.high:.0%}, n={spread.n})"


def plan_promotions(
    profile: FightProfile,
    observation,
    *,
    min_fights: int = DEFAULT_MIN_FIGHTS,
) -> list[Promotion]:
    """Every measured fact this encounter could contribute, eligible or not.

    ``observation`` is anything carrying the pooled readings -- a live
    ``EncounterObservation`` or the ``MeasuredEncounter`` view over a downloaded
    probe artifact. Both are read the same way on purpose: a promotion decided in
    CI and a promotion decided offline from the artifact must come out identical.

    Ineligible proposals are returned rather than filtered, because "the logs say
    three targets and a person said two" is the single most valuable thing this
    machinery can produce, and dropping it would leave a page that only ever
    agreed with itself.
    """
    sample = int(getattr(observation, "fights_sampled", 0) or 0)
    reports = tuple(getattr(observation, "reports", ()) or ())
    proposals = [
        _targets_promotion(observation),
        _fight_length_promotion(observation),
        _raid_size_promotion(observation),
    ]
    amplifications = _amplification_promotion(profile, observation)
    if amplifications is not None:
        proposals.append(amplifications)

    return [
        _decide(profile, proposal, sample=sample, reports=reports, min_fights=min_fights)
        for proposal in proposals
        if proposal is not None
    ]


@dataclass(frozen=True)
class _Proposal:
    """A measurement's own case, before the profile has been consulted."""

    key: str
    label: str
    value: object
    summary: str
    evidence: str
    #: Set when the measurement is not solid enough to offer regardless of what
    #: the profile says -- the fights disagreed with each other, for instance.
    withheld: str | None = None
    #: True for a field-level fill of blanks a hand fact left, which a hand fact
    #: does not block. See the section header.
    fills_blanks: bool = False


def _targets_promotion(observation) -> _Proposal | None:
    """A target count from the logs -- from the *peak*, never from the mean.

    The mean concurrent target count is the obvious candidate and is the wrong
    one twice over. It is the statistic the truncated event fetch destroys (see
    ``MIN_EVENT_COVERAGE``), and even on a complete fetch it counts an enemy only
    while it is *being damaged*: on Lightblinded Vanguard, where all three targets
    are up for the whole pull, the raid stops hitting one of them part-way through
    and the mean falls to 2.15 for a reason that has nothing to do with how many
    things are alive. The peak has neither problem -- it is the largest count
    observed, so a shorter read and an early target switch can only ever make it
    too small, and never quietly wrong.
    """
    peak = getattr(observation, "peak_targets", None)
    if peak is None:
        return None
    share = getattr(observation, "peak_share", None)
    coverage = getattr(observation, "event_coverage", None)
    constant = bool(share and share.median >= _CONSTANT_SHARE)
    baseline = int(round(peak.median))

    withheld = None
    if coverage is None:
        withheld = (
            "this probe run predates the event-coverage measurement, so there is no "
            "way to tell whether the counts describe whole fights or the first "
            "minute of them; re-probe before promoting a target count"
        )
    elif coverage.low < MIN_EVENT_COVERAGE:
        # The decision is made on the *worst* fight, so that is the figure the
        # sentence has to lead with. Printing the median after the word "only"
        # produced "the event fetch reached only 100% (range 93%-100%)", which
        # reads as nonsense and makes a correct refusal look like a bug -- seen on
        # Lightblinded Vanguard the first time this panel met real data.
        withheld = (
            f"one of the sampled fights was only read to {coverage.low:.0%} "
            f"(the set ran {coverage.low:.0%}-{coverage.high:.0%}, n={coverage.n}), "
            f"and a count taken over a partly-read fight is a reading of a prefix. "
            f"Raise --max-pages on the probe and re-run before promoting this"
        )
    elif peak.low != peak.high:
        withheld = (
            f"the sampled fights did not agree on the peak target count "
            f"({peak.low:g}-{peak.high:g}); a single number for the profile would "
            f"be a choice, not a measurement"
        )

    return _Proposal(
        key="targets",
        label="Targets at the pull",
        value={"baseline": baseline, "constant": constant},
        summary=f"{baseline} target(s), {'constant' if constant else 'varying'}",
        evidence=(
            f"peak concurrent enemies above the significance floor: "
            f"{_spread_text(peak)}; time at that peak {_share_text(share)}; "
            f"event fetch covered {_share_text(coverage)} of each fight. The peak "
            f"is used and the mean is not: an enemy counts only while it is being "
            f"damaged, so the mean falls when the raid switches targets and falls "
            f"again when the fetch stops early"
        ),
        withheld=withheld,
    )


def _fight_length_promotion(observation) -> _Proposal | None:
    duration = getattr(observation, "duration", None)
    if duration is None:
        return None
    # No agreement gate here, unlike the target count: kills legitimately differ in
    # length, so a spread is the expected result rather than a warning sign. The
    # spread travels in the evidence so the median never reads as exact.
    #
    # No coverage gate either, and that is not an oversight: fight length comes
    # from the report's own startTime/endTime, which is metadata about the pull
    # rather than something counted out of the event stream. A fetch that stopped
    # at 20% still knows exactly how long the fight ran.
    return _Proposal(
        key="fightLengthSeconds",
        label="Fight length",
        value=int(round(duration.median)),
        summary=f"{duration.median:.0f}s",
        evidence=(
            f"kill time {_spread_text(duration, 0, 's')}, from the report's own "
            f"start and end times -- metadata, so a bounded event fetch does not "
            f"affect it"
        ),
    )


def _raid_size_promotion(observation) -> _Proposal | None:
    size = getattr(observation, "raid_size", None)
    if size is None:
        return None
    withheld = (
        None
        if size.low == size.high
        else f"the sampled fights logged different group sizes ({size.low:g}-{size.high:g})"
    )
    return _Proposal(
        key="raidSize",
        label="Raid size",
        value=int(round(size.median)),
        summary=f"{size.median:.0f} players",
        evidence=(
            f"the log's own group size: {_spread_text(size)}. Metadata on the "
            f"fight, so unaffected by how much of the event stream was fetched"
        ),
        withheld=withheld,
    )


def _amplification_promotion(profile: FightProfile, observation) -> _Proposal | None:
    """Fill in an amplification's ability id and carrier, and nothing else.

    This is the answer to "which of the three targets does it sit on", written
    into the profile instead of asked of a person again. What it deliberately does
    not touch: ``multiplier`` (no log can ever supply it), ``first`` and
    ``duration`` (a person stated those; the measured window is printed beside
    them for comparison and that is where it stops).
    """
    amplifications = profile.amplifications
    if not amplifications:
        return None
    candidates = observation.pooled_auras()
    if not candidates:
        return None

    filled: list[dict] = []
    changes: list[str] = []
    evidence: list[str] = []
    for amplification in amplifications:
        match, _ = _match_amplification(amplification, candidates)
        payload = amplification.to_json()
        if match is None:
            filled.append(payload)
            continue

        if amplification.ability_id is None and isinstance(match.get("abilityId"), int):
            payload["abilityId"] = match["abilityId"]
            changes.append(
                f"{amplification.ability!r}: ability id {match['abilityId']} "
                f"({match.get('ability')})"
            )

        role = measured_target(match)
        if amplification.target == TARGET_UNKNOWN and role is not None:
            payload["target"] = role
            payload["targetSource"] = SOURCE_LOGS
            payload["targetEvidence"] = match.get("roleEvidence") or ""
            changes.append(f"{amplification.ability!r}: carried by the {role} target")

        named = carrier_text(match)
        if named:
            evidence.append(
                f"{amplification.ability!r} matched {match.get('ability')!r} "
                f"(id {match.get('abilityId')}), carried by {named}, "
                f"seen in {match.get('seenInFights')} fight(s)"
            )
        filled.append(payload)

    if not changes:
        return None

    return _Proposal(
        key="amplifications",
        label="Damage amplification",
        value=filled,
        summary="; ".join(changes),
        evidence=(
            "; ".join(evidence)
            + ". The multiplier, the start and the duration are left exactly as "
            "asserted: no field in the Warcraft Logs API says what an aura does."
        ),
        fills_blanks=True,
    )


def _decide(
    profile: FightProfile,
    proposal: _Proposal,
    *,
    sample: int,
    reports: tuple[str, ...],
    min_fights: int,
) -> Promotion:
    stored = profile.facts.get(proposal.key)
    current = stored.value if stored is not None else None
    source = stored.provenance.source if stored is not None else SOURCE_DEFAULT
    # A blanks-fill changes the stored value and contradicts nothing in it, so it
    # is not a disagreement. Reporting it as one would put the loudest word on the
    # page next to the one case where profile and logs are in complete accord.
    #
    # Nor is a withheld measurement a disagreement. "The logs say two where you say
    # three" is the finding this project cares most about, and spending that
    # sentence on a number the fetch never finished reading would teach a reader to
    # ignore it by the third time.
    disagrees = (
        stored is not None
        and not proposal.fills_blanks
        and not proposal.withheld
        and current != proposal.value
    )

    def promotion(eligible: bool, reason: str, blocked_by: str | None = None) -> Promotion:
        return Promotion(
            key=proposal.key,
            label=proposal.label,
            value=proposal.value,
            summary=proposal.summary,
            evidence=proposal.evidence,
            sample=sample,
            reports=reports,
            eligible=eligible,
            reason=reason,
            blocked_by=blocked_by,
            current=current,
            disagrees=disagrees,
        )

    # A person's statement is checked first because it is the permanent answer:
    # the measurement's own condition can change on the next run, and this cannot.
    if source == SOURCE_HAND and not proposal.fills_blanks:
        reason = "a person stated this, and a measurement never overwrites a person. "
        if proposal.withheld:
            reason += (
                f"This measurement is in no condition to argue with it in any case: "
                f"{proposal.withheld}."
            )
        elif disagrees:
            reason += (
                "The two disagree, which under this project's rule means the "
                "extraction is the likelier culprit -- fix that before editing the "
                "profile."
            )
        else:
            reason += "The two agree, so there is nothing to gain by rewriting it."
        return promotion(False, reason, blocked_by=SOURCE_HAND)
    if sample < min_fights:
        return promotion(
            False,
            f"{sample} sampled fight(s), below the floor of {min_fights}: not enough to "
            f"tell an encounter's shape from one guild's pull",
        )
    if proposal.withheld:
        return promotion(False, proposal.withheld)
    if proposal.fills_blanks:
        return promotion(
            True,
            "fills in fields the profile left blank; every value a person stated, "
            "including the multiplier, is left untouched",
        )
    if source == SOURCE_LOGS:
        return promotion(True, "supersedes an earlier measurement of the same fact")
    return promotion(True, "nothing is recorded for this fact, and the logs measured it")


# --------------------------------------------------------------------------------
# The data file
# --------------------------------------------------------------------------------


def _data_file() -> Path:
    return Path(str(resources.files("wowdps.data").joinpath("fight_profiles.json")))


@dataclass(frozen=True)
class TierProfiles:
    tier: str
    note: str
    profiles: dict[int, FightProfile]

    def get(self, encounter_id: int) -> FightProfile | None:
        return self.profiles.get(encounter_id)


def load_profiles(tier: str, path: Path | None = None) -> TierProfiles:
    """Read the fight profiles for one tier.

    A tier with no entry is not an error: a profile file that has not caught up
    with a new raid should fall back to the target sweep the site already
    publishes, not stop the run.
    """
    source = path or _data_file()
    raw = json.loads(source.read_text(encoding="utf-8"))
    entry = (raw.get("tiers") or {}).get(tier) or {}
    difficulty = int(entry.get("difficulty", 5))

    profiles: dict[int, FightProfile] = {}
    for encounter in entry.get("encounters") or []:
        encounter_id = int(encounter["encounterId"])
        profiles[encounter_id] = FightProfile(
            tier=tier,
            encounter_id=encounter_id,
            name=encounter.get("name", str(encounter_id)),
            difficulty=int(encounter.get("difficulty", difficulty)),
            facts={
                key: Fact.from_json(value)
                for key, value in (encounter.get("facts") or {}).items()
                if isinstance(value, dict)
            },
        )

    return TierProfiles(tier=tier, note=raw.get("note", ""), profiles=profiles)


def boss_scenarios(profiles: TierProfiles) -> dict[str, Scenario]:
    """Runnable scenarios for the tier's bosses, keyed by scenario id.

    Only bosses something is actually known about: a profile of pure fallbacks
    would sim as Patchwerk at one target under a boss's name, which is worse than
    publishing nothing for that boss. ``FightProfile.has_facts`` is the test, and
    it is the same one the Fights view uses to decide whether a boss reads as
    asserted or as unknown.

    Ordered by encounter id so a run over "every boss" is reproducible.
    """
    return {
        profile.to_scenario().id: profile.to_scenario()
        for _, profile in sorted(profiles.profiles.items())
        if profile.has_facts
    }


def target_counts_for(profiles: TierProfiles) -> tuple[int, ...]:
    """The distinct target counts the tier's profiles need simmed.

    Useful for pruning: there is no reason to sim ten target counts when the
    tier's nine bosses between them ask for three.
    """
    counts = {profile.baseline_targets for profile in profiles.profiles.values()}
    return tuple(sorted(counts)) or DEFAULT_TARGET_COUNTS
