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
*summary* claims -- mean targets, peak targets, share of the fight at peak -- stay
pooled as ``Spread``s with their min/median/max, which is where an aggregate belongs.

Which fight, though, is not always one question. Pulls of the same boss can be
genuinely different *shapes*: a wave killed before the next one spawns in half the
logs and overlapping in the other half is two patterns, not one pattern plus noise,
and picking the pull nearest the median then silently answers a question nobody
asked. So the sampled pulls are **clustered on their target-count curve** and every
pattern at least two pulls share is published as its own preset, ordered by how many
pulls it holds. The first is the one most kills looked like; it is what the view
opens on. A boss whose pulls all look alike yields exactly one pattern and the view
shows no chooser at all -- the clustering is what decides that, not a setting.

Comparison is on normalised fight time, because two pulls of the same shape at
different lengths are the same pattern and their length difference is already
published separately. A pull that matches nothing stays in ``others`` as context
rather than becoming a preset of one: with three pulls sampled, "one log did this"
is not a pattern.

Phase and aura windows come in two forms for the same reason. The representative
fight's own windows are published with it, because those are the only ones that
line up with the steps being drawn; the pooled ones are published in the measured
block as spreads, because that is the claim about the encounter rather than about
one pull.

Promotions: a measured fact, offered rather than applied
--------------------------------------------------------
Each encounter also carries ``promotions`` -- the facts the measurement could
contribute to the profile, each with the value, the evidence, whether it is
eligible, and what blocks it. They are *published, never applied*: writing one
into ``fight_profiles.json`` takes ``wowdps fight-promote --write``, and no
promotion ever overwrites a fact a person stated. See ``fightprofile`` for the
rules and ``fightpromote`` for the command. Publishing the proposal is what makes
the decision inspectable rather than something that happened in CI overnight.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .fightextract import COMPLETE_EVENT_COVERAGE, Spread
from .fightprofile import (
    SOURCE_DEFAULT,
    SOURCE_HAND,
    SOURCE_LOGS,
    FightProfile,
    TierProfiles,
    plan_promotions,
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
    def fights_sampled(self) -> int:
        return len(self.fights)

    @property
    def reports(self) -> list[str]:
        return [str(code) for code in self.payload.get("reports") or []]

    @property
    def duration(self) -> Spread | None:
        return _spread(self.payload.get("durationSeconds"))

    @property
    def raid_size(self) -> Spread | None:
        return _spread(self.payload.get("raidSize"))

    @property
    def peak_targets(self) -> Spread | None:
        return _spread(self.payload.get("peakTargets"))

    @property
    def peak_share(self) -> Spread | None:
        return _spread(self.payload.get("peakTargetShare"))

    @property
    def event_coverage(self) -> Spread | None:
        """``None`` for a payload written before coverage was measured at all.

        That is a meaningful absence rather than a missing field to default: a run
        that did not record how much of each fight it read cannot support a claim
        about a whole fight, and ``plan_promotions`` treats it as such.
        """
        return _spread(self.payload.get("eventCoverage"))

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
                **amp.to_json(),
                # Field-level provenance, published even when unset, so the view
                # never has to guess whether "unknown" means nobody looked or
                # nobody could tell.
                "targetSource": amp.target_source,
                "targetEvidence": amp.target_evidence,
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


#: Buckets a pull's target-count curve is resampled into before two are compared.
#: Sixty over a normalised fight is one bucket per five seconds of a five-minute
#: pull, which is finer than any add wave worth separating.
_PATTERN_BUCKETS = 60

#: Share of the fight two pulls may disagree on and still be the same shape.
#:
#: Deliberately *not* a mean absolute difference, which was the first attempt and is
#: wrong in a way the fixtures caught: a real add wave is large but short, so
#: averaging it over the fight buries it. Imperator Averzian peaks at seven targets
#: for 4% of its length -- six extra targets contributing 0.25 to a mean, under any
#: threshold coarse enough to ignore jitter. Counting *how much of the fight* the two
#: curves are a whole target apart separates "the wave came" from "it did not" while
#: staying blind to an add dying three seconds earlier.
#:
#: The number is calibrated, not derived, and the calibration is worth writing down
#: because it is the first thing to redo when the sample grows. Every pair of pulls
#: in the published MID2 probe (three per boss, nine bosses):
#:
#:     Lightblinded Vanguard   0.050  0.067  0.117    a constant-three fight
#:     Vorasius                0.000  0.117  0.117    single target throughout
#:     Chimaerus               0.033  0.333  0.367    two pulls alike, one not
#:     Belo'ren                0.150  0.317  0.400    two pulls alike, one not
#:     Midnight Falls          0.100  0.117  0.217
#:     Crown of the Cosmos     0.167  0.183  0.283
#:     Fallen-King Salhadaar   0.217  0.267  0.283    no two alike
#:     Vaelgor & Ezzorak       0.217  0.233  0.317    no two alike
#:     Imperator Averzian      0.383  0.433  0.667    no two alike
#:
#: Pulls of one shape sit at 0.00-0.15 and pulls of different shapes at 0.28-0.67,
#: so 0.20 falls in the gap. Two caveats on that: three pulls per boss is far too
#: few to call it a distribution, and a pull whose event fetch stopped early differs
#: from a complete one over exactly the part it never read -- raise the probe's
#: `--reports` before trusting a split, and check `coverage` on both pulls.
_PATTERN_DIFFERENCE = 0.20

#: Pulls that must share a shape before it is offered as a preset. One log doing
#: something is not a pattern -- it is one log, and it stays in ``others`` where the
#: view draws it as context behind the chosen curve.
_MIN_PATTERN_PULLS = 2


def _resample(steps: list, duration: float, buckets: int = _PATTERN_BUCKETS) -> list[float]:
    """A step function read at even fractions of the fight, so lengths compare.

    ``steps`` is ``[[time, count], ...]`` and holds its value until the next entry.
    Sampling at bucket midpoints of *normalised* time is what makes a 240s pull and
    a 300s pull of the same shape come out equal: their length difference is a
    published fact of its own and does not need to be a second pattern.
    """
    ordered = [
        (float(step[0]), float(step[1]))
        for step in steps
        if isinstance(step, (list, tuple)) and len(step) >= 2
    ]
    if not ordered or duration <= 0:
        return [0.0] * buckets
    ordered.sort(key=lambda entry: entry[0])

    curve: list[float] = []
    index = 0
    value = 0.0
    for bucket in range(buckets):
        when = duration * (bucket + 0.5) / buckets
        while index < len(ordered) and ordered[index][0] <= when:
            value = ordered[index][1]
            index += 1
        curve.append(value)
    return curve


def _shape_difference(left: list[float], right: list[float]) -> float:
    """Share of the fight on which two curves are at least one whole target apart."""
    if not left or not right:
        return 1.0
    apart = sum(1 for a, b in zip(left, right, strict=True) if abs(a - b) >= 1.0)
    return apart / len(left)


def _cluster_fights(fights: list[dict]) -> list[list[dict]]:
    """Group pulls whose target-count curves are the same shape.

    Single-link agglomerative, done as connected components over "within the
    threshold of each other": with three to a dozen pulls per encounter anything
    heavier would be fitting parameters to noise, and single link has the property
    that matters here -- a slow drift across many pulls stays one pattern rather
    than being cut at an arbitrary point.

    Order is by size, then by the earliest report code in the group, so the same
    payload always yields the same patterns in the same order.
    """
    curves = [
        _resample(
            (fight.get("significantTargetCount") or {}).get("steps") or [],
            float(fight.get("durationSeconds") or 0.0),
        )
        for fight in fights
    ]

    parent = list(range(len(fights)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(fights)):
        for right in range(left + 1, len(fights)):
            if _shape_difference(curves[left], curves[right]) < _PATTERN_DIFFERENCE:
                parent[find(left)] = find(right)

    groups: dict[int, list[dict]] = {}
    for index, fight in enumerate(fights):
        groups.setdefault(find(index), []).append(fight)

    def order(group: list[dict]) -> tuple[int, str]:
        return (-len(group), min(str(fight.get("reportCode") or "") for fight in group))

    return sorted(groups.values(), key=order)


def _widest_difference(group: list[dict]) -> float:
    """The largest pairwise shape difference inside one pattern."""
    curves = [
        _resample(
            (fight.get("significantTargetCount") or {}).get("steps") or [],
            float(fight.get("durationSeconds") or 0.0),
        )
        for fight in group
    ]
    widest = 0.0
    for left in range(len(curves)):
        for right in range(left + 1, len(curves)):
            widest = max(widest, _shape_difference(curves[left], curves[right]))
    return round(widest, 3)


def _nearest_to_centre(group: list[dict], length_median: float = 0.0) -> dict:
    """The pull in a group that looks most like the group.

    Its own centre, not the pooled one: a pattern's representative has to be a pull
    that pattern actually contains, or the preset draws a curve from one shape and
    labels it with another.

    Length is the tiebreak, and it earns its place -- inside a pattern the curves
    are by construction near-identical, so without it the choice would fall through
    to the report code and the published pull would be whichever log happens to sort
    first. Nearest the pooled median length is the same rule the single-representative
    version used, which is why a boss whose pulls all agree keeps publishing the
    pull it published before.
    """
    curves = {
        id(fight): _resample(
            (fight.get("significantTargetCount") or {}).get("steps") or [],
            float(fight.get("durationSeconds") or 0.0),
        )
        for fight in group
    }

    def spread(fight: dict) -> tuple[float, float, str, int]:
        own = curves[id(fight)]
        total = sum(_shape_difference(own, curves[id(other)]) for other in group)
        length = abs(float(fight.get("durationSeconds") or 0.0) - length_median)
        return (
            round(total, 6),
            length,
            str(fight.get("reportCode") or ""),
            int(fight.get("fightId") or 0),
        )

    return min(group, key=spread)


#: Bands one ability may contribute to the chart before the rest are dropped.
#: A shaded band is a heavy mark; a dozen of them is a wash, and the published
#: MID2 run drew roughly two hundred (a Death Knight disease on fifteen enemy
#: instances) which covered the step lines completely.
_MAX_BANDS_PER_ABILITY = 3

#: Share of the pull above which an aura is reported rather than drawn as a band.
#: Two thirds, and the choice is about what a band *means*: it marks a stretch that
#: differs from the rest of the fight, so an aura covering most of the fight has no
#: such stretch to mark. Lightblinded Vanguard's `Light Infused` runs 285s of a 285s
#: pull; shading it is shading the plot.
_PERMANENT_AURA_SHARE = 0.66


def _merge_windows(windows: list[dict]) -> list[dict]:
    """Overlapping windows of one ability, merged into the intervals they cover.

    Five copies of an add carrying the same buff at the same time is *one* thing
    happening to the encounter, and drawing it five times says nothing the first
    band did not, at five times the ink. Merging is on the interval, so two
    genuinely separate applications stay two bands.
    """
    ordered = sorted(windows, key=lambda window: window["start"])
    merged: list[dict] = []
    for window in ordered:
        end = window["start"] + window["duration"]
        if merged and window["start"] <= merged[-1]["start"] + merged[-1]["duration"]:
            last = merged[-1]
            last["duration"] = round(max(last["start"] + last["duration"], end) - last["start"], 3)
            last["instances"] += 1
            if window.get("actorName") and window["actorName"] not in last["actorNames"]:
                last["actorNames"].append(window["actorName"])
            last["truncated"] = last["truncated"] or window["truncated"]
            continue
        merged.append(
            {
                **window,
                "instances": 1,
                "actorNames": [window["actorName"]] if window.get("actorName") else [],
            }
        )
    return merged


def _drawable_auras(fight: dict, pooled: list[dict]) -> list[dict]:
    """The representative fight's aura windows, filtered and merged for drawing.

    Three reductions, in order, each of which the published MID2 run needed:

    1. **Filtered to the pooled shortlist.** A fight's own aura list is
       unfiltered -- every aura on every actor, including ones players applied,
       because the per-fight payload does not record who applied what.
       ``pooled_auras`` already dropped those and one-off appearances, so its
       ability ids are the shortlist of things that might be an encounter
       mechanic. Drawing anything else repeats the mistake that once nominated
       Avenging Wrath as a boss's amplification.
    2. **Merged where they overlap.** One buff on five copies of an add is one
       thing happening, not five bands.
    3. **Capped per ability.** A shaded band is a heavy mark and a periodic aura
       has no natural limit. The count that was dropped travels with the band, so
       the chart says "and 27 more" rather than quietly showing three.

    Each window also carries ``permanent``: an aura up for essentially the whole
    pull is not a *window*, because a band marks a stretch that differs from the
    rest of the fight and this one has none. The view names those under the chart
    instead of shading them. This is the same wash the per-ability cap exists to
    prevent, arriving by a different route, and it appeared the moment the probe
    sampled six pulls rather than three and more of the encounter's own long buffs
    survived the aura filter.
    """
    keep = {aura.get("abilityId") for aura in pooled}
    length = float(fight.get("durationSeconds") or 0.0)
    by_ability: dict[object, list[dict]] = {}
    for aura in fight.get("auras") or []:
        if not isinstance(aura, dict) or aura.get("abilityId") not in keep:
            continue
        by_ability.setdefault(aura["abilityId"], []).append(
            {
                "abilityId": aura.get("abilityId"),
                "ability": aura.get("ability"),
                "start": float(aura.get("start") or 0.0),
                "duration": float(aura.get("duration") or 0.0),
                "truncated": bool(aura.get("truncated")),
                # Which enemy this window was on, so the band can be labelled with
                # a name rather than an ability floating over a three-target chart.
                "actorName": aura.get("actorName"),
                "role": aura.get("role"),
                "permanent": (
                    length > 0
                    and float(aura.get("duration") or 0.0) / length >= _PERMANENT_AURA_SHARE
                ),
            }
        )

    drawn: list[dict] = []
    for windows in by_ability.values():
        merged = _merge_windows(windows)
        shown, hidden = merged[:_MAX_BANDS_PER_ABILITY], len(merged) - _MAX_BANDS_PER_ABILITY
        for window in shown:
            window["alsoAtOtherTimes"] = max(hidden, 0)
            drawn.append(window)
    drawn.sort(key=lambda window: (window["start"], window["abilityId"]))
    return drawn


def _context_pull(fight: dict) -> dict:
    """One pull as the view draws it behind the chosen curve."""
    return {
        "reportCode": fight.get("reportCode"),
        "fightId": fight.get("fightId"),
        "durationSeconds": fight.get("durationSeconds"),
        "kill": bool(fight.get("kill")),
        "steps": (fight.get("significantTargetCount") or {}).get("steps") or [],
        "coverage": fight.get("eventCoverage"),
    }


def _pull_block(fight: dict, pooled: list[dict]) -> dict:
    """One pull, whole, as a preset draws it."""
    significant = fight.get("significantTargetCount") or {}
    everything = fight.get("targetCount") or {}
    return {
        "reportCode": fight.get("reportCode"),
        "fightId": fight.get("fightId"),
        "kill": bool(fight.get("kill")),
        "durationSeconds": fight.get("durationSeconds"),
        "raidSize": fight.get("raidSize"),
        "steps": significant.get("steps") or [],
        "mean": significant.get("mean"),
        "peak": significant.get("peak"),
        "peakShare": significant.get("peakShare"),
        "constant": significant.get("constant"),
        "observed": significant.get("observed"),
        "coverage": fight.get("eventCoverage"),
        # Everything that took a hit, including props and strays under the
        # significance floor. "Three things exist" and "three things matter"
        # are different claims and the view can show both.
        "allEnemySteps": everything.get("steps") or [],
        "allEnemyPeak": everything.get("peak"),
        "phases": fight.get("phases") or [],
        "auras": _drawable_auras(fight, pooled),
        "truncated": bool(fight.get("truncated")),
        "warnings": list(fight.get("warnings") or []),
    }


#: Words for how many times a shape reaches its own peak. Past this many the count
#: is written as a number, because "peaks at 3 seven times" reads as a measurement
#: and "peaks at 3 many times" reads as a shrug.
_TIMES = {1: "once", 2: "twice", 3: "three times", 4: "four times"}


def _peak_visits(steps: list, peak: float) -> int:
    """How many separate times the target count rises to the shape's own peak.

    This is what tells two patterns apart when their peak and mean do not. Vaelgor
    & Ezzorak's six sampled pulls split into a 237s kill that reaches three targets
    once and a 337s kill that reaches three twice; both label as "peaks at 3, 2.1 on
    average" without it, and a chooser offering two identical-looking options is
    worse than no chooser.
    """
    visits = 0
    above = False
    for step in steps:
        if not isinstance(step, (list, tuple)) or len(step) < 2:
            continue
        at_peak = float(step[1]) >= peak
        if at_peak and not above:
            visits += 1
        above = at_peak
    return visits


def _pattern_label(pull: dict) -> str:
    """A factual name for a shape, never an interpretation of it.

    "Three targets throughout" is a reading somebody could disagree with; "peaks at
    3 twice, 2.2 on average" is what was measured. The view writes the sentence.
    """
    peak = pull.get("peak")
    mean = pull.get("mean")
    if peak is None or mean is None:
        return "unmeasured shape"
    if pull.get("constant"):
        return f"{int(peak)} target{'' if int(peak) == 1 else 's'} throughout"
    visits = _peak_visits(pull.get("steps") or [], float(peak))
    how_often = _TIMES.get(visits, f"{visits} times") if visits else ""
    return (
        f"peaks at {int(peak)}{f' {how_often}' if how_often else ''}, {float(mean):.1f} on average"
    )


#: Two sampled pulls are one kill uploaded twice when their lengths agree to within
#: this many seconds **and** their target-count curves agree step for step.
#:
#: Warcraft Logs indexes uploads, not raid nights. Six people in one raid who each
#: run a logger produce six reports carrying the same kills, and the sampler picks
#: kills per report -- so one kill arrives as six rows, and nothing in a row says
#: which raid it came from. What does say it is the clock.
#:
#: Measured over the committed MID2 ``fights.json`` on 2026-08-31, 145 sampled rows
#: across ten (encounter, difficulty) pairs: sorting each pair's pulls by length, the
#: 65 closest consecutive gaps are all under **0.082 s** and the next smallest gap is
#: **1.074 s**. That is an order-of-magnitude break with nothing in it, and the
#: threshold sits inside it -- six times the widest duplicate spread observed, half
#: the distance to the closest pair of genuinely different kills. Calibrated, not
#: derived, exactly like ``_PATTERN_DIFFERENT_SHARE`` above; redo it when the sample
#: grows.
_DUPLICATE_UPLOAD_SECONDS = 0.5


def _fight_steps(fight: dict) -> list:
    """The significant target-count curve of a payload fight row."""
    return (fight.get("significantTargetCount") or {}).get("steps") or []


def _curves_agree(left: list, right: list, tolerance: float = _DUPLICATE_UPLOAD_SECONDS) -> bool:
    """Do two pulls' target-count curves describe the same events?

    Length alone is circumstantial -- two guilds can kill a boss in the same number
    of seconds -- so the step function has to agree too: the same number of
    transitions, each to exactly the same count, each within ``tolerance`` of the
    other's time. Two uploads of one kill differ only by the recording clients'
    clocks, which on MID2's Vashnik is about 30 ms across six uploads; two different
    kills do not line up at all.

    The count test is equality rather than a tolerance on purpose. It is an integer
    and the whole question is whether these rows describe the same events, so a
    curve that reaches three targets where the other reaches two is a different
    fight however well the times match.
    """
    if len(left) != len(right):
        return False
    for step, other in zip(left, right, strict=True):
        if not isinstance(step, (list, tuple)) or not isinstance(other, (list, tuple)):
            return False
        if len(step) < 2 or len(other) < 2:
            return False
        if abs(float(step[0]) - float(other[0])) > tolerance:
            return False
        if float(step[1]) != float(other[1]):
            return False
    return True


def group_duplicate_uploads(fights: list[dict]) -> list[list[dict]]:
    """Partition sampled pulls into one group per distinct kill.

    A kill logged once is a group of one, so the result is always a partition of the
    input and a payload with no duplicates comes back unchanged in shape.

    The grouping is anchored rather than transitive: a row joins a group by matching
    that group's **first** member, so a chain of near-misses cannot walk a group
    across a gap wider than the threshold. Rows are sorted by length first, so the
    answer does not depend on the order the probe happened to write them in.

    One structural guard: **a report cannot contain the same kill twice**, so two
    rows sharing a report code are two pulls however alike they look. Checked
    against the whole committed MID2 document -- over 34 multi-member groups
    spanning 93 rows it never fired once, which makes it an independent
    confirmation of the length-and-curve rule rather than a guard doing work.
    """
    ordered = sorted(
        (fight for fight in fights if isinstance(fight, dict)),
        key=lambda fight: (
            float(fight.get("durationSeconds") or 0.0),
            str(fight.get("reportCode") or ""),
            fight.get("fightId") if isinstance(fight.get("fightId"), int) else -1,
        ),
    )
    groups: list[list[dict]] = []
    for fight in ordered:
        duration = float(fight.get("durationSeconds") or 0.0)
        for group in groups:
            anchor = group[0]
            if (
                abs(duration - float(anchor.get("durationSeconds") or 0.0))
                > _DUPLICATE_UPLOAD_SECONDS
            ):
                continue
            if not _curves_agree(_fight_steps(fight), _fight_steps(anchor)):
                continue
            codes = {str(member.get("reportCode") or "") for member in group}
            if str(fight.get("reportCode") or "") in codes:
                continue
            group.append(fight)
            break
        else:
            groups.append([fight])
    return groups


def distinct_kills(fights: list[dict]) -> list[dict]:
    """One row per distinct kill: the upload of it that was read furthest.

    Uploads of one kill are near-identical by construction, so the choice barely
    moves a number -- but it must be deterministic, and it must prefer the copy
    whose event fetch got furthest, because that is the one whose tail is real.
    """
    return [
        max(
            group,
            key=lambda fight: (
                not fight.get("truncated"),
                float(fight.get("eventCoverage") or 0.0),
                str(fight.get("reportCode") or ""),
            ),
        )
        for group in group_duplicate_uploads(fights)
    ]


def _patterns(payload: dict, pooled: list[dict]) -> list[dict]:
    """Every shape at least ``_MIN_PATTERN_PULLS`` of the sampled pulls share.

    Ordered by how many pulls they hold, so the first is what most of these kills
    looked like. A boss whose pulls all agree yields one entry and the view shows no
    chooser; a boss with none -- every pull its own shape -- also yields one, built
    from the pull nearest the pooled middle, because a chart still has to draw
    something and "no two of these agreed" is what the caveats are for.
    """
    # Distinct kills, not sampled rows. Six uploads of one kill always cluster
    # together, so without this a single pull clears ``_MIN_PATTERN_PULLS`` on its
    # own and is published as "a shape five other pulls shared" -- which is the one
    # claim this function exists to make and the one thing it must not invent.
    fights = distinct_kills(
        [fight for fight in payload.get("fights") or [] if isinstance(fight, dict)]
    )
    if not fights:
        return []

    groups = [group for group in _cluster_fights(fights) if len(group) >= _MIN_PATTERN_PULLS]
    if not groups:
        chosen = _representative(payload)
        groups = [[chosen]] if chosen is not None else []

    length_median = float(((payload.get("durationSeconds") or {}).get("median")) or 0.0)
    patterns = []
    for index, group in enumerate(groups):
        chosen = _nearest_to_centre(group, length_median)
        members = {id(fight) for fight in group}
        pull = _pull_block(chosen, pooled)
        patterns.append(
            {
                "id": f"pattern-{index + 1}",
                "pulls": len(group),
                "share": round(len(group) / len(fights), 3),
                "label": _pattern_label(pull),
                # The widest disagreement inside this pattern, in share of the
                # fight. A pattern held together at 0.19 and one held together at
                # 0.02 are different claims, and the threshold alone hides that.
                "spread": _widest_difference(group),
                "representative": pull,
                # The pattern's own other members, for the view to draw behind the
                # chosen curve without mixing in pulls of a different shape.
                "alsoInThisPattern": [
                    _context_pull(fight) for fight in group if fight is not chosen
                ],
                "reportCodes": sorted(str(fight.get("reportCode") or "") for fight in group),
                "unmatched": [_context_pull(fight) for fight in fights if id(fight) not in members],
            }
        )
    return patterns


#: Buckets the aggregate band is sampled at over normalised fight time. Finer than
#: the pattern buckets because this is drawn as a curve, not compared as a shape.
_BAND_BUCKETS = 60

#: A fight read to less than this fraction is left out of the band: a partial fetch
#: reports the tail as an empty room, and one such curve drags the whole distribution
#: down at the times it never read. The band says how many it kept.
#: A kill enters the band only if its event fetch ran to the end of the fight.
#:
#: The test is the probe's own ``truncated`` flag -- set when a stream hit its page
#: limit -- and not a coverage ratio. Two reasons, and the second is why this was
#: wrong twice before:
#:
#: 1. ``eventCoverage`` is the *span of observed events* over the fight length, so a
#:    completely read kill whose first damage lands at 0.4s and whose last lands a
#:    second before the kill scores about 0.995, never 1.0. A "fully read" threshold
#:    expressed as a ratio therefore cannot be written down correctly.
#: 2. Low coverage is not always a gap. A raid that stops damaging an add halfway
#:    through leaves a genuine flat tail, and that is the encounter, not missing data.
#:    Excluding it would throw away a real observation.
#:
#: What a truncated fetch does is worse than reporting zeros: ``_resample`` carries
#: the last known value forward, so it *freezes* the target count at the cut point and
#: asserts it for the rest of the fight. The end of a kill is where adds die off, so a
#: partial read systematically **overstates** how many targets were up at the end --
#: holding flat exactly the fall the curve should show.
_BAND_NEEDS_COMPLETE_FETCH = True


def _percentile_at(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of an unsorted list of one bucket's counts."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _observed_a_target(fight: dict) -> bool:
    """Did this pull's event fetch ever see a target up?

    Separate from ``truncated``: a truncated fetch stopped part-way and its tail is
    wrong, while this one produced nothing at all and its whole curve is. Both are
    excluded from the band, for the same reason and by different tests.
    """
    steps = (fight.get("significantTargetCount") or {}).get("steps") or []
    for step in steps:
        count = step[1] if isinstance(step, (list, tuple)) and len(step) > 1 else None
        if count is None and isinstance(step, dict):
            count = step.get("count")
        try:
            if float(count or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _target_band(payload: dict) -> dict | None:
    """How many targets are up at each point of the fight, across every kill.

    The answer to "how many are normally up, and when", built the way that question
    should be answered: not from one representative pull but from the distribution
    over all of them. Each kill's target-count curve is resampled onto the same grid
    of *normalised* fight time -- so kills of slightly different length line up -- and
    at each point the band reports the median and the inter-quartile range across
    kills, plus the full min/max. A wide band at some moment means the kills genuinely
    disagreed there; a tight one means that many targets were reliably up.

    Only kills whose event fetch ran to the end are included -- see
    ``_BAND_NEEDS_COMPLETE_FETCH`` above for why that is the probe's ``truncated`` flag and
    not a coverage ratio. The count kept is published so a thin band is visible as
    thin.

    Normalised time is turned back into seconds for the reader by the median kill
    length, which is meaningful precisely because the user asked for kills whose
    timings are alike -- when they are, one length fits them all.

    **One kill is enough.** The floor used to be two, and on a boss whose Mythic
    field is still filling in that discards the only observation there is -- MID2's
    Sszorak is exactly that case, and the owner asked for it to be drawn (#48).
    A band over a single kill is arithmetically fine and degenerate: median, both
    quartiles and both extremes are that one curve, so it renders as a line with no
    spread, which is the honest picture of one observation. ``kills`` is published
    beside it, so a reader can see the band is built from one pull rather than
    inferring it from a shape that happens to be flat.

    The floor that matters is still enforced above and is a different one: a kill
    whose event fetch was truncated is excluded whatever the count, because that
    curve is wrong rather than thin.
    """
    candidates = [
        fight
        for fight in payload.get("fights") or []
        if isinstance(fight, dict) and not fight.get("truncated")
    ]
    # A pull the fetch read NOTHING from is not truncated -- the fetch did not stop
    # part-way, it never produced anything -- so the `truncated` test above lets it
    # through, and its curve resamples to zero across every bucket. Measured in the
    # committed MID2 data: The Lost Explorers at Mythic publishes a band over nine
    # kills whose min envelope is 0 at all sixty buckets, because one contributing
    # pull has a single step at zero targets. Eight real pulls reach 1-3.
    #
    # What it drags depends on how much the real curves disagree, and on THIS band
    # it is only the envelope: with nine curves Q1 is the third smallest and the
    # median the fifth, the eight real pulls all sit at 3 targets there, so the
    # published `low` and `median` are 3.0 at every bucket and only `min` is 0.0.
    # The zero curve occupies rank 0 and nothing else. That is luck rather than
    # safety -- it takes one rank from every statistic, so a band whose real pulls
    # spread across two or three targets loses its lower quartile as well.
    #
    # The test is "did this curve ever reach a target", not the pull's `coverage`,
    # and deliberately: a payload written before `TargetCountTimeline.window` was
    # fixed records `coverage: 1.0` for exactly these pulls, so filtering on it
    # would leave every existing payload contaminated. A kill in which no target was
    # ever damaged is not a kill, so this cannot exclude a legitimate pull.
    observed = [fight for fight in candidates if _observed_a_target(fight)]
    unobserved = len(candidates) - len(observed)

    # One kill logged by six people is one observation, not six. Left in, its curve
    # occupies six of the ranks every percentile below is taken over, so the band
    # reports a distribution of one kill as though six kills had agreed -- an IQR of
    # zero that reads as consensus. Measured on the committed MID2 document:
    # Vashnik at Mythic publishes a band over six "kills" that are six uploads of a
    # 434.78 s pull, and The Lost Explorers at Mythic gives one kill six votes
    # against another kill's two.
    #
    # Deduplication happens *after* the two exclusions above, deliberately. A pull
    # whose fetch read nothing has no curve to compare, so it could only ever be
    # grouped on length -- and it is already gone by here, which removes the
    # question instead of answering it with a special case.
    fights = distinct_kills(observed)
    duplicates = len(observed) - len(fights)
    if not fights:
        return None

    curves = [
        _resample(
            _fight_steps(fight),
            float(fight.get("durationSeconds") or 0.0),
            _BAND_BUCKETS,
        )
        for fight in fights
    ]
    lengths = sorted(float(fight.get("durationSeconds") or 0.0) for fight in fights)
    median_length = lengths[len(lengths) // 2]

    band = []
    for bucket in range(_BAND_BUCKETS):
        column = [curve[bucket] for curve in curves]
        band.append(
            {
                "t": round((bucket + 0.5) / _BAND_BUCKETS, 4),
                "second": round(median_length * (bucket + 0.5) / _BAND_BUCKETS, 1),
                "median": round(_percentile_at(column, 0.5), 2),
                "low": round(_percentile_at(column, 0.25), 2),
                "high": round(_percentile_at(column, 0.75), 2),
                "min": round(min(column), 2),
                "max": round(max(column), 2),
            }
        )

    block = {
        # Distinct kills, which is what a band is a distribution over. The
        # encounter's `fightsSampled` is the row count beside it, and
        # `duplicateUploads` below is what separates the two.
        "fights": len(fights),
        "buckets": _BAND_BUCKETS,
        "medianLengthSeconds": round(median_length, 1),
        "band": band,
        "why": (
            "The median and inter-quartile range of how many targets were up at each "
            "point of the fight, across every fully-read kill sampled -- not one "
            "representative pull. Time is normalised across kills and shown in seconds "
            "at the median kill length. A wide band is a moment the kills disagreed on."
        ),
    }
    # Published rather than silently dropped, the same rule the docstring states for
    # the truncated ones: a thinner band has to read as thinner, and a reader
    # comparing `fights` against the encounter's `fightsSampled` would otherwise see
    # two numbers disagree with nothing explaining it.
    if unobserved:
        block["unobservedKills"] = unobserved
    # Same rule, and the larger of the two on real data: a band drawn over one kill
    # that six people uploaded has to say so, or `fights: 1` beside
    # `fightsSampled: 6` reads as five kills silently discarded.
    if duplicates:
        block["duplicateUploads"] = duplicates
    return block


def _timeline_block(payload: dict) -> dict | None:
    pooled = [aura for aura in payload.get("auras") or [] if isinstance(aura, dict)]
    patterns = _patterns(payload, pooled)
    if not patterns:
        return None

    # The first pattern is the one most of these kills looked like, and it is what
    # the view opens on. `representative` and `others` are that pattern flattened,
    # kept so a file written before patterns existed and one written after are read
    # by the same code path.
    chosen_pattern = patterns[0]
    others = chosen_pattern["alsoInThisPattern"] + chosen_pattern["unmatched"]

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
            "the pull nearest the centre of the shape most of these kills shared; "
            "pulls are grouped by their target-count curve over normalised fight "
            "time, and a shape only becomes a preset when at least two pulls have it"
        ),
        # Every shape at least two sampled pulls shared, most-shared first. One entry
        # means the pulls agreed and there is nothing to choose between.
        "patterns": patterns,
        "representative": chosen_pattern["representative"],
        "others": others,
    }


def _caveats(payload: dict, rankings_page: int | None, order: str | None = None) -> list[str]:
    """Everything about the measurement that limits how far it can be read."""
    notes: list[str] = []
    fights = [fight for fight in payload.get("fights") or [] if isinstance(fight, dict)]
    kills = distinct_kills(fights)
    duplicates = len(fights) - len(kills)

    if duplicates:
        # First, because it changes how every other number in this block reads.
        notes.append(
            f"{len(fights)} sampled row(s) are {len(kills)} distinct kill(s): "
            f"{duplicates} of them are the same pull uploaded by more than one "
            f"person in the same raid, matched on fight length and target-count "
            f"curve. The band and the shape presets count kills; the pooled spreads "
            f"beside them still count rows, so a heavily duplicated boss weights "
            f"one pull's numbers by how many people logged it."
        )
    # Counted in kills rather than rows: six uploads of one pull cleared this test
    # while being exactly the case it exists to warn about.
    if len(kills) < 3:
        notes.append(
            f"{len(kills)} distinct kill(s) sampled: too few to tell an encounter's "
            f"shape from one guild's pull."
        )
    coverages = [
        float(fight["eventCoverage"])
        for fight in fights
        if isinstance(fight.get("eventCoverage"), (int, float))
    ]
    if coverages and min(coverages) < COMPLETE_EVENT_COVERAGE:
        # Quantitative on purpose. "At least one event fetch stopped at its page
        # limit" is what this used to say, and it was true of a run where one boss
        # was read for 11% of its length and reported a mean concurrent target
        # count of 0.34 -- a number nobody could have known to distrust from that
        # sentence.
        notes.append(
            f"The event fetch reached {min(coverages):.0%}-{max(coverages):.0%} of "
            f"these fights. Counts here are averaged over the part that was read, "
            f"not the whole pull, and a whole-fight claim cannot be taken from them: "
            f"raise the probe's --max-pages and re-run."
        )
    elif any(fight.get("truncated") for fight in fights):
        notes.append(
            "At least one event fetch stopped at its page limit, so the tail of that "
            "fight is incomplete rather than absent."
        )
    if order == "first":
        span = payload.get("killedBetween")
        if isinstance(span, dict) and span.get("first"):
            # The dates, not just the claim. "Earliest kills" is only as true as the
            # ranking window is wide: the selector sorts by date within the pages it
            # gathered, and those are sorted by damage, so a slow first-night kill
            # can sit past the window entirely. A reader who can see the dates can
            # judge that; one who is only told "the earliest kills" cannot.
            notes.append(
                f"Sampled from the earliest kills by kill date, {span['first'][:10]} to "
                f"{span['last'][:10]} ({span.get('spanDays')} days) -- long kills at the "
                f"intended tuning, whose timings are alike. Earliest here means earliest "
                f"among the ranking pages gathered, which Warcraft Logs sorts by damage, "
                f"so a slow early kill can fall outside the window: widen it with "
                f"--rankings-pages."
            )
        else:
            notes.append(
                "Sampled from the earliest kills of the boss by kill date -- long kills "
                "at the intended tuning, whose timings are alike. No kill dates were "
                "recorded, so how early they really were cannot be checked."
            )
    elif rankings_page == 1:
        notes.append(
            "Sampled from page 1 of the rankings: the world's best pulls, which are "
            "shorter than a typical kill and kill adds faster. Probe with --order "
            "first for the earliest kills instead."
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


def _no_fights_caveats(payload: dict) -> list[str]:
    """Why an encounter has no kills in it, when the search can say.

    "The probe read no fights" is true and answers nothing: a boss nobody has killed
    yet and a boss whose kills are all at a difficulty this run did not ask for look
    identical on the page, and only one of them names its own fix. The counts come
    from the report search, which is unfiltered by difficulty on purpose -- so it
    sees the kills the probe then declines to open.
    """
    caveats = ["The probe read no fights for this encounter."]
    seen = payload.get("difficultiesSeen")
    if not isinstance(seen, dict) or not seen:
        return caveats

    wanted = payload.get("difficulty")
    names = {"5": "Mythic", "4": "Heroic", "3": "Normal", "1": "Raid Finder"}
    parts = [
        f"{count} at {names.get(str(key), f'difficulty {key}')}"
        if key != "None"
        else f"{count} with no difficulty recorded"
        for key, count in seen.items()
    ]
    asked = names.get(str(wanted), f"difficulty {wanted}")
    caveats.append(
        f"The log search did find kills of this encounter -- {', '.join(parts)} -- "
        f"but this run asked for {asked}, so none of them was opened."
    )
    return caveats


#: Which difficulty a boss's headline `measured` block comes from when several were
#: read. Highest wins: Mythic is the reference every other number on this site uses,
#: and a boss with only Heroic data -- four of MID2's eight, which have zero Mythic
#: kills -- then surfaces its Heroic band instead of nothing. The choice is a policy,
#: not arithmetic, so `measuredDifficulty` states which one was taken and
#: `measurements` carries all of them; a reader is never left inferring it from a
#: number's size.
def _hardest(at_difficulty: dict[int | None, dict]) -> dict | None:
    """The headline difficulty: the hardest one that actually READ a fight.

    ``None`` -- a fight that stated no difficulty -- sorts last rather than first: it
    is the weakest row, and letting an unknown outrank a measured Mythic would be the
    "unknown is not zero" rule inverted.

    **"Present" is not "read", and taking the first for the second published an empty
    headline over a full measurement.** Measured against the committed MID2 file on
    2026-08-30: Sszorak carries ``{5: 0 kills, 4: 17 kills}`` and The Twin Fangs
    ``{5: 0, 4: 10}``. Both had `measuredDifficulty: 5` and a `measured` block reading
    nothing, so the view -- which reads `measured` alone -- drew its never-probed
    state over 27 kills that were paid for and sitting in the same document. That is
    the same shape as `coverage == 1.0` on a pull that read nothing: the field says
    the run looked, and what it looked at is empty.

    So a block that read fights beats one that did not, and only then does difficulty
    decide. The fallback is deliberate: when NOTHING read a fight the hardest present
    block still wins, because "probed and read nothing" and "never probed" are
    different sentences and dropping to ``None`` here would publish the second for the
    first.
    """
    usable = [(k, v) for k, v in at_difficulty.items() if v]
    if not usable:
        return None
    return max(
        usable,
        key=lambda kv: (bool(kv[1].get("fights")), kv[0] if kv[0] is not None else -1),
    )[1]


def _measured_block(
    payload: dict | None,
    rankings_page: int | None,
    order: str | None = None,
) -> dict | None:
    """One difficulty's measurement, or None when the probe never looked at all.

    Three states, and they are three sentences rather than degrees of one: `None`
    means never probed, a block with `fightsSampled: 0` means probed and read
    nothing, and a full block means read. Collapsing the first two would publish a
    claim no run ever made -- the view says something different for each.
    """
    measured = MeasuredEncounter(payload) if payload and payload.get("fights") else None
    if measured is not None and payload is not None:
        return {
            "fightsSampled": len(measured.fights),
            # How many kills those rows are. Warcraft Logs indexes uploads, so one
            # kill logged by six people in the same raid arrives as six rows and
            # every count derived from them is six times an observation of one
            # pull. Published unconditionally beside `fightsSampled`, because an
            # absent field would read as "the same number" -- which is exactly the
            # reading that was wrong.
            "distinctKills": len(distinct_kills(measured.fights)),
            "reports": list(payload.get("reports") or []),
            "durationSeconds": payload.get("durationSeconds"),
            "raidSize": payload.get("raidSize"),
            "playersListed": payload.get("playersListed"),
            "meanTargets": payload.get("meanTargets"),
            "peakTargets": payload.get("peakTargets"),
            "peakTargetShare": payload.get("peakTargetShare"),
            "activeTimeFraction": payload.get("activeTimeFraction"),
            # How much of each sampled fight the events actually covered. Every
            # count in this block is averaged over that, not over the fight.
            "eventCoverage": payload.get("eventCoverage"),
            # When the sampled kills actually happened. `--order first` promises the
            # earliest kills and delivers the earliest *among the ranking pages
            # gathered*, which are sorted by damage -- so a slow first-night kill can
            # sit past the window and never be seen. Publishing the span is what
            # turns that from an assumption into something a reader can check.
            "killedBetween": payload.get("killedBetween"),
            "adds": payload.get("adds") or [],
            "auras": payload.get("auras") or [],
            "phases": payload.get("phases") or [],
            "truncated": any(fight.get("truncated") for fight in measured.fights),
            "caveats": _caveats(payload, rankings_page, order),
            # The distribution of concurrent targets over the fight, across every
            # fully-read kill: the direct answer to "how many are normally up, when".
            "targetBand": _target_band(payload),
            "timeline": _timeline_block(payload),
        }
    if payload is not None:
        # The probe looked and found nothing. That is a different state from never
        # having looked, and reads differently on the page.
        return {
            "fightsSampled": 0,
            "reports": list(payload.get("reports") or []),
            "caveats": _no_fights_caveats(payload),
            "timeline": None,
        }
    return None


def _encounter_document(
    profile: FightProfile,
    at_difficulty: dict[int | None, dict],
    rankings_page: int | None,
    order: str | None = None,
) -> dict:
    facts = _fact_entries(profile)
    payload = _hardest(at_difficulty)
    measured = MeasuredEncounter(payload) if payload and payload.get("fights") else None
    measured_block = _measured_block(payload, rankings_page, order)

    promotions = plan_promotions(profile, measured) if measured is not None else []
    # Every difficulty read, hardest first, each with its own measured block. The
    # headline `measured` above is one of these; `measuredDifficulty` says which, so
    # the two can never disagree about what is being shown.
    measurements = [
        {
            "difficulty": difficulty,
            **(
                _measured_block(entry, rankings_page, order)
                or {"fightsSampled": 0, "reports": [], "caveats": [], "timeline": None}
            ),
        }
        for difficulty, entry in sorted(
            ((k, v) for k, v in at_difficulty.items() if v),
            key=lambda kv: -(kv[0] if kv[0] is not None else -1),
        )
    ]
    return {
        "encounterId": profile.encounter_id,
        "name": profile.name,
        "difficulty": profile.difficulty,
        "measuredDifficulty": (payload or {}).get("difficulty"),
        "measurements": measurements,
        "hasFacts": any(entry["source"] != SOURCE_DEFAULT for entry in facts),
        "facts": facts,
        "profile": _profile_block(profile),
        "scenario": _scenario_block(profile),
        "measured": measured_block,
        "comparison": _comparison(profile, measured),
        # What the measurement could contribute to the profile, and what stops it.
        # Published rather than applied: the site is where somebody looks at a
        # proposal before running the command that writes it.
        "promotions": [promotion.to_json() for promotion in promotions],
        "promoteCommand": (
            f"wowdps fight-promote --tier {profile.tier} "
            f"--encounter {profile.encounter_id} --probe fight-probe-{profile.tier}.json --write"
        ),
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
    # encounter -> difficulty -> entry. One payload holds every difficulty measured
    # (the owner's decision, 2026-08-26), so a flat dict keyed on the encounter would
    # drop one of them here -- the same silent loss the probe side was just fixed for,
    # one layer down, which is where this repository's losses usually live.
    by_encounter: dict[int, dict[int | None, dict]] = {}
    measurement: dict | None = None
    rankings_page: int | None = None
    order: str | None = None

    if probe:
        for entry in probe.get("encounters") or []:
            if isinstance(entry, dict) and isinstance(entry.get("encounterId"), int):
                difficulty = entry.get("difficulty")
                by_encounter.setdefault(entry["encounterId"], {})[
                    difficulty if isinstance(difficulty, int) else None
                ] = entry
        rankings_page = probe.get("rankingsPage")
        order = probe.get("order")
        measurement = {
            "generatedAt": probe.get("generatedAt"),
            "difficulty": probe.get("difficulty"),
            "metric": probe.get("metric"),
            "reportsPerEncounter": probe.get("reportsPerEncounter"),
            "rankingsPage": rankings_page,
            "order": order,
            "eventStreams": probe.get("eventStreams") or [],
            "significantDamageShare": probe.get("significantDamageShare"),
            "samplingBias": probe.get("sampling"),
            "abortedBecause": probe.get("abortedBecause"),
            "cost": probe.get("cost"),
        }

    known = dict(profiles.profiles)
    # A probe of an encounter the profile file has never heard of still publishes:
    # a new raid tier arrives as measurements before anybody writes facts down.
    for encounter_id, at_difficulty in by_encounter.items():
        if encounter_id not in known:
            entry = _hardest(at_difficulty)
            known[encounter_id] = FightProfile(
                tier=tier,
                encounter_id=encounter_id,
                name=str((entry or {}).get("encounterName") or encounter_id),
                difficulty=int((entry or {}).get("difficulty") or 5),
            )

    encounters = [
        _encounter_document(profile, by_encounter.get(encounter_id) or {}, rankings_page, order)
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
#: Wall-clock stamps that must not, on their own, make a document look changed.
#: Nested paths matter: the first version of this listed only the top-level
#: ``generatedAt``, so ``measurement.generatedAt`` still differed on every run, the
#: comparison never matched, and *neither* stamp settled. The file was rewritten and
#: committed on every probe re-run with nothing about the fights moved -- which is
#: precisely the property the settle exists to protect.
_PROVENANCE_PATHS: tuple[tuple[str, ...], ...] = (
    ("generatedAt",),
    ("measurement", "generatedAt"),
    # The point meter, and it belongs here for the same reason the timestamps do:
    # it describes the *run*, not the fights. Five of its nine fields are readings
    # taken at the moment the run happened -- what the hour's counter stood at,
    # how long until it resets -- so they differ on every pass by construction and
    # the settle can never fire.
    #
    # Measured on 2026-08-24: three consecutive hourly probe commits, each "data:
    # refresh fight shapes for MID2", each a one-line diff, and the only fields
    # that moved were the two stamps and `cost`. No encounter, no kill count, no
    # aura window changed. That is exactly the failure the settle exists to
    # prevent -- "any diff means something moved" stops being true -- reached
    # through a field nobody thought of as a stamp.
    #
    # The block is kept in the document rather than dropped: what a pass costs is
    # the open question behind the event-budget decision, and it is the only
    # measurement of it anywhere.
    ("measurement", "cost"),
)


def _without_stamps(document: dict) -> dict:
    """A copy with every provenance stamp removed, for comparison only."""
    stripped = json.loads(json.dumps(document))
    for path in _PROVENANCE_PATHS:
        node = stripped
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return stripped


def _carry_stamps(document: dict, published: dict) -> dict:
    """The new document wearing the published run's stamps."""
    settled = json.loads(json.dumps(document))
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


class MeasurementWouldBeLost(RuntimeError):
    """Refusal: this write would replace measured fights with nothing."""


def write_fights(out_dir: Path, document: dict, force: bool = False) -> Path:
    """Write ``<out_dir>/fights.json``, keeping the timestamp when nothing changed.

    Same reasoning as the manifest: a wall-clock timestamp that rewrites itself on
    every run means every run commits, and "a diff means something moved" stops
    being true. ``generatedAt`` reads as when the fight data last changed.

    **A write that would drop a measurement is refused.** ``wowdps fights`` is
    deliberately usable with no probe at all -- that is the state of a checkout
    that has never reached Warcraft Logs, and publishing the assertions alone is
    right there. Pointed at a directory that *already* holds a probe's results it
    is something else entirely: it silently replaces 30 sampled kills per boss with
    nulls, and the run reports success. Done exactly that once, by hand, one
    command after promoting the facts those measurements produced.

    ``force`` is the way through for somebody who means it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "fights.json"

    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        published = None

    if published is not None and not force:
        had = int((published.get("coverage") or {}).get("measured") or 0)
        has = int((document.get("coverage") or {}).get("measured") or 0)
        if had and not has:
            raise MeasurementWouldBeLost(
                f"{path} carries measurements for {had} encounter(s) and this "
                f"document has none, so writing it would discard them. Pass a "
                f"--probe payload to rebuild the measured half, or --force if "
                f"dropping it is what you mean."
            )

    if published is not None and not force:
        # After the refusal above and before the settle below: the carried-forward
        # entries have to be in the document the settle compares, or a run that
        # changed nothing else would still write a new timestamp.
        document = _keep_measurements(published, document)

    settled = document
    if published is not None and _without_stamps(published) == _without_stamps(document):
        settled = _carry_stamps(document, published)

    path.write_text(json.dumps(settled, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _keep_measurements(published: dict, document: dict) -> dict:
    """Carry an encounter's measurements forward when this run produced none for it.

    The payload level already works this way -- "a run contributes what it managed;
    everything else comes back from the previous payload untouched" -- but
    ``--no-resume`` clears the previous payload by design, so an encounter the run
    did not reach vanishes from it and publishes as ``measured: null``.

    That is not a smaller claim, it is a *different* one. ``fightsSampled: 0`` says
    the probe looked and read nothing; ``measured: null`` says nothing ever looked,
    and the view says something different for each. Observed on 2026-08-21: a
    ``--no-resume`` pass moved four of MID2's eight encounters from the first state
    to the second, and the whole-document guard above could not see it because the
    other four still carried measurements.

    The carried block is from an earlier run, so ``measurement.generatedAt`` bounds
    the newest measurement in the document rather than every entry in it. That is
    the lesser of the two inaccuracies: the alternative asserts an encounter was
    never probed when it was.
    """
    by_id = {
        entry.get("encounterId"): entry
        for entry in published.get("encounters") or []
        if isinstance(entry, dict)
    }
    kept = 0
    encounters = []
    for entry in document.get("encounters") or []:
        was = by_id.get(entry.get("encounterId"))
        if entry.get("measured") is None and was is not None and was.get("measured") is not None:
            # Both fields, as a pair. `measured` is one member of `measurements`, so
            # carrying the headline forward and leaving the list empty would publish a
            # boss whose two blocks disagree about whether it was measured at all --
            # and the per-difficulty list is the one a reader checks for the OTHER
            # difficulty, which is exactly what a --no-resume pass is most likely to
            # have dropped.
            entry = {
                **entry,
                "measured": was["measured"],
                "measuredDifficulty": was.get("measuredDifficulty"),
                "measurements": was.get("measurements") or [],
            }
            kept += 1
        encounters.append(entry)
    if not kept:
        return document
    return {**document, "encounters": encounters}


def load_probe(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
