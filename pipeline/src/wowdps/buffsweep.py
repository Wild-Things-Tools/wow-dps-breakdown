"""What a tier set and an outside cooldown are worth, per spec.

Two questions the community answers in a spreadsheet and nobody can check:
*how much is my class's tier set worth* and *who should the Priest press Power
Infusion on*. Both are one profileset sweep each, both come out of the same simc
run the rest of this project already knows how to drive, and both are exactly the
kind of number that should carry its uncertainty rather than sit in a cell.

Everything here is a *toggle against the spec's own shipped profile*, which is
what makes the answer a difference rather than a level. Two profilesets with the
same options return bit-identical DPS (measured, see CLAUDE.md), so a gain is an
exact subtraction -- and the base actor is deliberately not used as the reference,
because it runs a slightly different iteration count and lands ~0.09% away from an
identical profileset. Every comparison below is profileset against profileset.

### Tier sets

`item_set_bonus.inc` carries every set simc knows: name, the option token, the
tier, the class, the spec, whether the row is the 2- or 4-piece, and the spell.
MID2's thirteen sets -- one per class -- all share the option token
``midnight_season_2``, so the sweep is the same three variants for every spec:

    set_bonus=midnight_season_2_2pc=0  _4pc=0     nothing
    set_bonus=midnight_season_2_2pc=1  _4pc=0     two pieces
    set_bonus=midnight_season_2_2pc=1  _4pc=1     four pieces

which yields the 2-piece value and the 4-piece value separately. That separation
is the point: a set whose 4-piece is most of its value plays differently from one
where the 2-piece carries it, and a single "tier set worth X%" hides that.

### Power Infusion

``external_buffs.power_infusion`` takes a list of *times*, not a count -- it is
"the buff lands at these seconds", so the option encodes a usage pattern rather
than a number of casts. Modelling it as one cast at the pull would flatter specs
whose own cooldowns line up with the pull and nothing else, so the default here is
on cooldown from the pull for the whole fight, which is what a Priest actually
does. The pattern is published beside the number, because the number means
nothing without it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .profiles import SpecProfile
from .scenarios import PATCHWERK, Scenario, SimSettings
from .simc_runner import Profileset, ProfilesetResult, SimRequest, run_profilesets

log = logging.getLogger(__name__)

#: Power Infusion's cooldown and duration, from the spell. Used only to build the
#: default cast pattern; the pattern itself is published, so a reader is never
#: asked to take these on trust.
PI_COOLDOWN = 120.0
PI_DURATION = 20.0

_SET_ROW = re.compile(
    r'\{\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,'
    r"\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,"
)

_NONE = "set__none"
_TWO = "set__2pc"
_FOUR = "set__4pc"
_NO_PI = "pi__none"
_WITH_PI = "pi__oncooldown"


@dataclass(frozen=True)
class TierSet:
    """One class's tier set for one tier."""

    name: str
    option: str
    tier: str
    class_id: int

    def to_json(self) -> dict:
        return {"name": self.name, "option": self.option, "tier": self.tier}


def parse_tier_sets(simc_dir: Path, ptr: bool = False) -> list[TierSet]:
    """Every set bonus simc ships, one entry per (set, class).

    Parsed rather than queried, for the same reason the item and trait tables are:
    there is no simc command that lists them.
    """
    name = "item_set_bonus_ptr.inc" if ptr else "item_set_bonus.inc"
    path = simc_dir / "engine" / "dbc" / "generated" / name
    found: dict[tuple[str, str, int], TierSet] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        row = _SET_ROW.search(line)
        if not row:
            continue
        set_name, option, tier, _enum, _set_id, _bonus, class_id, _spec = row.groups()
        key = (option, tier, int(class_id))
        found.setdefault(
            key, TierSet(name=set_name, option=option, tier=tier, class_id=int(class_id))
        )
    return sorted(found.values(), key=lambda entry: (entry.tier, entry.class_id, entry.name))


def sets_for_tier(sets: list[TierSet], tier: str) -> list[TierSet]:
    """The sets belonging to one tier, by simc's own tier label (``MID2``)."""
    return [entry for entry in sets if entry.tier == tier]


def power_infusion_times(fight_length: float) -> tuple[float, ...]:
    """When Power Infusion lands, on cooldown from the pull.

    A single cast at the pull would flatter a spec whose own cooldowns happen to
    line up there, so the default is the pattern a Priest actually produces.
    """
    times: list[float] = []
    at = 0.0
    while at + PI_DURATION <= fight_length:
        times.append(at)
        at += PI_COOLDOWN
    return tuple(times or (0.0,))


def set_variants(option: str) -> list[Profileset]:
    """Nothing, two pieces, four pieces -- as toggles against the shipped profile.

    The profile is overridden in all three, including the "nothing" one: a spec
    whose shipped profile already wears the set would otherwise have its baseline
    silently include it, and every gain would come out near zero.
    """
    return [
        Profileset(key=_NONE, options=(f"set_bonus={option}_2pc=0", f"set_bonus={option}_4pc=0")),
        Profileset(key=_TWO, options=(f"set_bonus={option}_2pc=1", f"set_bonus={option}_4pc=0")),
        Profileset(key=_FOUR, options=(f"set_bonus={option}_2pc=1", f"set_bonus={option}_4pc=1")),
    ]


def power_infusion_variants(times: tuple[float, ...]) -> list[Profileset]:
    cast_at = "/".join(f"{value:g}" for value in times)
    return [
        Profileset(key=_NO_PI, options=("external_buffs.power_infusion=",)),
        Profileset(key=_WITH_PI, options=(f"external_buffs.power_infusion={cast_at}",)),
    ]


