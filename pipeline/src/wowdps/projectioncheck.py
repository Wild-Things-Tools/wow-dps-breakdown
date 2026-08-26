"""Does a talent gain measured on the gear anchor survive on simc's own kit?

The Overview ranks a winning build at ``publishedDps x (1 + margin)``. The margin is
**measured** -- both contenders on one normalised kit, talents the only variable. The
product is **not**: nobody has run the computed talents on the gear simc's profile
actually wears. That assumption is what this module measures, and it is the whole of
issue #52.

The two numbers and why they can differ at all:

- **anchored** -- both builds at the anchor (item level 334, the floor of MID2's
  334-344 band), which is what `build-search` publishes. Reproducing it here is the
  control: if this run cannot reproduce the published margin, the run is wrong and
  nothing else it says means anything.
- **shipped** -- the same two talent hashes on simc's own profile, only ``talents=``
  overridden. This is the number the site's projection is standing in for.

**Both are profileset-against-profileset**, never against the base actor. CLAUDE.md's
measurement: the base actor runs a different iteration count and lands ~0.09% away,
which is the same order as the gains being compared here. Reading a margin off the
base actor is the single easiest way to get a wrong-by-0.1% answer, and this module
would be exactly where it did the most damage.

The verdict per build is the project's tie rule, applied to the *difference of two
margins*: the two errors added in quadrature, doubled, because each margin is itself a
difference of two measured means. A difference inside that band is not a finding.

What the outcome should lead to, stated before the run so the answer cannot be read
to taste:

- **inside the band on every build** -- the projection is as good as a measurement,
  and that belongs in CLAUDE.md beside the projection itself.
- **systematically larger** -- then the right answer is to measure the gain on shipped
  gear directly and drop the projection, not to correct it by a factor. A factor would
  be a second unmeasured assumption on top of the first.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .buildsearch import Candidate
from .talenttree import Loadout

log = logging.getLogger(__name__)


#: Each margin is a difference of two measured means, so its error is the two added in
#: quadrature; comparing two margins adds two more. The band is therefore the quadrature
#: sum of all four errors -- not a tuned tolerance, and not a fixed percentage.
def tie_band(*errors: float) -> float:
    """The project's uncertainty convention: hypot of every error involved."""
    return math.sqrt(sum(e * e for e in errors))


@dataclass(frozen=True)
class Comparison:
    """One build's answer to the question, with everything needed to check it."""

    build_id: str
    anchored_margin: float
    shipped_margin: float
    band: float
    published_margin: float | None

    @property
    def difference(self) -> float:
        return self.shipped_margin - self.anchored_margin

    @property
    def separates(self) -> bool:
        """True when the two margins genuinely disagree rather than jitter apart."""
        return abs(self.difference) > self.band

    @property
    def reproduced(self) -> bool | None:
        """Did this run reproduce the published anchored margin?

        None when the document carried none to compare against. This is the control,
        and a run that fails it says nothing about the projection -- so it is reported
        separately rather than folded into the verdict.
        """
        if self.published_margin is None:
            return None
        return abs(self.anchored_margin - self.published_margin) <= self.band

    def to_json(self) -> dict:
        out: dict = {
            "id": self.build_id,
            "anchoredMargin": round(self.anchored_margin * 100, 4),
            "shippedMargin": round(self.shipped_margin * 100, 4),
            "differencePoints": round(self.difference * 100, 4),
            "tieBandPoints": round(self.band * 100, 4),
            "separates": self.separates,
        }
        if self.published_margin is not None:
            out["publishedMargin"] = round(self.published_margin * 100, 4)
            out["reproducedPublished"] = self.reproduced
        return out


