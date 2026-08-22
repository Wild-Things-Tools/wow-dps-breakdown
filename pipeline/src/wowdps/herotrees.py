"""Which hero tree a build plays, named from simc's own data.

Every specialisation in the game plays a hero-talent tree. SimulationCraft names it
in the profile for most builds (``MID2_Death_Knight_Frost_Rider``), but for the build
it treats as a spec's default it ships the profile with no suffix
(``MID2_Death_Knight_Frost``) -- and that surfaces on the site as a build with *no*
hero tree, which cannot exist. It has one; the name just does not say so.

## What this used to do, and why it does not any more

The first version of this module could not look a tree's name up, because there was
nowhere to look: ``trait_data.inc`` stored the sub-tree SELECTION rows with the
literal name ``"0"``, and the absence was recorded across this repository as a fact
("hero tree names are not in simc's data at all"). So it took the long way round --
run each unnamed profile for one iteration and read which hero-tree-gated abilities
fired, against a **hand-written table of ability slugs per spec**. That worked, and
it had the two costs a hand table always has: it needed a compiled simc and a
simulation per profile, and it answered only for the eight specs somebody had typed
signatures for. When simc shipped MID2 profiles for Balance Druid, Windwalker Monk
and Outlaw Rogue, all three arrived as "Default" and nothing could resolve them.

simc now ships ``__trait_sub_tree_data`` in the same generated file -- ``{id,
"name", class id}`` for all 41 hero trees. That closes the gap completely, because
the *id* was always derivable: a profile's talent hash decodes to exactly one
sub-tree id (``talenttree.Loadout.sub_tree``, the SELECTION node), and simc itself
uses that value the same way. So the resolution is now a pure join of two things
already in a checkout, with **no simc binary, no simulation and no hand-typed
table**, and it answers for every spec rather than for a list somebody maintains.

Verified against the ability-signature method it replaces, on simc ``22b442e``
(2026-08-21): all five builds that method had resolved for MID2 resolve identically
here -- Frost Death Knight to Deathbringer, Beast Mastery to Pack Leader,
Marksmanship and Survival to Sentinel, Subtlety to Deathstalker -- and the three it
could not resolve at all come out as Trickster (Outlaw), Elune's Chosen (Balance)
and Shado-Pan (Windwalker). Two independent derivations agreeing on five of five is
the same check ``talenttree``'s own docstring rests on, run the other way round.

## It names every build, not only the unnamed ones

A profile name carries whatever abbreviation simc's file naming used, and two of
those are not the tree's name: MID2 spells Scalecommander ``SB``/``SC`` and
Demonology's Soul Harvester ``Soulharvester``. The canonical name is now available
for those too, so this resolves *every* build and the display follows simc's data
rather than its filenames.

**The resolution feeds the display, never the id.** ``SpecProfile.name_hero`` is the
suffix the profile name carried and is what ``SpecProfile.id`` is built from, so
``evoker_devastation_sc`` stays ``evoker_devastation_sc`` and every dataset that
joins on it keeps joining. ``hero_talent`` carries the tree's real name. Both are
true and neither moves the joins -- the same rule ``profiles.HERO_ALIASES`` states
from the other side.

The result is written per tier to ``data/hero_trees.json`` by ``wowdps hero-trees``
and read back by ``profiles.discover``. It is data, generated from simc and checked
in, so a tier that has not been processed falls back to the profile-name answer
rather than the site inventing one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Resolution:
    """What one pass over a tier's profiles could and could not name."""

    #: ``{profile's internal name: hero tree name}``, for every profile resolved.
    resolved: dict[str, str] = field(default_factory=dict)
    #: Profiles whose tree could not be established, with the reason.
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    #: ``(profile, name the profile carried, name simc's table gives)`` where the two
    #: differ. Reported rather than silently applied: an abbreviation being expanded
    #: is expected, and a profile whose suffix names a *different* tree from the one
    #: its talent hash selects is a finding about the profile.
    renamed: list[tuple[str, str, str]] = field(default_factory=list)


