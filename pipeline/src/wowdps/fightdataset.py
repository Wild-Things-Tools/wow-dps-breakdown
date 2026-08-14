"""Publishing per-boss fight shapes as ``<tier>/fights.json``, for the site to draw.

What this file is for
---------------------
``fightprofile`` holds what somebody asserts about a boss, ``fightextract`` holds
what the logs measured, and ``fightprobe`` prints both to a terminal in CI. None of
that is checkable by the person who actually plays these fights. This module turns
the pair into a dataset the web app renders, so the machine's reading of each
encounter can be looked at and contradicted.

That is the whole purpose: a **verification surface**. Nothing here resolves a
disagreement between an assertion and a measurement -- it publishes both, side by
side, with the sample size and the sampling bias attached to the measured half.

Two inputs, either of which may be missing
------------------------------------------
* The tier's fight profiles. Always present (they ship in the package), and for
  most bosses they are empty -- which is the honest state and is published as such.
* A ``fight-probe-<tier>.json`` payload from a probe run. Absent in a development
  checkout, because the Warcraft Logs credentials are Actions secrets. Without it
  every ``measured`` block is ``null`` and the site says "never measured" rather
  than showing a default that looks like a finding.

So ``build_document`` is a pure function over (profiles, probe payload or None),
and the published file can be regenerated offline from a probe artifact downloaded
out of CI.

How the target-count timeline is pooled: it is not
--------------------------------------------------
A step function of "how many things are alive" is a *shape*, and the shape is the
thing being put in front of somebody to confirm. Averaging several pulls per second
would produce a curve that no pull had: a wave that arrives at 88s in one log and
96s in another comes out as a two-step ramp, which is exactly the artefact that
would make a correct extraction look wrong. It is also the failure this project
already documented for simc's own timelines -- past the shortest fight the mean is
drawn from fewer and fewer samples and stops being comparable.

So the published timeline is **one representative fight, whole**, plus every other
sampled fight's own steps carried alongside for the view to draw faintly. The
representative is the sampled fight whose time-weighted mean target count sits
nearest the pooled median (ties broken on fight length). The *summary* claims --
mean targets, peak targets, share of the fight at peak -- stay pooled as
``Spread``s with their min/median/max, which is where an aggregate belongs.

Phase and aura windows come in two forms for the same reason. The representative
fight's own windows are published with it, because those are the only ones that
line up with the steps being drawn; the pooled ones are published in the measured
block as spreads, because that is the claim about the encounter rather than about
one pull.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .fightextract import Spread
from .fightprofile import (
    SOURCE_DEFAULT,
    SOURCE_HAND,
    SOURCE_LOGS,
    FightProfile,
    TierProfiles,
)

#: Written to ``<tier>/fights.json``. Bumped independently of the spec dataset's
#: schema version: the two files change for unrelated reasons.
FIGHTS_SCHEMA_VERSION = 1

#: Fact keys the document always carries an entry for, even when the profile says
#: nothing about them. A missing fact publishes as ``source: "default"`` with the
#: reason it fell back, which is what makes "nothing is known about this boss"
#: legible instead of looking like a measured one-target fight.
FACT_KEYS: tuple[tuple[str, str], ...] = (
    ("targets", "Targets at the pull"),
    ("fightLengthSeconds", "Fight length"),
    ("raidSize", "Raid size"),
    ("addWaves", "Add waves"),
    ("amplifications", "Damage amplification"),
    ("phases", "Phases"),
)

SOURCE_LABELS = {
    SOURCE_HAND: "Asserted by hand",
    SOURCE_LOGS: "Measured from logs",
    SOURCE_DEFAULT: "Nothing known",
}

#: simc reschedules a raid event on its cooldown for as long as the fight runs, so
#: a one-shot is written as a cooldown longer than any fight. Anything at or above
#: this never recurs, and the projected timeline must not loop forever trying.
_ONE_SHOT_FLOOR = 10_000


# --------------------------------------------------------------------------------
# A probe payload, read back as something ``compare_to_measurement`` accepts
# --------------------------------------------------------------------------------


def _spread(raw: object) -> Spread | None:
    if not isinstance(raw, dict):
        return None
    median, low, high = raw.get("median"), raw.get("low"), raw.get("high")
    if median is None or low is None or high is None:
        return None
    return Spread(median=float(median), low=float(low), high=float(high), n=int(raw.get("n") or 0))


class MeasuredEncounter:
    """One encounter's measurements, read out of a probe payload rather than events.

    ``FightProfile.compare_to_measurement`` wants an ``EncounterObservation``, and
    rebuilding one from JSON would mean reconstructing every ``FightObservation``
    dataclass underneath it. It only ever asks for four things, so this presents
    those four over the pooled numbers the probe already wrote down. The upshot is
    that a published dataset can be rebuilt from a CI artifact with no network and
    no re-extraction.
    """

    def __init__(self, payload: dict):
        self.payload = payload

    @property
    def fights(self) -> list[dict]:
        return [fight for fight in self.payload.get("fights") or [] if isinstance(fight, dict)]

    @property
    def duration(self) -> Spread | None:
        return _spread(self.payload.get("durationSeconds"))

    @property
    def raid_size(self) -> Spread | None:
        return _spread(self.payload.get("raidSize"))

    @property
    def peak_targets(self) -> Spread | None:
        return _spread(self.payload.get("peakTargets"))

    def pooled_auras(self) -> list[dict]:
        return [aura for aura in self.payload.get("auras") or [] if isinstance(aura, dict)]


# --------------------------------------------------------------------------------
# The shape a scenario would run, as a step function
# --------------------------------------------------------------------------------


def scenario_target_steps(profile: FightProfile) -> list[list[float]]:
    """How many targets the *simulation* would have alive, second by second.

    Computed from the profile rather than by parsing the simc options back out,
    because the meaning of ``adds,count=,first=,duration=,cooldown=`` lives here.
    Published so the view can draw what the sim does against what the logs show
    on one axis -- which is the whole comparison, and the reason the profile
    exists.
    """
    plan = profile.to_plan()
    length = float(plan.max_time)
    deltas: dict[float, int] = {0.0: plan.targets}

    for wave in profile.add_waves:
        when = float(wave.first)
        # A wave with no cadence happens once; simc expresses that as a cooldown
        # longer than the fight, and either form has to terminate here.
        cadence = float(wave.cadence) if wave.cadence else float(_ONE_SHOT_FLOOR)
        while when < length:
            end = min(when + float(wave.duration), length)
            deltas[when] = deltas.get(when, 0) + wave.count
            deltas[end] = deltas.get(end, 0) - wave.count
            if cadence >= _ONE_SHOT_FLOOR or cadence <= 0:
                break
            when += cadence

    steps: list[list[float]] = []
    running = 0
    for when in sorted(deltas):
        running += deltas[when]
        if steps and steps[-1][1] == running:
            continue
        if steps and steps[-1][0] == when:
            steps[-1][1] = running
            continue
        steps.append([round(when, 3), running])
    return steps


# --------------------------------------------------------------------------------
# One encounter
# --------------------------------------------------------------------------------


def _fact_entries(profile: FightProfile) -> list[dict]:
    """Every well-known fact key with its provenance, present or not."""
    entries: list[dict] = []
    for key, label in FACT_KEYS:
        fact = profile.fact(key, None, _DEFAULT_REASONS.get(key, "nothing recorded"))
        # `fact()` invents a default provenance for a key the profile lacks, which
        # is exactly what should be published: "nothing known, and here is why the
        # value you see is what it is".
        stored = profile.facts.get(key)
        provenance = (stored or fact).provenance
        entries.append(
            {
                "key": key,
                "label": label,
                "source": provenance.source,
                "sourceLabel": SOURCE_LABELS.get(provenance.source, provenance.source),
                "summary": provenance.summary(),
                "detail": provenance.detail,
                "statedBy": provenance.stated_by,
                "observedAt": provenance.observed_at,
                "sample": provenance.sample,
                "reports": list(provenance.reports),
                "value": stored.value if stored is not None else None,
            }
        )
    return entries


#: Why a value exists when nothing was asserted. Mirrors the fallbacks the typed
#: readers in ``FightProfile`` use, so the published reason is the real one.
_DEFAULT_REASONS = {
    "targets": "no target count recorded; one target is what the site already assumes",
    "fightLengthSeconds": "no measured kill time; 300s is the length every other scenario uses",
    "raidSize": "raid size not recorded",
    "addWaves": "no add waves recorded",
    "amplifications": "no damage amplification recorded",
    "phases": "no phases recorded",
}


def _profile_block(profile: FightProfile) -> dict:
    targets = profile.targets.value
    # Null rather than False when nothing was asserted. The default fact carries
    # `constant: true` so that a scenario built from it is well-formed, but
    # publishing that as "this boss holds a constant target count" would turn a
    # fallback into a finding, which is the exact failure this view exists to
    # catch. Every value here is only as good as the `source` of its fact key.
    constant = (
        bool(targets.get("constant"))
        if "targets" in profile.facts and isinstance(targets, dict)
        else None
    )
    return {
        "baselineTargets": profile.baseline_targets,
        "constantTargets": constant,
        "fightLengthSeconds": profile.fight_length.value,
        "raidSize": profile.raid_size.value,
        "addWaves": [
            {
                "name": wave.name,
                "count": wave.count,
                "first": wave.first,
                "duration": wave.duration,
                "cadence": wave.cadence,
            }
            for wave in profile.add_waves
        ],
        "amplifications": [
            {
                "ability": amp.ability,
                "abilityId": amp.ability_id,
                "multiplier": amp.multiplier,
                "first": amp.first,
                "duration": amp.duration,
                "target": amp.target,
                "magnitudeSource": amp.magnitude_source,
                # The one number Warcraft Logs cannot supply at all, flagged where
                # a reader will see it rather than in a footnote.
                "magnitudeMeasurable": False,
                "representable": amp.simc_option() is not None,
            }
            for amp in profile.amplifications
        ],
        "phases": list(profile.phases),
    }


def _scenario_block(profile: FightProfile) -> dict:
    plan = profile.to_plan()
    return {
        **plan.to_json(),
        # Left null on purpose: naming a fight style makes simc clear the raid
        # events the scenario is built out of. Published so the view can say so.
        "fightStyle": None,
        "steps": scenario_target_steps(profile),
    }


def _representative(payload: dict) -> dict | None:
    """The sampled fight whose shape is nearest the pooled middle.

    Nearest on the time-weighted mean target count first, because that is what the
    chart is about; fight length breaks ties. Deliberately *a fight*, never an
    average of fights -- see the module docstring.
    """
    fights = [fight for fight in payload.get("fights") or [] if isinstance(fight, dict)]
    if not fights:
        return None

    target_median = ((payload.get("meanTargets") or {}).get("median")) or 0.0
    length_median = ((payload.get("durationSeconds") or {}).get("median")) or 0.0

    def distance(fight: dict) -> tuple[float, float, str, int]:
        mean = float((fight.get("significantTargetCount") or {}).get("mean") or 0.0)
        duration = float(fight.get("durationSeconds") or 0.0)
        return (
            abs(mean - float(target_median)),
            abs(duration - float(length_median)),
            str(fight.get("reportCode") or ""),
            int(fight.get("fightId") or 0),
        )

    return min(fights, key=distance)


def _drawable_auras(fight: dict, pooled: list[dict]) -> list[dict]:
    """The representative fight's aura windows, filtered to the pooled shortlist.

    A fight's own aura list is unfiltered: it carries every aura on every enemy,
    including debuffs players applied, because the per-fight payload does not
    record who applied what. ``pooled_auras`` already dropped player-sourced auras
    and one-off appearances, so its ability ids are the shortlist of things that
    might be an encounter mechanic. Drawing anything else would repeat the mistake
    that once nominated Avenging Wrath as a boss's amplification.
    """
    keep = {aura.get("abilityId") for aura in pooled}
    return [
        {
            "abilityId": aura.get("abilityId"),
            "ability": aura.get("ability"),
            "start": aura.get("start"),
            "duration": aura.get("duration"),
            "truncated": bool(aura.get("truncated")),
            "instance": aura.get("instance"),
        }
        for aura in fight.get("auras") or []
        if isinstance(aura, dict) and aura.get("abilityId") in keep
    ]


def _timeline_block(payload: dict) -> dict | None:
    chosen = _representative(payload)
    if chosen is None:
        return None

    pooled = [aura for aura in payload.get("auras") or [] if isinstance(aura, dict)]
    significant = chosen.get("significantTargetCount") or {}
    everything = chosen.get("targetCount") or {}

    others = [
        {
            "reportCode": fight.get("reportCode"),
            "fightId": fight.get("fightId"),
            "durationSeconds": fight.get("durationSeconds"),
            "kill": bool(fight.get("kill")),
            "steps": (fight.get("significantTargetCount") or {}).get("steps") or [],
        }
        for fight in payload.get("fights") or []
        if isinstance(fight, dict) and fight is not chosen
    ]

    return {
        "pooling": "representative",
        "why": (
            "One sampled pull, drawn whole. A per-second median across pulls of "
            "different lengths produces a shape no pull had -- a wave arriving at 88s "
            "in one log and 96s in another comes out as a two-step ramp -- and past "
            "the shortest fight it would be drawn from fewer and fewer samples. The "
            "pooled claims are the spreads beside the chart, not the curve."
        ),
        "chosenBecause": (
            "the sampled fight whose time-weighted mean target count sits nearest the "
            "pooled median, fight length breaking ties"
        ),
        "representative": {
            "reportCode": chosen.get("reportCode"),
            "fightId": chosen.get("fightId"),
            "kill": bool(chosen.get("kill")),
            "durationSeconds": chosen.get("durationSeconds"),
            "raidSize": chosen.get("raidSize"),
            "steps": significant.get("steps") or [],
            "mean": significant.get("mean"),
            "peak": significant.get("peak"),
            "peakShare": significant.get("peakShare"),
            "constant": significant.get("constant"),
            # Everything that took a hit, including props and strays under the
            # significance floor. "Three things exist" and "three things matter"
            # are different claims and the view can show both.
            "allEnemySteps": everything.get("steps") or [],
            "allEnemyPeak": everything.get("peak"),
            "phases": chosen.get("phases") or [],
            "auras": _drawable_auras(chosen, pooled),
            "truncated": bool(chosen.get("truncated")),
            "warnings": list(chosen.get("warnings") or []),
        },
        "others": others,
    }


def _caveats(payload: dict, rankings_page: int | None) -> list[str]:
    """Everything about the measurement that limits how far it can be read."""
    notes: list[str] = []
    fights = [fight for fight in payload.get("fights") or [] if isinstance(fight, dict)]

    if len(fights) < 3:
        notes.append(
            f"{len(fights)} fight(s) sampled: too few to tell an encounter's shape from "
            f"one guild's pull."
        )
    if any(fight.get("truncated") for fight in fights):
        notes.append(
            "At least one event fetch stopped at its page limit, so the tail of that "
            "fight is incomplete rather than absent."
        )
    if rankings_page == 1:
        notes.append(
            "Sampled from page 1 of the rankings: the world's best pulls, which are "
            "shorter than a typical kill and kill adds faster. Probe with --page for a "
            "more ordinary sample."
        )
    for warning in sorted({w for fight in fights for w in (fight.get("warnings") or [])}):
        notes.append(str(warning))
    return notes


def _comparison(profile: FightProfile, measured: MeasuredEncounter | None) -> list[dict]:
    """Asserted against measured, resolving nothing.

    ``delta`` is arithmetic, not a verdict: it exists so a reader can see the size
    of a disagreement without the file having decided whether it matters. There is
    deliberately no "agrees" flag -- a fight profile that says 300s against a
    measured 288s is not wrong, and a boss measured at 2 targets where the owner
    plays 3 means the extraction is probably broken. Both need a person.
    """
    if measured is None:
        return []
    rows = profile.compare_to_measurement(measured)
    for row in rows:
        left, right = row.get("profile"), row.get("measured")
        row["delta"] = (
            round(float(right) - float(left), 3)
            if isinstance(left, (int, float))
            and isinstance(right, (int, float))
            and not isinstance(left, bool)
            and not isinstance(right, bool)
            else None
        )
    return rows


def _encounter_document(
    profile: FightProfile,
    payload: dict | None,
    rankings_page: int | None,
) -> dict:
    facts = _fact_entries(profile)
    measured = MeasuredEncounter(payload) if payload and payload.get("fights") else None

    measured_block = None
    if measured is not None and payload is not None:
        measured_block = {
            "fightsSampled": len(measured.fights),
            "reports": list(payload.get("reports") or []),
            "durationSeconds": payload.get("durationSeconds"),
            "raidSize": payload.get("raidSize"),
            "playersListed": payload.get("playersListed"),
            "meanTargets": payload.get("meanTargets"),
            "peakTargets": payload.get("peakTargets"),
            "peakTargetShare": payload.get("peakTargetShare"),
            "activeTimeFraction": payload.get("activeTimeFraction"),
            "adds": payload.get("adds") or [],
            "auras": payload.get("auras") or [],
            "phases": payload.get("phases") or [],
            "truncated": any(fight.get("truncated") for fight in measured.fights),
            "caveats": _caveats(payload, rankings_page),
            "timeline": _timeline_block(payload),
        }
    elif payload is not None:
        # The probe looked and found nothing. That is a different state from never
        # having looked, and reads differently on the page.
        measured_block = {
            "fightsSampled": 0,
            "reports": list(payload.get("reports") or []),
            "caveats": ["The probe read no fights for this encounter."],
            "timeline": None,
        }

    return {
        "encounterId": profile.encounter_id,
        "name": profile.name,
        "difficulty": profile.difficulty,
        "hasFacts": any(entry["source"] != SOURCE_DEFAULT for entry in facts),
        "facts": facts,
        "profile": _profile_block(profile),
        "scenario": _scenario_block(profile),
        "measured": measured_block,
        "comparison": _comparison(profile, measured),
    }


# --------------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------------


def build_document(
    tier: str,
    profiles: TierProfiles,
    probe: dict | None = None,
    *,
    generated_at: str | None = None,
) -> dict:
    """The published fight dataset for one tier.

    ``probe`` is a decoded ``fight-probe-<tier>.json``, or ``None``. Without one
    every encounter publishes its assertions and its scenario with ``measured``
    null, which is the state a development checkout is in and a perfectly
    publishable one -- the gap is the point.
    """
    by_encounter: dict[int, dict] = {}
    measurement: dict | None = None
    rankings_page: int | None = None

    if probe:
        for entry in probe.get("encounters") or []:
            if isinstance(entry, dict) and isinstance(entry.get("encounterId"), int):
                by_encounter[entry["encounterId"]] = entry
        rankings_page = probe.get("rankingsPage")
        measurement = {
            "generatedAt": probe.get("generatedAt"),
            "difficulty": probe.get("difficulty"),
            "metric": probe.get("metric"),
            "reportsPerEncounter": probe.get("reportsPerEncounter"),
            "rankingsPage": rankings_page,
            "eventStreams": probe.get("eventStreams") or [],
            "significantDamageShare": probe.get("significantDamageShare"),
            "samplingBias": probe.get("sampling"),
            "abortedBecause": probe.get("abortedBecause"),
            "cost": probe.get("cost"),
        }

    known = dict(profiles.profiles)
    # A probe of an encounter the profile file has never heard of still publishes:
    # a new raid tier arrives as measurements before anybody writes facts down.
    for encounter_id, entry in by_encounter.items():
        if encounter_id not in known:
            known[encounter_id] = FightProfile(
                tier=tier,
                encounter_id=encounter_id,
                name=str(entry.get("encounterName") or encounter_id),
                difficulty=int(entry.get("difficulty") or 5),
            )

    encounters = [
        _encounter_document(profile, by_encounter.get(encounter_id), rankings_page)
        for encounter_id, profile in sorted(known.items())
    ]

    asserted = sum(1 for entry in encounters if entry["hasFacts"])
    measured = sum(
        1 for entry in encounters if (entry["measured"] or {}).get("fightsSampled", 0) > 0
    )

    return {
        "schemaVersion": FIGHTS_SCHEMA_VERSION,
        "generatedAt": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": tier,
        "note": profiles.note,
        "measurement": measurement,
        # Same discipline as the gear sweep: a dataset covering one boss of nine is
        # a useful thing to publish and a misleading thing to publish silently.
        "coverage": {
            "encounters": len(encounters),
            "asserted": asserted,
            "measured": measured,
        },
        "encounters": encounters,
    }


#: Fields describing *when the file was written* rather than what is in it.
_PROVENANCE = ("generatedAt",)


def write_fights(out_dir: Path, document: dict) -> Path:
    """Write ``<out_dir>/fights.json``, keeping the timestamp when nothing changed.

    Same reasoning as the manifest: a wall-clock timestamp that rewrites itself on
    every run means every run commits, and "a diff means something moved" stops
    being true. ``generatedAt`` reads as when the fight data last changed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "fights.json"

    settled = document
    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        published = None

    if published is not None and {k: v for k, v in published.items() if k not in _PROVENANCE} == {
        k: v for k, v in document.items() if k not in _PROVENANCE
    }:
        settled = dict(document)
        for key in _PROVENANCE:
            if key in published:
                settled[key] = published[key]

    path.write_text(json.dumps(settled, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def load_probe(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
