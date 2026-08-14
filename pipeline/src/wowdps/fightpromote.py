"""``wowdps fight-promote``: turn a measured fact into a profile fact, on purpose.

Why this is a command and not a step in the probe
-------------------------------------------------
Nine bosses' worth of target counts is exactly the sort of thing nobody should be
typing in by hand, and the probe already measures it. So a measurement needs a
route into ``fight_profiles.json`` with ``source: "logs"`` and the reports behind
it. What it must not have is an *automatic* route.

CLAUDE.md's rule is the reason: a disagreement between a hand fact and the probe is
a finding, and the likelier culprit is the extraction. A pipeline step that let a
measurement land on top of an assertion would delete that finding on the way past
and leave a profile that always agreed with the log reader -- which is exactly the
state in which the log reader can never be shown to be wrong.

Hence: the planning is pure and lives in ``fightprofile.plan_promotions``; this
module prints the plan by default, writes only under ``--write``, and never writes
over a hand fact at all. The one thing it will write into a hand fact is a *blank*
the person left -- an amplification's ability id and which target carries it -- and
even then the multiplier, the start and the duration stay exactly as asserted.

What a written fact looks like
------------------------------
``provenance.source`` becomes ``logs``, ``sample`` the number of fights, ``reports``
the codes, ``observedAt`` the probe run's timestamp, and ``detail`` the evidence
sentence including the spread the value was pooled from. Every one of those is
there so that the next person to read the file can tell what the number is worth
without re-running anything.

A note on hand-written prose that is left alone
-----------------------------------------------
When a blank inside a hand fact is filled, the fact's own ``provenance.detail`` --
somebody's sentence about what they stated -- is **not** rewritten. It is a record
of what was said at the time, and it stays true as that. The measured half arrives
as field-level provenance beside it (``targetSource``/``targetEvidence``), which is
the same shape ``magnitudeSource`` already had.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import fightdataset, fightprofile

log = logging.getLogger(__name__)


def _profiles_path(args: argparse.Namespace) -> Path:
    if args.profiles_file:
        return Path(args.profiles_file)
    return fightprofile._data_file()


def _measured_for(probe: dict, encounter_id: int) -> dict | None:
    for entry in probe.get("encounters") or []:
        if isinstance(entry, dict) and entry.get("encounterId") == encounter_id:
            return entry
    return None


def apply_promotion(
    document: dict,
    tier: str,
    encounter_id: int,
    promotion: fightprofile.Promotion,
    *,
    observed_at: str | None = None,
) -> bool:
    """Write one eligible promotion into a decoded ``fight_profiles.json``.

    Returns whether anything changed. An ineligible promotion is refused here as
    well as in the caller: the guard belongs next to the mutation, not only next
    to the decision to call it.
    """
    if not promotion.eligible:
        return False

    tiers = document.setdefault("tiers", {})
    entry = tiers.setdefault(tier, {"encounters": []})
    encounters = entry.setdefault("encounters", [])
    encounter = next(
        (e for e in encounters if isinstance(e, dict) and e.get("encounterId") == encounter_id),
        None,
    )
    if encounter is None:
        return False

    facts = encounter.setdefault("facts", {})
    existing = facts.get(promotion.key)
    if (
        isinstance(existing, dict)
        and (existing.get("provenance") or {}).get("source") == fightprofile.SOURCE_HAND
    ):
        # Filling blanks inside a hand fact keeps the hand provenance, untouched.
        # Anything else would have been refused upstream; refuse it again here.
        if promotion.key != "amplifications":
            return False
        existing["value"] = promotion.value
        return True

    facts[promotion.key] = {
        "value": promotion.value,
        "provenance": {
            "source": fightprofile.SOURCE_LOGS,
            "detail": promotion.evidence,
            "sample": promotion.sample,
            "reports": list(promotion.reports),
            **({"observedAt": observed_at} if observed_at else {}),
        },
    }
    return True


def render(
    encounter_name: str, encounter_id: int, promotions: list[fightprofile.Promotion]
) -> list[str]:
    """The plan, in the form somebody reads before deciding to run it with --write."""
    lines = [f"=== {encounter_name} (encounter {encounter_id}) ==="]
    if not promotions:
        lines.append("  nothing measured that this profile could take")
        return lines
    for promotion in promotions:
        mark = "PROMOTE" if promotion.eligible else "hold   "
        lines.append(f"  [{mark}] {promotion.label} -> {promotion.summary}")
        lines.append(f"            evidence: {promotion.evidence}")
        # Only a real contradiction gets printed. A blanks-fill changes the stored
        # value without contradicting any part of it, and printing "differs from"
        # there would put the alarming word on the one case that is not alarming.
        if promotion.disagrees:
            lines.append(f"            profile currently says {promotion.current!r}")
        lines.append(f"            {promotion.reason}")
    return lines


def cmd_fight_promote(args: argparse.Namespace) -> int:
    probe = fightdataset.load_probe(Path(args.probe))
    if probe.get("tier") and probe["tier"] != args.tier:
        log.warning(
            "the probe payload is for tier %s, promoting it into %s", probe["tier"], args.tier
        )

    path = _profiles_path(args)
    profiles = fightprofile.load_profiles(args.tier, path)
    document = json.loads(path.read_text(encoding="utf-8"))
    observed_at = probe.get("generatedAt")

    wanted = set(args.encounter or ())
    transcript: list[str] = []
    written = 0
    eligible = 0
    filled_blanks = False

    for encounter_id, profile in sorted(profiles.profiles.items()):
        if wanted and encounter_id not in wanted:
            continue
        payload = _measured_for(probe, encounter_id)
        if not payload or not payload.get("fights"):
            continue
        measured = fightdataset.MeasuredEncounter(payload)
        promotions = fightprofile.plan_promotions(profile, measured, min_fights=args.min_fights)
        transcript.extend(render(profile.name, encounter_id, promotions))
        transcript.append("")
        for promotion in promotions:
            if not promotion.eligible:
                continue
            eligible += 1
            if args.write and apply_promotion(
                document, args.tier, encounter_id, promotion, observed_at=observed_at
            ):
                written += 1
                filled_blanks = (
                    filled_blanks
                    or promotion.blocked_by is None
                    and (
                        profile.facts.get(promotion.key) is not None
                        and profile.facts[promotion.key].provenance.source
                        == fightprofile.SOURCE_HAND
                    )
                )

    text = "\n".join(transcript).rstrip()
    print(text if text else "no encounter in this probe payload has a profile to promote into")

    if not args.write:
        print()
        print(
            f"{eligible} fact(s) could be promoted. Nothing was written: re-run with "
            f"--write to apply them, which is the only way a measurement ever reaches "
            f"a profile."
        )
        return 0

    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print()
    print(f"wrote {written} fact(s) into {path}")
    if filled_blanks:
        print(
            "One or more hand-asserted facts had blanks filled in. Their "
            "`provenance.detail` is left exactly as written: it records what a "
            "person stated, not what the value is now, and rewriting somebody's "
            "sentence is not this command's job."
        )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--probe",
        required=True,
        help="a fight-probe-<tier>.json from a probe run, live or downloaded from CI",
    )
    parser.add_argument("--tier", default="MID2", help="tier whose profiles are promoted into")
    parser.add_argument(
        "--encounter",
        type=int,
        action="append",
        help="restrict to one encounter id (repeatable); default is every measured boss",
    )
    parser.add_argument(
        "--min-fights",
        type=int,
        default=fightprofile.DEFAULT_MIN_FIGHTS,
        help="fights a measurement must be pooled from before it is offered as a fact",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply the eligible promotions to the profile file; without it this "
        "command only prints what it would do",
    )
    parser.add_argument("--profiles-file", help="alternative fight profile file")
    parser.set_defaults(func=cmd_fight_promote)