@dataclass
class BuffResult:
    """One spec's answer to both questions."""

    spec_id: str
    display_name: str
    wow_class: str
    spec: str
    hero_talent: str
    base_dps: float = 0.0
    two_piece_gain: float | None = None
    four_piece_gain: float | None = None
    set_name: str | None = None
    power_infusion_gain: float | None = None
    power_infusion_times: tuple[float, ...] = ()
    dps_error: float = 0.0
    errors: list[str] | None = None

    def to_json(self) -> dict:
        return {
            "id": self.spec_id,
            "displayName": self.display_name,
            "class": self.wow_class,
            "spec": self.spec,
            "heroTalent": self.hero_talent,
            "baseDps": round(self.base_dps, 1),
            "dpsError": round(self.dps_error, 4),
            "setName": self.set_name,
            # Absolute and relative both, because a percentage alone hides that a
            # 1% gain on a 600k spec is worth more raid damage than 2% on 300k.
            "twoPieceGain": _gain(self.two_piece_gain),
            "twoPiecePercent": _percent(self.two_piece_gain, self.base_dps),
            "fourPieceGain": _gain(self.four_piece_gain),
            "fourPiecePercent": _percent(self.four_piece_gain, self.base_dps),
            "powerInfusionGain": _gain(self.power_infusion_gain),
            "powerInfusionPercent": _percent(self.power_infusion_gain, self.base_dps),
            "powerInfusionTimes": list(self.power_infusion_times),
            "errors": self.errors or [],
        }


def _gain(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percent(value: float | None, base: float) -> float | None:
    if value is None or base <= 0:
        return None
    return round(value / base, 5)


def sweep_spec(
    simc: Path,
    profile: SpecProfile,
    tier_set: TierSet | None,
    settings: SimSettings,
    scenario: Scenario = PATCHWERK,
    targets: int = 1,
    timeout: int = 1800,
) -> BuffResult:
    """Both sweeps for one spec, in two simc invocations.

    Two rather than one on purpose: the set variants and the Power Infusion
    variants are independent questions, and combining them into one profileset list
    would measure Power Infusion on whichever set state simc's shipped profile
    happens to carry -- an answer to neither question.
    """
    result = BuffResult(
        spec_id=profile.id,
        display_name=profile.display_name,
        wow_class=profile.wow_class,
        spec=profile.spec,
        hero_talent=profile.hero_label,
        errors=[],
    )
    request = SimRequest(profile=profile, scenario=scenario, targets=targets)

    if tier_set is not None:
        result.set_name = tier_set.name
        try:
            measured = run_profilesets(
                simc, request, settings, set_variants(tier_set.option), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - one bad spec must not kill a sweep
            result.errors.append(f"tier set: {exc}")
        else:
            result = _read_sets(result, measured)

    fight_length = float(scenario.max_time)
    times = power_infusion_times(fight_length)
    result.power_infusion_times = times
    try:
        measured = run_profilesets(
            simc, request, settings, power_infusion_variants(times), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"power infusion: {exc}")
    else:
        without, with_pi = measured.get(_NO_PI), measured.get(_WITH_PI)
        if without and with_pi:
            result.power_infusion_gain = with_pi.dps - without.dps
            if not result.base_dps:
                result.base_dps = without.dps
                result.dps_error = without.dps_error

    return result


def _read_sets(result: BuffResult, measured: dict[str, ProfilesetResult]) -> BuffResult:
    none, two, four = measured.get(_NONE), measured.get(_TWO), measured.get(_FOUR)
    if not none:
        (result.errors or []).append("no result for the set-less variant")
        return result
    result.base_dps = none.dps
    result.dps_error = none.dps_error
    if two:
        result.two_piece_gain = two.dps - none.dps
    # The four-piece is reported as what it adds *over the two-piece*, not over
    # nothing: nobody chooses between four pieces and none, they choose whether the
    # third and fourth are worth their slots.
    if four and two:
        result.four_piece_gain = four.dps - two.dps
    return result


def class_id_of(profile: SpecProfile, sets: list[TierSet]) -> TierSet | None:
    """The tier set belonging to a profile's class.

    ``item_set_bonus.inc`` keys on simc's numeric class id and the profiles carry a
    class *name*, so the join runs through the order classes appear in that table --
    which is `player_e`, the same order ``specindex`` reads. Returned as None rather
    than guessed when the class has no set for the tier, because a spec with no set
    is a real state (a tier can ship late) and inventing one would publish a gain
    for something nobody can wear.
    """
    from .specindex import _CLASS_ORDER

    try:
        class_id = _CLASS_ORDER.index(profile.wow_class) + 1
    except ValueError:
        return None
    return next((entry for entry in sets if entry.class_id == class_id), None)


def write_buffs(out_dir: Path, tier: str, results: list[BuffResult], settings: SimSettings) -> Path:
    """Write ``<tier>/buffs.json``."""
    import json
    from datetime import UTC, datetime

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "buffs.json"
    document = {
        "tier": tier,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": {
            "deterministic": settings.target_error == 0,
            "iterations": settings.max_iterations,
        },
        "note": (
            "Every figure is a toggle against the spec's own profile, measured as a "
            "difference between two profilesets rather than against the base actor -- "
            "two profilesets with identical options return bit-identical DPS, while the "
            "base actor runs a slightly different iteration count. The four-piece value "
            "is what it adds over the two-piece, because that is the choice being made. "
            "Power Infusion is a usage pattern, not a count: the seconds it lands at are "
            "published beside the number."
        ),
        "specs": [result.to_json() for result in results],
    }
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    return path