def resolve_tier(
    profiles_dir: Path,
    tier: str,
    simc_dir: Path,
    ptr: bool = False,
    dps_only: bool = True,
) -> Resolution:
    """Name the hero tree of every profile of one tier, from simc's data alone.

    Decodes each profile's talent hash against simc's trait table, reads the sub-tree
    the SELECTION node names, and looks that id up in ``__trait_sub_tree_data``.
    """
    from . import profiles as profiles_module
    from . import talenttree

    traits = talenttree.parse_trait_data(simc_dir, ptr=ptr)
    names = talenttree.parse_sub_tree_names(simc_dir, ptr=ptr)
    result = Resolution()
    if not names:
        log.warning(
            "%s ships no __trait_sub_tree_data table, so no hero tree can be named "
            "from it -- nothing is resolved rather than guessed",
            simc_dir,
        )
        return result

    nodes_by_class: dict[int, dict[int, list[talenttree.Trait]]] = {}
    for profile in profiles_module.discover(profiles_dir, tier, dps_only=dps_only):
        label = profile.profile_name or profile.path.stem
        class_id = talenttree.CLASS_IDS.get(profile.wow_class)
        if class_id is None or not profile.talent_hash:
            result.unresolved.append((label, "the profile states no talent hash"))
            continue
        nodes = nodes_by_class.setdefault(class_id, talenttree.nodes_for_class(traits, class_id))
        try:
            loadout = talenttree.decode_loadout(profile.talent_hash, nodes)
        except talenttree.TalentDecodeError as exc:
            result.unresolved.append((label, f"the talent hash does not decode: {exc}"))
            continue
        sub_tree = loadout.sub_tree
        if sub_tree is None:
            result.unresolved.append(
                (label, "the loadout selects no hero tree (nothing spent in one)")
            )
            continue
        entry = names.get(sub_tree)
        if entry is None:
            result.unresolved.append(
                (label, f"sub-tree {sub_tree} is not in simc's hero tree table")
            )
            continue
        result.resolved[label] = entry.name
        if profile.name_hero and profile.name_hero != entry.name:
            result.renamed.append((label, profile.name_hero, entry.name))
    return result


def _data_file() -> Path:
    from importlib import resources

    return Path(str(resources.files("wowdps.data") / "hero_trees.json"))


def load_overrides(tier: str, path: Path | None = None) -> dict[str, str]:
    """The resolved ``{profile internal name: hero tree}`` map for one tier.

    Empty when nothing has been generated: `profiles.discover` then falls back to
    whatever the profile's own name said, exactly as before this existed.
    """
    source = path or _data_file()
    if not source.is_file():
        return {}
    raw = json.loads(source.read_text(encoding="utf-8"))
    entry = (raw.get("tiers") or {}).get(tier) or {}
    return {str(k): str(v) for k, v in (entry.get("resolved") or {}).items()}


def write_overrides(tier: str, resolved: dict[str, str], path: Path | None = None) -> Path:
    """Merge one tier's resolved map into the checked-in data file.

    The tier's entry is replaced rather than merged into: a pass names every profile
    the tier ships, so a name left over from a profile simc has since removed would
    be a stale answer nothing corrects. Other tiers are untouched. The file is left
    alone when nothing changed, so a nightly run over an unmoved tier commits
    nothing -- the same rule the datasets follow.
    """
    target = path or _data_file()
    raw: dict = {}
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8"))
    raw["note"] = (
        "Hero trees per profile, decoded from each profile's talent hash against "
        "simc's own trait table. Written by `wowdps hero-trees`; feeds the display "
        "name only, never a build id."
    )
    tiers = raw.setdefault("tiers", {})
    tiers[tier] = {"resolved": dict(sorted(resolved.items()))}
    text = json.dumps(raw, indent=1) + "\n"
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
    return target
