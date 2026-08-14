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
which takes a ``multiplier`` and, without a ``target=``, lands on the priority
target -- so a profile that says the buff goes on *an add* has nothing to map onto,
because simc's add names are generated rather than named by the profile. Anything
that cannot be expressed is returned in ``ScenarioPlan.unrepresented`` rather than
dropped: a scenario that silently models three quarters of an encounter is worse
than one that says which quarter is missing.

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
        )


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

    def to_json(self) -> dict:
        return {
            "encounterId": self.encounter_id,
            "name": self.name,
            "targets": self.targets,
            "maxTime": self.max_time,
            "options": list(self.options),
            "unrepresented": list(self.unrepresented),
            "asserted": list(self.asserted),
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

    # -- turning a profile into a simulation ---------------------------------------

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
        # damage taken is already an amplification. Anything else is genuinely
        # not modelled, and says so.
        for phase in self.phases:
            if phase.get("downtime"):
                unrepresented.append(
                    f"phase {phase.get('name')!r} has {phase['downtime']}s of stated "
                    f"downtime; no raid event expresses a target being unattackable "
                    f"for part of a fight without also moving the players"
                )

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
                f"amplification window(s). Built from a fight profile, not a fight style."
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


def target_counts_for(profiles: TierProfiles) -> tuple[int, ...]:
    """The distinct target counts the tier's profiles need simmed.

    Useful for pruning: there is no reason to sim ten target counts when the
    tier's nine bosses between them ask for three.
    """
    counts = {profile.baseline_targets for profile in profiles.profiles.values()}
    return tuple(sorted(counts)) or DEFAULT_TARGET_COUNTS
