"""Making a refused talent hash loadable, and saying exactly what that does and does not claim.

Four of MID2's twenty-six damage specs have **no number anywhere on the site**: Havoc
Demon Hunter, Retribution Paladin, Arms Warrior and Fury Warrior. The published
dataset carries 36 builds across 22 specs (``index.json``, generated 2026-08-23), and
those four are the gap. The cause is not gear, not an action list and not a missing
profile -- simc's generators carry a *complete* profile for each of them, every gear
slot with its gems and enchants -- it is that **simc's own talent parser refuses the
stored hash**, so the profile cannot be loaded at all.

Asked directly, on simc ``625a591`` (2026-08-23), each of the four exits **81**:

=========================  =====================================================================
``Havoc`` (both builds)    ``Node 91020 is not a choice node but has index selection.``
``Retribution`` (both)     ``Node 81527 is not a choice node but has index selection.``
``Arms``                   ``Selected node 110203 entry 136735 is not available to player's
                           spec.``
``Fury``                   ``Selected node 109391 entry 135597 is not available to player's
                           spec.``
=========================  =====================================================================

Those wordings were **read off simc's stderr, not predicted**, and the first two are
worth a second look by anyone comparing them with what this project publishes. The
site's ``spec-index.json`` words the same four refusals as *"choice index 1 out of
bounds for node 91020 (1 entries)"* -- which is this project's own decoder speaking,
and a different one of simc's eleven refusals (#11 rather than #10). simc names the
right node in both tellings, so nothing downstream is wrong about *which* profile
fails; but a caller matching a run's stderr against the published sentence would find
nothing. Reported rather than fixed here: it is ``specindex``'s field and rewriting it
churns a published document for one clause.

Two different diseases, and this module treats them differently because they are.

What a repair claims, and what it does not
------------------------------------------
**Claim: this is a hash simc loads, derived from simc's own by the correction the
current trait table forces.** Both halves are checkable -- simc either loads it or it
does not, and the correction is forced rather than chosen: a node that now holds one
entry cannot be written with a choice index, and a node another spec owns cannot be
selected at all. There is no second legal option in either case.

**Not claimed: that this is the build simc's authors meant.** That would need the
trait table the hash was written against, which nobody has. A repaired build is
*simc's profile, minus what the tree no longer permits* -- which is a weaker thing
than a shipped build and a stronger thing than nothing, and it has to be labelled as
exactly that wherever it is published.

**Not claimed either: that the decode underneath it is faithful.** This is the part
that cost the most to establish and is the easiest to get wrong.

Why round-tripping is not evidence of a sound read
--------------------------------------------------
Measured on simc ``625a591``: 47 of MID2's 51 hashes decode strictly and **all 47
re-encode byte for byte**, Arms and Fury among them. That is tempting to read as "the
decode is right", and it is not: the encoder walks the same node list as the decoder,
so a reader whose node walk disagrees with the *writer's* still reproduces the string
it misread. Round-tripping proves reader and writer are inverses. It says nothing
about whether either matches the tree the hash was exported from.

Two signals do carry information, and both are read off the corpus rather than typed:

* **``spare_bits``** -- how much of the string is left when the node stream ends.
  Over the 84 MID1+MID2 profiles that decode strictly the whole observed range is
  **-1 to 9** (MID2 alone: 0 to 9), which is the padding to a 6-bit boundary plus the
  extra character three Hunter profiles carry. A stream that overruns the string, or
  stops a long way short of it, has lost sync with the writer.
* **per-tree point totals** against ``talentedit.derive_point_budget``, the ceiling the
  tier's own shipped profiles set.

Applied to the four refused profiles, they separate cleanly and they agree with each
other:

===================================  ==========  ===============  ==========
profile                              spare bits  class/spec/hero  verdict
===================================  ==========  ===============  ==========
Havoc Fel-Scarred                    3           35 / 34 / 14     sound
Havoc Aldrachi Reaver                **-77**     33 / 30 / 6      **overran the string**
Retribution (both builds)            **10**      31 / **37** / 16 **stopped early, and over budget**
Arms                                 1           36 / 34 / 14     sound
Fury                                 4           37 / **30** / 14 sound, with a caveat
===================================  ==========  ===============  ==========

So the choice-index overflow that ``decode_loadout`` raises on is a *symptom* on three
of those five and the whole disease on only one: Havoc Aldrachi Reaver and both
Retribution builds are reading a tree that has changed shape under them, and repairing
them would produce a valid hash for a build nobody ever wrote -- the worst outcome
available here, because the number that comes back looks exactly like a real one.

**The screen only ever rejects.** A build that passes it is one that was not caught,
not one that was proven; that sentence travels with every repair as a caveat rather
than living only here.

Fury's spec tree
----------------
Fury spends **30** spec points where 82 of the other 83 profiles across both tiers
spend exactly 34. That is anomalous and it is not a decode failure: the same 30 comes
out of MID1's Fury profile as well, and a desync would not reproduce across two tiers
written years apart. It is simc's profile being four points light, and the repair does
not spend them -- spending a point is a *search* decision and this module makes none.
The count is published as a caveat instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import talentedit, talenttree
from .talenttree import Loadout, Trait

log = logging.getLogger(__name__)

#: The ``spare_bits`` range observed across every profile that decodes strictly, in
#: both tiers, on simc 625a591 (2026-08-23). Derived by ``observed_framing`` at run
#: time from whatever corpus is at hand; this is the measured value it reproduced,
#: kept here so a future run that produces something wildly different is visible as a
#: change rather than accepted as the new normal.
MEASURED_SPARE_RANGE = (-1, 9)


class RepairError(ValueError):
    """A hash that cannot be repaired, with the reason a person needs."""


@dataclass(frozen=True)
class Correction:
    """One forced change, named so a reader can see what was given up."""

    #: ``choice-index-dropped`` -- the node holds one entry now, so no index can be
    #: written. ``node-deselected`` -- the node belongs to another spec.
    kind: str
    node_id: int
    entry_id: int | None
    name: str
    detail: str

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "nodeId": self.node_id,
            "entryId": self.entry_id,
            "name": self.name,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Soundness:
    """Whether the decode underneath a repair survived the two screens.

    ``ok`` false means **do not repair this**: the reasons are the ones that indicate a
    stream out of step with the writer, and a repair built on one is a valid hash for a
    build nobody wrote.
    """

    ok: bool
    spare_bits: int
    points: tuple[int, int, int]
    reasons: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "spareBits": self.spare_bits,
            "points": {
                "class": self.points[0],
                "spec": self.points[1],
                "hero": self.points[2],
            },
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Repair:
    """What one refused hash became, or why it stayed refused."""

    profile_id: str
    original_hash: str
    #: None when the repair was refused. Never a silent fallback to the original.
    repaired_hash: str | None
    loadout: Loadout | None
    corrections: tuple[Correction, ...]
    soundness: Soundness
    #: What ``talentedit.validate_loadout`` still says about the repaired build.
    remaining: tuple[talentedit.Finding, ...]
    #: Why no hash came out, when none did.
    refused: str | None = None
    #: Points the repaired build leaves unspent against the tier's own ceiling.
    unspent: tuple[int, int, int] = (0, 0, 0)

    @property
    def ok(self) -> bool:
        return self.repaired_hash is not None

    def caveats(self) -> list[str]:
        """Everything a published number from this build has to carry with it."""
        notes = [
            "Repaired talent hash, not an optimised build: simc refuses the hash its "
            "own profile ships, and this is that hash with the correction the current "
            "trait table forces -- nothing was chosen.",
            "The screen that cleared this decode can only reject: passing it means the "
            "read was not caught out, not that it was proven faithful to the tree the "
            "hash was exported from.",
        ]
        for correction in self.corrections:
            notes.append(correction.detail)
        spent = sum(self.unspent)
        if spent:
            trees = ("class", "specialisation", "hero")
            where = ", ".join(
                f"{count} in the {name} tree"
                for name, count in zip(trees, self.unspent, strict=True)
                if count
            )
            notes.append(
                f"The repaired build leaves {spent} talent point(s) unspent ({where}) "
                f"against the ceiling this tier's shipped profiles set. Spending them "
                f"is a search decision and this repair makes none."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "profile": self.profile_id,
            "originalHash": self.original_hash,
            "repairedHash": self.repaired_hash,
            "corrections": [c.to_json() for c in self.corrections],
            "soundness": self.soundness.to_json(),
            "remaining": [
                {"code": f.code, "message": f.message, "simcRefuses": f.simc_refuses}
                for f in self.remaining
            ],
            "refused": self.refused,
            "unspent": list(self.unspent),
            "caveats": self.caveats(),
        }


def observed_framing(loadouts: list[Loadout]) -> tuple[int, int]:
    """The ``spare_bits`` range a corpus of trusted builds actually spans.

    Derived rather than written down, for the same reason ``derive_point_budget`` and
    ``spec_coverage``'s reference list are: a constant typed into a table goes stale in
    the patch where it matters, and the format's padding is a property of whoever
    exported the string. Refuses an empty corpus rather than inventing a range -- a
    screen with no corpus behind it would pass everything.
    """
    if not loadouts:
        raise RepairError("cannot derive a framing range from no loadouts")
    spares = [lo.spare_bits for lo in loadouts if lo.spare_bits is not None]
    if not spares:
        raise RepairError("no loadout in the corpus recorded its framing")
    return min(spares), max(spares)


def check_soundness(
    loadout: Loadout,
    budget: talentedit.PointBudget,
    framing: tuple[int, int],
) -> Soundness:
    """The two screens, applied. See the module docstring for what they are worth."""
    reasons: list[str] = []
    spare = loadout.spare_bits if loadout.spare_bits is not None else 0
    low, high = framing
    if spare < low:
        reasons.append(
            f"the node stream overran the string by {-spare} bit(s); every build in "
            f"the corpus ends between {low} and {high} bits from the end, so this "
            f"reader is out of step with whoever wrote the hash"
        )
    elif spare > high:
        reasons.append(
            f"the node stream stopped {spare} bits short of the end, past the "
            f"{low}-{high} every build in the corpus spans"
        )

    points = (
        loadout.points(talenttree.TREE_CLASS),
        loadout.points(talenttree.TREE_SPEC),
        loadout.points(talenttree.TREE_HERO),
    )
    trees = ("class", "specialisation", "hero")
    for tree, name, spent in zip(talentedit.POINT_TREES, trees, points, strict=True):
        ceiling = budget.per_tree.get(tree)
        if ceiling is not None and spent > ceiling:
            reasons.append(
                f"the build reads {spent} points in the {name} tree, above the "
                f"{ceiling} in {budget.source}"
            )
    return Soundness(ok=not reasons, spare_bits=spare, points=points, reasons=tuple(reasons))


def repair(
    profile_id: str,
    talent_hash: str,
    nodes: dict[int, list[Trait]],
    budget: talentedit.PointBudget,
    framing: tuple[int, int],
) -> Repair:
    """One refused hash, corrected as far as the tree forces and no further.

    The order matters and is not arbitrary. The choice-index overflow is handled by
    the *reader* (``decode_lenient``), because it is the thing that stops the build
    being read at all; the spec-rule offenders are handled after, on the build that
    reading produced. Doing it the other way round is not possible -- there is no
    loadout to inspect until the first is dealt with.

    A hash that decodes strictly is passed through the same path: it simply produces no
    overflow corrections. That keeps one code path for "refused for one reason" and
    "refused for the other", which is what stops the two claims drifting apart.
    """
    loadout, overflows = talenttree.decode_lenient(talent_hash, nodes)
    soundness = check_soundness(loadout, budget, framing)

    corrections = [
        Correction(
            kind="choice-index-dropped",
            node_id=overflow.node_id,
            entry_id=None,
            name=_node_name(nodes, overflow.node_id),
            detail=(
                f"{_node_name(nodes, overflow.node_id)!r} (node {overflow.node_id}) is "
                f"written with choice index {overflow.written_index} and now holds "
                f"{overflow.entries} entry/entries, so the index cannot be written at "
                f"all; the node keeps its first entry, which is what simc's reader "
                f"gives a node carrying no index"
            ),
        )
        for overflow in overflows
    ]

    if not soundness.ok:
        return Repair(
            profile_id=profile_id,
            original_hash=talent_hash,
            repaired_hash=None,
            loadout=None,
            corrections=tuple(corrections),
            soundness=soundness,
            remaining=(),
            refused=(
                "the decode did not survive the soundness screen, so a repair would be "
                "a valid hash for a build nobody wrote: " + "; ".join(soundness.reasons)
            ),
        )

    for offender in talenttree.spec_rule_offenders(loadout, nodes):
        corrections.append(
            Correction(
                kind="node-deselected",
                node_id=offender.node_id,
                entry_id=offender.entry_id,
                name=offender.name,
                detail=(
                    f"{offender.name!r} (node {offender.node_id}, entry "
                    f"{offender.entry_id}) belongs to another specialisation, so this "
                    f"build cannot take it and simc refuses the hash over it; the node "
                    f"is dropped and its {offender.rank} rank(s) left unspent"
                ),
            )
        )
        loadout = talentedit.deselect_node(loadout, offender.node_id)

    if not corrections:
        return Repair(
            profile_id=profile_id,
            original_hash=talent_hash,
            repaired_hash=None,
            loadout=loadout,
            corrections=(),
            soundness=soundness,
            remaining=(),
            refused="nothing to repair: the hash decodes and breaks no rule simc checks",
        )

    try:
        # No framing replay. The source string described a different build, and
        # replaying its length would state a framing this build never had.
        repaired = talenttree.encode_loadout(loadout, nodes, preserve_framing=False)
    except talenttree.TalentEncodeError as error:
        return Repair(
            profile_id=profile_id,
            original_hash=talent_hash,
            repaired_hash=None,
            loadout=None,
            corrections=tuple(corrections),
            soundness=soundness,
            remaining=(),
            refused=f"the corrected build cannot be written as a hash: {error}",
        )

    validation = talentedit.validate_loadout(loadout, nodes, budget=budget)
    if validation.simc_refusals:
        return Repair(
            profile_id=profile_id,
            original_hash=talent_hash,
            repaired_hash=None,
            loadout=None,
            corrections=tuple(corrections),
            soundness=soundness,
            remaining=validation.findings,
            refused=(
                "simc would still refuse the corrected build: "
                + "; ".join(f.message for f in validation.simc_refusals)
            ),
        )

    unspent = tuple(
        max(0, budget.per_tree.get(tree, 0) - loadout.points(tree))
        for tree in talentedit.POINT_TREES
    )
    return Repair(
        profile_id=profile_id,
        original_hash=talent_hash,
        repaired_hash=repaired,
        loadout=loadout,
        corrections=tuple(corrections),
        soundness=soundness,
        remaining=validation.findings,
        unspent=unspent,  # type: ignore[arg-type]
    )


def _node_name(nodes: dict[int, list[Trait]], node_id: int) -> str:
    entries = nodes.get(node_id) or []
    return entries[0].name if entries else f"node {node_id}"


def corpus_from(
    simc_dir: Path, tiers: tuple[str, ...], ptr: bool
) -> tuple[list[Loadout], dict[str, dict[int, list[Trait]]]]:
    """Every strictly-decodable shipped build across some tiers, plus the node tables.

    The corpus is what both screens are derived from, so what goes into it is a
    decision rather than plumbing: **shipped profiles only**. A profile simc left
    switched off is exactly the population being screened, and letting one into the
    corpus would let it widen the range that is supposed to catch it.
    """
    from . import profiles as profiles_mod

    traits = talenttree.parse_trait_data(simc_dir, ptr=ptr)
    tables: dict[str, dict[int, list[Trait]]] = {}
    loadouts: list[Loadout] = []
    for tier in tiers:
        for profile in profiles_mod.discover(simc_dir / "profiles", tier, dps_only=False):
            if not profile.talent_hash or profile.unvalidated:
                continue
            table = tables.setdefault(
                profile.wow_class,
                talenttree.nodes_for_class(traits, talenttree.CLASS_IDS[profile.wow_class]),
            )
            try:
                loadouts.append(talenttree.decode_loadout(profile.talent_hash, table))
            except talenttree.TalentDecodeError:
                continue
    return loadouts, tables