def marked_builds(document: dict) -> list[dict]:
    """The rows the site actually draws a projection for.

    Exactly the rule `bestBuild.ts` applies, restated here rather than imported,
    because it lives in the other repository: a row counts when the computed build
    beats simc's *outside the tie band*. A row that ties is drawn as simc's own and
    carries no projection, so measuring it would answer a question nobody asked.
    """
    rows = []
    for entry in document.get("specs") or []:
        best, simc = entry.get("best"), entry.get("simc")
        if not best or not simc or not simc.get("dps"):
            continue
        margin = (best["dps"] / simc["dps"]) - 1
        band = tie_band((best.get("dpsError") or 0.0) / 100, (simc.get("dpsError") or 0.0) / 100)
        if margin > band:
            rows.append(entry)
    return rows


def candidates_for(entry: dict, nodes: dict[int, list]) -> list[Candidate] | None:
    """simc's build and the computed one, as the two profilesets to run.

    Returns None when either hash will not decode -- which is not a failure of this
    check but of the document, and the caller reports it as such rather than
    silently measuring one side.
    """
    from . import talenttree as tt

    out = []
    for key, source in (("simc", entry.get("simc")), ("best", entry.get("best"))):
        talent_hash = (source or {}).get("talentHash")
        if not talent_hash:
            return None
        try:
            loadout: Loadout = tt.decode_loadout(talent_hash, nodes)
        except tt.TalentDecodeError:
            return None
        out.append(
            Candidate(
                key=key,
                label=(source or {}).get("label") or key,
                origin=(source or {}).get("origin") or key,
                loadout=loadout,
                talent_hash=talent_hash,
            )
        )
    return out


def compare(
    build_id: str,
    anchored: dict,
    shipped: dict,
    published_margin: float | None,
) -> Comparison | None:
    """Turn two measured fields into one build's answer.

    ``anchored`` and ``shipped`` are ``{key: Measurement}`` as ``buildsearchrun.measure``
    returns them. Either missing a side is a refusal: a margin computed against a
    missing measurement is not a smaller claim, it is a wrong one.
    """
    if not {"simc", "best"} <= set(anchored) or not {"simc", "best"} <= set(shipped):
        return None
    if not anchored["simc"].dps or not shipped["simc"].dps:
        return None

    anchored_margin = (anchored["best"].dps / anchored["simc"].dps) - 1
    shipped_margin = (shipped["best"].dps / shipped["simc"].dps) - 1
    band = tie_band(
        *(m.dps_error / 100 for m in (anchored["best"], anchored["simc"])),
        *(m.dps_error / 100 for m in (shipped["best"], shipped["simc"])),
    )
    return Comparison(
        build_id=build_id,
        anchored_margin=anchored_margin,
        shipped_margin=shipped_margin,
        band=band,
        published_margin=published_margin,
    )


def verdict(rows: list[Comparison]) -> str:
    """One sentence over the whole run, in the terms the issue set out.

    Deliberately reports the direction as well as the count: "they disagree" would
    leave open whether the projection over- or under-states, and those lead to
    different fixes.
    """
    if not rows:
        return "no build could be compared, so this run says nothing about the projection"
    separating = [r for r in rows if r.separates]
    if not separating:
        return (
            f"the projection holds: on all {len(rows)} build(s) the shipped-gear margin "
            f"sits inside the tie band of the anchored one"
        )
    over = [r for r in separating if r.difference < 0]
    under = [r for r in separating if r.difference > 0]
    worst = max(separating, key=lambda r: abs(r.difference))
    direction = (
        "the projection overstates" if len(over) >= len(under) else "the projection understates"
    )
    return (
        f"{len(separating)} of {len(rows)} build(s) disagree outside the tie band; "
        f"{direction} on the majority of them, worst {worst.build_id} at "
        f"{worst.difference * 100:+.2f} points"
    )


def write_report(path: Path, tier: str, rows: list[Comparison], notes: list[str]) -> None:
    """The run's own record, so a later reader need not re-run it to check the claim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tier": tier,
        "question": ("does a talent gain measured on the gear anchor hold on simc's own kit?"),
        "verdict": verdict(rows),
        "builds": [row.to_json() for row in rows],
        "notes": notes,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
