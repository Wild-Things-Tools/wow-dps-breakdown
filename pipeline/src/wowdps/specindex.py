"""Every class and every spec in the game, derived from simc rather than typed.

The Spec detail view needs a picker that shows the *whole* game -- all thirteen
classes, all their specs, tanks and healers included -- so that a spec's absence
from the rankings reads as absence rather than as a bad result. That is the same
argument the coverage panel rests on, one level closer to the reader.

Four things go into it, and each has exactly one honest source:

``sc_spec_list.inc``
    Class index to its specs, in the game's own order. Druid has four, everything
    else three, and Midnight's new Demon Hunter spec (Devourer, 1480) is in it --
    which is the argument against a hand-written table in one line.

``sc_specialization_data.inc``
    The specialization enum: spec name to canonical spec id, for every spec.

``profiles/<tier>/*.simc``
    The ``role=`` line, and whether this tier ships a profile at all. Role is the
    one field simc's *generated* data does not carry -- there is no role column in
    ``sc_specialization_data.inc`` and no role table under ``generated/`` (checked)
    -- so it comes from the profiles, and a spec no tier has ever shipped has an
    unknown role rather than an assumed one.

``trait_data.inc``
    Which hero trees a spec can play, from the hero nodes' own ``sub_tree`` and
    ``id_spec``. Two specs of a class share each tree, which is what lets the
    picker draw the trees between the specs that own them.

**Hero tree names used to be absent, and are not any more.** The SELECTION rows
that identify a tree still carry the literal string ``"0"`` where a name would be,
which is why this module reported a tree as named only when some build played one
-- 24 of 41 for MID2, so every tree belonging to a spec nobody profiles was drawn
as a blank. simc ships ``__trait_sub_tree_data`` in the same file now: id, name and
class for all 41. Names therefore come from simc's table, and a build playing a
tree is kept only as a cross-check -- a build whose own name disagrees with the
table is logged rather than resolved by picking one.

That is what makes coverage answerable **per hero tree** rather than per spec.
A spec plays two trees, this tier may ship a build for one of them, and until the
trees could all be named there was no way to say which one was missing. See
``hero_tree_coverage``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import talenttree
from .profiles import CLASS_TOKENS, discover

log = logging.getLogger(__name__)

_ENUM_ROW = re.compile(r"^\s*([A-Z][A-Z_0-9]*)\s*=\s*(\d+),", re.M)
_GROUP = re.compile(r"\{([^{}]*)\}")

#: simc's ``player_e`` order, which is the row order of ``__class_spec_id``. Index
#: 0 is the pet block; the thirteen playable classes follow. Read off the file
#: rather than assumed: the group count is asserted below, so a class added to the
#: game fails loudly here instead of shifting every class silently by one.
_CLASS_ORDER = (
    "Warrior",
    "Paladin",
    "Hunter",
    "Rogue",
    "Priest",
    "Death Knight",
    "Shaman",
    "Mage",
    "Warlock",
    "Monk",
    "Druid",
    "Demon Hunter",
    "Evoker",
)

#: Roles simc writes in a profile's ``role=`` line that are not damage.
_TANK_ROLES = {"tank"}
_HEALER_ROLES = {"heal", "healer"}


@dataclass
class SpecEntry:
    """One specialization of one class."""

    spec_id: int
    name: str
    wow_class: str
    #: "damage" | "tank" | "healer" | "unknown" -- see the module docstring on why
    #: unknown is a real state rather than a gap to be filled in.
    role: str = "unknown"
    #: Does simc ship a profile for this spec in *this* tier?
    profiled: bool = False
    #: Does simc ship one for it in any tier? Distinguishes "never simulated" from
    #: "not simulated this season", which is the distinction the coverage panel
    #: exists for.
    profiled_ever: bool = False
    #: Builds this tier actually publishes for the spec, by dataset id.
    builds: list[str] = field(default_factory=list)
    #: Hero sub-tree ids this spec can play, from the trait table.
    sub_trees: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "specId": self.spec_id,
            "name": self.name,
            "class": self.wow_class,
            "role": self.role,
            "profiled": self.profiled,
            "profiledEver": self.profiled_ever,
            "builds": self.builds,
            "subTrees": self.sub_trees,
        }


@dataclass
class HeroTree:
    """One hero talent tree, and the specs that can play it."""

    sub_tree: int
    wow_class: str
    spec_ids: list[int]
    #: From simc's ``__trait_sub_tree_data``. None only on a checkout old enough not
    #: to ship that table, where the previous behaviour (named by a build that plays
    #: it) is what remains.
    name: str | None = None
    #: Dataset ids of this tier's builds that play it. Empty is the whole point: a
    #: tree with no build is a hole in the coverage, not a missing name.
    builds: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "subTree": self.sub_tree,
            "class": self.wow_class,
            "specIds": self.spec_ids,
            "name": self.name,
            "builds": self.builds,
        }


def parse_spec_enum(simc_dir: Path, ptr: bool = False) -> dict[str, int]:
    """``WARRIOR_ARMS`` -> 71, for every specialization simc knows."""
    name = "sc_specialization_data_ptr.inc" if ptr else "sc_specialization_data.inc"
    text = (simc_dir / "engine" / "dbc" / "generated" / name).read_text(encoding="utf-8")
    return {match.group(1): int(match.group(2)) for match in _ENUM_ROW.finditer(text)}


def parse_spec_list(simc_dir: Path, ptr: bool = False) -> list[list[str]]:
    """Each class's specs in the game's order, as enum names.

    The pet block is dropped; the remaining rows are the playable classes in
    ``player_e`` order. A row count that does not match ``_CLASS_ORDER`` raises,
    because a silent off-by-one here would relabel every class in the picker.
    """
    name = "sc_spec_list_ptr.inc" if ptr else "sc_spec_list.inc"
    text = (simc_dir / "engine" / "dbc" / "generated" / name).read_text(encoding="utf-8")
    # The PTR table is `__ptr_class_spec_id`, the live one `__class_spec_id`. Both
    # end in the same suffix, so splitting on that reads either without a branch --
    # and a file with neither raises here rather than yielding an empty class list.
    marker = "class_spec_id"
    if marker not in text:
        raise ValueError(f"{name} has no class_spec_id table")
    body = text.split(marker, 1)[1]
    groups = []
    for raw in _GROUP.findall(body):
        names = [token.strip() for token in raw.split(",") if token.strip()]
        names = [token for token in names if token and token != "SPEC_NONE"]
        if names:
            groups.append(names)

    # The first group is the pet specialisations, which are not player classes.
    if groups and groups[0][0].startswith("PET_"):
        groups = groups[1:]
    if len(groups) != len(_CLASS_ORDER):
        raise ValueError(
            f"sc_spec_list.inc has {len(groups)} class rows, expected "
            f"{len(_CLASS_ORDER)}. A class was added or removed and the order this "
            f"module assumes has to be re-checked rather than shifted."
        )
    return groups


def _pretty_spec(enum_name: str, wow_class: str) -> str:
    """``DEATH_KNIGHT_UNHOLY`` -> ``Unholy``, given the class it belongs to."""
    prefix = wow_class.upper().replace(" ", "_") + "_"
    tail = enum_name[len(prefix) :] if enum_name.startswith(prefix) else enum_name
    return " ".join(part.capitalize() for part in tail.split("_"))


def hero_trees(simc_dir: Path, ptr: bool = False) -> dict[int, HeroTree]:
    """Every hero tree, keyed by sub-tree id, with the specs that can play it.

    Read off the *hero* nodes rather than the SELECTION nodes: a selection row
    names one spec at a time, while a hero node carries the sub-tree it belongs to
    and the specs allowed to take it, which is the pairing the picker draws.
    """
    traits = talenttree.parse_trait_data(simc_dir, ptr=ptr)
    found: dict[int, HeroTree] = {}
    for trait in traits:
        if trait.tree_index != talenttree.TREE_HERO or not trait.sub_tree:
            continue
        wow_class = _CLASS_ORDER[trait.class_id - 1] if 1 <= trait.class_id <= 13 else "?"
        entry = found.setdefault(
            trait.sub_tree, HeroTree(sub_tree=trait.sub_tree, wow_class=wow_class, spec_ids=[])
        )
        for spec_id in trait.spec_ids:
            if spec_id and spec_id not in entry.spec_ids:
                entry.spec_ids.append(spec_id)
    for entry in found.values():
        entry.spec_ids.sort()
    return found


def builds_by_sub_tree(document: dict) -> dict[str, int]:
    """``{dataset build id: hero sub-tree id}``, from a published talent-trees file.

    The sub-tree is the one thing that says *which* of a spec's two trees a build
    plays, and it is already decoded: ``build["tree"]`` is ``<specId>-<subTree>``.
    Note the key inside that file is called ``specId`` and holds the dataset's build
    id, not a specialization id.
    """
    found: dict[str, int] = {}
    for build in document.get("builds") or []:
        tree = str(build.get("tree") or "")
        build_id = build.get("specId")
        if not build_id or "-" not in tree:
            continue
        try:
            found[str(build_id)] = int(tree.rsplit("-", 1)[1])
        except ValueError:
            continue
    return found


def refused_profiles(
    simc_dir: Path, tier: str, ptr: bool = False, dps_only: bool = True
) -> list[dict]:
    """Profiles simc ships or wrote whose stored talent hash the current tree refuses.

    This is the state between "simc wrote a profile" and "there is a number": the
    file is complete, and simc will not build an actor out of it. It has been visible
    in this project before -- 15 of MID1's 41 damage profiles are in it -- but only
    as a spec that quietly failed to appear, because establishing it meant running
    simc over every profile (``wowdps check-profiles``).

    It does not. ``talenttree`` decodes the hash and ``spec_rule_violation`` applies
    simc's own spec rule, so the verdict *and the node id simc would name* come out
    of a checkout with no binary in it. Measured on MID2 (2026-08-22, simc 22b442e):
    **6 of 51 profiles refused, every one of them from the disabled generator set** --
    both Havoc builds, both Retribution builds, Arms and Fury -- and all 35 profiles
    simc actually ships pass. Those six are exactly the builds that produced nothing
    in the nightly run.
    """
    try:
        found = discover(simc_dir / "profiles", tier, dps_only=dps_only)
    except FileNotFoundError:
        # simc deletes an old tier's profiles while `tiers.json` still lists it, and
        # `build_index` tolerates that already. Raising here would crash the publish
        # loop on a stale tier instead of publishing it with nothing refused.
        log.warning("simc no longer ships %s; no profile can be checked for refusal", tier)
        return []

    traits = talenttree.parse_trait_data(simc_dir, ptr=ptr)
    nodes_by_class: dict[int, dict[int, list[talenttree.Trait]]] = {}
    refused: list[dict] = []
    for profile in found:
        class_id = talenttree.CLASS_IDS.get(profile.wow_class)
        if class_id is None or not profile.talent_hash:
            continue
        nodes = nodes_by_class.setdefault(class_id, talenttree.nodes_for_class(traits, class_id))
        refusal = _refusal(profile.talent_hash, nodes)
        if refusal is None:
            continue
        reason, simc_message = refusal
        refused.append(
            {
                "class": profile.wow_class,
                "spec": profile.spec,
                "profile": profile.profile_name or profile.path.stem,
                "heroTree": profile.hero_talent,
                "unvalidated": profile.unvalidated,
                "reason": reason,
                # simc's own line for the first thing it trips on, punctuation
                # included, or None where nothing can be quoted. Separate from
                # `reason` on purpose: see `_refusal`.
                "simcMessage": simc_message,
            }
        )
    return refused


def _refusal(
    talent_hash: str, nodes: dict[int, list[talenttree.Trait]]
) -> tuple[str, str | None] | None:
    """Why simc will refuse this hash, in our words, and in simc's -- or None.

    Two fields because they are two claims, and publishing one of them as the other is
    the defect this replaces (issue #43). The site's coverage panel prints *"simc will
    not load it: <reason>"*, and what stood in `reason` for four of MID2's six refused
    profiles was `decode_loadout`'s own message -- "choice index 1 out of bounds for
    node 91020 (1 entries)" -- a sentence simc **never emits**. simc's line for that
    node is one of its other refusals entirely ("Node 91020 is not a choice node but has
    index selection."), so a reader grepping a real run's stderr for the published text
    found nothing and could not tell our misprediction from a simc change.

    So `reason` is deliberately, visibly **ours**: lower-case prose that reads on from
    the panel's own lead-in and never impersonates simc. `simcMessage` is simc's, from
    the one place its literals live (`talenttree.SIMC_*`), for anyone matching output.

    **And the count is part of being true.** `decode_loadout` and
    `spec_rule_violation` both stop at the *first* failure, so a reason naming one node
    read as "one node is wrong" when the real figures on MID2 are 5 for Havoc Aldrachi
    Reaver, 7 and 4 for the two Retribution builds and 2 each for Arms and Fury. The
    wording carries the number, and where it comes from reading on past the point simc
    stops at, it says so -- that reading is `decode_lenient`, which is out of step with
    whoever wrote the hash by definition, so the figure is the extent of the problem
    rather than a tally to be quoted elsewhere.
    """
    try:
        loadout = talenttree.decode_loadout(talent_hash, nodes)
    except talenttree.TalentDecodeError as exc:
        return _decode_refusal(talent_hash, nodes, exc)
    offenders = talenttree.spec_rule_offenders(loadout, nodes)
    if not offenders:
        return None
    first = offenders[0]
    simc_message = talenttree.SIMC_SPEC_RULE.format(node=first.node_id, entry=first.entry_id)
    named = f"node {first.node_id} ({first.name or 'unnamed'}, entry {first.entry_id})"
    if len(offenders) == 1:
        return (
            f"{named} belongs to another specialisation, so this build cannot take it",
            simc_message,
        )
    return (
        f"{len(offenders)} of the selected nodes belong to another specialisation; the "
        f"first is {named}, which is where simc stops",
        simc_message,
    )


def _node_name(nodes: dict[int, list[talenttree.Trait]], node_id: int) -> str:
    """What the trait table calls a node, for a reason a person has to read.

    A bare id says nothing to anyone who is not holding the table; the name is the
    half that makes the sentence checkable against the game.
    """
    entries = nodes.get(node_id) or []
    return entries[0].name if entries and entries[0].name else "unnamed"


def _decode_refusal(
    talent_hash: str,
    nodes: dict[int, list[talenttree.Trait]],
    exc: talenttree.TalentDecodeError,
) -> tuple[str, str | None]:
    """A hash the strict reader will not finish, described without pretending to be simc.

    The lenient read is what turns "one node" into the real extent, and it is also the
    only thing that can say *which* of simc's two choice wordings applies -- that is
    decided by the node's type, not by the index. Where even the lenient reader raises,
    there is nothing to quote and nothing to count, and the raise itself is the evidence.
    """
    try:
        _loadout, overflows = talenttree.decode_lenient(talent_hash, nodes)
    except talenttree.TalentDecodeError:
        overflows = ()
    if not overflows:
        # Not a choice overflow at all -- a version, an alphabet or a length failure.
        # Ours entirely: simc has its own line for each and this reader has not
        # established which one, so quoting any of them would be a guess.
        return f"this project's reader cannot decode the hash: {exc}", None

    first = overflows[0]
    if first.node_type in (talenttree.NODE_CHOICE, talenttree.NODE_SELECTION):
        what = (
            f"the hash takes option {first.written_index + 1} at node {first.node_id} "
            f"({_node_name(nodes, first.node_id)}), and this tier's tree gives that node "
            f"only {first.entries} to choose from"
        )
    else:
        what = (
            f"the hash chooses between options at node {first.node_id} "
            f"({_node_name(nodes, first.node_id)}), which this tier's tree gives a single "
            f"entry and no choice"
        )
    if len(overflows) == 1:
        return f"{what} -- and it is the only node in the hash written that way", (
            talenttree.simc_choice_refusal(first)
        )
    return (
        f"{what}; reading on past the point simc stops at finds {len(overflows)} nodes "
        f"written that way, so the hash is stale in more than one place",
        talenttree.simc_choice_refusal(first),
    )


#: How a damage spec stands in this tier, in the manifest coverage block's own words.
#: Order matters: a spec appearing in two lists takes the first that matches, and
#: "shipped but produced nothing" is the stronger claim than "shipped".
_COVERAGE_STATES = ("broken", "unvalidated", "shipped", "missing")


def hero_tree_coverage(
    manifest: dict,
    trees: dict[int, HeroTree],
    spec_ids: dict[tuple[str, str], int],
    build_sub_trees: dict[str, int],
    refused: list[dict] | None = None,
) -> dict | None:
    """Which (damage spec x hero tree) pairs this tier has a build for, and which not.

    The coverage panel could only ever say "this spec is absent", because until every
    tree had a name there was nothing to call the half of a spec that is missing. A
    spec plays two hero trees and a tier routinely ships a build for one of them:
    measured on MID2 (2026-08-22, simc 22b442e) **35 of 53 spec-and-hero-tree pairs
    have a build**, where the spec-level count reads 17 of 26. Survival Hunter is
    simulated and Pack Leader Survival is not, and only this number says so.

    53 rather than 52 because the pairing is read out of the trait table rather than
    assumed: Havoc carries three trees there today (Fel-Scarred, Aldrachi Reaver and
    Midnight's new Void-Scarred), so "every spec plays two" is a rule about the game
    that this does not encode.

    Every cell carries the state of its *spec* from the manifest's own coverage
    block, so the three reasons a spec can be absent are not re-derived here and
    cannot drift from the panel above it. ``None`` when the manifest predates that
    block: an empty coverage claim would read as complete coverage.
    """
    coverage = manifest.get("coverage") or {}
    if not coverage.get("damageSpecsKnown"):
        return None

    # Placing a build in a hero tree needs `talent-trees.json`, and without it every
    # build is unplaceable. Answering anyway reported **cells=53, covered=0** and
    # listed all 34 shipped specs as having no build for either tree -- so the panel
    # would have said "Arcane Mage: no Sunfury build, no Spellslinger build" directly
    # above both of them in the ranking. Refusing is the only honest answer: the
    # caller then publishes null and the panel falls back to spec-level coverage,
    # which is what its warning already promised.
    builds = manifest.get("specs") or []
    if builds and not any(build["id"] in build_sub_trees for build in builds):
        log.warning(
            "no build could be placed in a hero tree (%d builds, %d placements known)"
            " -- publishing no hero tree coverage rather than reporting every spec as"
            " uncovered",
            len(builds),
            len(build_sub_trees),
        )
        return None

    state_of: dict[tuple[str, str], str] = {}
    for state in _COVERAGE_STATES:
        for entry in coverage.get(state) or []:
            state_of.setdefault((entry["class"], entry["spec"]), state)

    plays: dict[tuple[str, str], set[int]] = {}
    builds_of: dict[tuple[tuple[str, str], int], list[str]] = {}
    unplaced: list[dict] = []
    for build in manifest.get("specs") or []:
        sub_tree = build_sub_trees.get(build["id"])
        if sub_tree is None:
            continue
        key = (build["class"], build["spec"])
        plays.setdefault(key, set()).add(sub_tree)
        builds_of.setdefault((key, sub_tree), []).append(build["id"])
        tree = trees.get(sub_tree)
        spec_id = spec_ids.get(key)
        if tree is not None and spec_id is not None and spec_id not in tree.spec_ids:
            # simc's trait table places some trees on no spec at all (Annihilator
            # carries no id_spec on any of its nodes today). A build plainly plays
            # it, so the pairing is reported as a gap in the table rather than
            # silently dropped from the count.
            unplaced.append({"build": build["id"], "subTree": sub_tree, "tree": tree.name})

    cells = 0
    covered = 0
    uncovered: list[dict] = []
    for key in sorted(state_of):
        spec_id = spec_ids.get(key)
        if spec_id is None:
            continue
        for sub_tree in sorted(t.sub_tree for t in trees.values() if spec_id in t.spec_ids):
            cells += 1
            if sub_tree in plays.get(key, set()):
                covered += 1
                continue
            tree_name = trees[sub_tree].name if sub_tree in trees else None
            # A refusal names the profile that will not load. One that carries a hero
            # tree answers for that cell only; one simc ships unnamed answers for the
            # spec, because there is no second build to distinguish it from.
            candidates = [
                entry
                for entry in (refused or [])
                if (entry["class"], entry["spec"]) == key and entry["heroTree"] in (None, tree_name)
            ]
            # A refusal naming this very tree beats one that names none. Retribution
            # ships two disabled builds refused at two different nodes, one of them
            # unnamed, and taking the first match would print the unnamed build's
            # node against the named build's tree.
            candidates.sort(key=lambda entry: entry["heroTree"] != tree_name)
            reason = candidates[0]["reason"] if candidates else None
            uncovered.append(
                {
                    "class": key[0],
                    "spec": key[1],
                    "specId": spec_id,
                    "subTree": sub_tree,
                    "tree": tree_name,
                    "state": state_of[key],
                    "reason": reason,
                }
            )

    for (_key, sub_tree), ids in builds_of.items():
        tree = trees.get(sub_tree)
        if tree is not None:
            tree.builds = sorted(set(tree.builds) | set(ids))

    return {
        "cells": cells,
        "covered": covered,
        "uncovered": uncovered,
        "unplaced": unplaced,
    }


def build_index(
    simc_dir: Path,
    tier: str,
    manifest: dict | None = None,
    tree_names: dict[int, str] | None = None,
    ptr: bool = False,
    build_sub_trees: dict[str, int] | None = None,
    refused: list[dict] | None = None,
) -> dict:
    """The whole picker's data: every class, every spec, every hero tree.

    ``manifest`` is this tier's ``index.json``; its ``specs`` array says which
    builds exist and is what turns a spec from "simc ships a profile" into "this
    dataset has something to show". ``build_sub_trees`` maps a build id to the hero
    tree it plays, from ``builds_by_sub_tree`` -- that is what makes coverage
    answerable per hero tree rather than per spec.

    ``tree_names`` is no longer the source of a name -- simc's own table is -- and
    is kept as a cross-check: a tree that some build calls something other than what
    simc's table calls it is logged and the table wins.
    """
    enum = parse_spec_enum(simc_dir, ptr=ptr)
    groups = parse_spec_list(simc_dir, ptr=ptr)
    trees = hero_trees(simc_dir, ptr=ptr)
    for sub_tree, entry in talenttree.parse_sub_tree_names(simc_dir, ptr=ptr).items():
        # A tree simc names but whose nodes place it on no spec still belongs in the
        # index: the picker draws it, and dropping it would hide the gap.
        class_name = (
            _CLASS_ORDER[entry.class_id - 1] if 1 <= entry.class_id <= len(_CLASS_ORDER) else "?"
        )
        trees.setdefault(sub_tree, HeroTree(sub_tree=sub_tree, wow_class=class_name, spec_ids=[]))
        trees[sub_tree].name = entry.name
    for sub_tree, name in (tree_names or {}).items():
        played = trees.get(sub_tree)
        if played is not None and played.name and played.name != name:
            log.warning(
                "sub-tree %d is called %r by simc's table and %r by a build that "
                "plays it; the table wins",
                sub_tree,
                played.name,
                name,
            )
        elif played is not None and not played.name:
            played.name = name

    # Role and "has a profile" come from the profiles directory, for this tier and
    # for every tier simc ships -- the two answer different questions and the
    # picker shows both.
    roles: dict[tuple[str, str], str] = {}
    this_tier: set[tuple[str, str]] = set()
    ever: set[tuple[str, str]] = set()
    profiles_dir = simc_dir / "profiles"
    for candidate in sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()):
        try:
            found = discover(profiles_dir, candidate, dps_only=False)
        except FileNotFoundError:
            continue
        for profile in found:
            key = (profile.wow_class, profile.spec)
            ever.add(key)
            roles.setdefault(key, _role_of(profile.role))
            if candidate == tier:
                this_tier.add(key)

    builds_by_spec: dict[tuple[str, str], list[str]] = {}
    for build in (manifest or {}).get("specs", []):
        builds_by_spec.setdefault((build["class"], build["spec"]), []).append(build["id"])

    classes = []
    for index, enum_names in enumerate(groups):
        wow_class = _CLASS_ORDER[index]
        specs = []
        for enum_name in enum_names:
            spec_id = enum.get(enum_name)
            if spec_id is None:
                log.warning("no id for %s; skipping", enum_name)
                continue
            spec_name = _pretty_spec(enum_name, wow_class)
            key = (wow_class, spec_name)
            specs.append(
                SpecEntry(
                    spec_id=spec_id,
                    name=spec_name,
                    wow_class=wow_class,
                    role=roles.get(key, "unknown"),
                    profiled=key in this_tier,
                    profiled_ever=key in ever,
                    builds=sorted(builds_by_spec.get(key, [])),
                    sub_trees=sorted(
                        tree.sub_tree for tree in trees.values() if spec_id in tree.spec_ids
                    ),
                )
            )
        classes.append(
            {
                "class": wow_class,
                "token": next(
                    (token for token, (label, _) in CLASS_TOKENS.items() if label == wow_class),
                    None,
                ),
                "specs": [spec.to_json() for spec in specs],
            }
        )

    spec_ids = {
        (spec["class"], spec["name"]): spec["specId"]
        for entry in classes
        for spec in entry["specs"]
    }
    coverage = hero_tree_coverage(manifest or {}, trees, spec_ids, build_sub_trees or {}, refused)

    return {
        "tier": tier,
        "classes": classes,
        "heroTrees": [trees[key].to_json() for key in sorted(trees)],
        "heroTreeCoverage": coverage,
        "refusedProfiles": refused if refused is not None else None,
        "note": (
            "Every class and spec simc knows, from sc_spec_list.inc and "
            "sc_specialization_data.inc. Role comes from the profiles' role= line, "
            "so a spec no tier has ever shipped has role 'unknown' rather than an "
            "assumed one. Hero tree names and the specs that can play them come from "
            "trait_data.inc -- __trait_sub_tree_data and the hero nodes' own id_spec."
        ),
    }


def _role_of(simc_role: str) -> str:
    role = (simc_role or "").lower()
    if role in _TANK_ROLES:
        return "tank"
    if role in _HEALER_ROLES:
        return "healer"
    return "damage"


def tree_names_from_talents(document: dict) -> dict[int, str]:
    """Sub-tree id to hero tree name, joined out of a published talent-trees file.

    The only source of a name there is: simc's SELECTION rows carry the literal
    ``"0"``. Each build's ``tree`` key is ``<specId>-<subTree>`` and its
    ``heroTalent`` is the resolved tree, so a build that plays a tree names it. A
    tree nobody plays stays unnamed, which is the true state rather than a gap.
    """
    names: dict[int, str] = {}
    for build in document.get("builds") or []:
        tree = str(build.get("tree") or "")
        hero = build.get("heroTalent")
        if "-" not in tree or not hero or hero == "Default":
            continue
        try:
            sub_tree = int(tree.rsplit("-", 1)[1])
        except ValueError:
            continue
        existing = names.get(sub_tree)
        if existing and existing != hero:
            # Two builds disagreeing about what one tree is called is a finding,
            # not something to resolve by picking one.
            log.warning("sub-tree %d is called both %r and %r", sub_tree, existing, hero)
            continue
        names[sub_tree] = hero
    return names


def write_spec_index(out_dir: Path, document: dict) -> Path:
    """Write ``<tier>/spec-index.json``."""
    import json

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "spec-index.json"
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    return path
