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

**Hero tree *names* are not in simc.** The SELECTION rows that identify a tree
carry the literal string ``"0"`` where a name would be, and ``TraitSubTree`` -- the
table holding the name and the texture atlas element -- is not shipped. This is
the same absence as item source and as the Mythic+ rotation: it is not hiding
somewhere else in the checkout. What *is* available is a join: a build's decoded
loadout yields the sub-tree id it plays, and the build already knows its tree name
from ``herotrees``. So a tree is named exactly when some build plays it, which is
also exactly when the picker has something to select -- the unnamed ones belong to
specs that are greyed out anyway.
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
    #: None when no build in any known tier plays it -- simc ships no name.
    name: str | None = None

    def to_json(self) -> dict:
        return {
            "subTree": self.sub_tree,
            "class": self.wow_class,
            "specIds": self.spec_ids,
            "name": self.name,
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


def build_index(
    simc_dir: Path,
    tier: str,
    manifest: dict | None = None,
    tree_names: dict[int, str] | None = None,
    ptr: bool = False,
) -> dict:
    """The whole picker's data: every class, every spec, every hero tree.

    ``manifest`` is this tier's ``index.json``; its ``specs`` array says which
    builds exist and is what turns a spec from "simc ships a profile" into "this
    dataset has something to show". ``tree_names`` maps sub-tree id to name for the
    trees some build plays -- see the module docstring on why that join is the only
    source of a name.
    """
    enum = parse_spec_enum(simc_dir, ptr=ptr)
    groups = parse_spec_list(simc_dir, ptr=ptr)
    trees = hero_trees(simc_dir, ptr=ptr)
    names = tree_names or {}
    for sub_tree, name in names.items():
        if sub_tree in trees:
            trees[sub_tree].name = name

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

    return {
        "tier": tier,
        "classes": classes,
        "heroTrees": [trees[key].to_json() for key in sorted(trees)],
        "note": (
            "Every class and spec simc knows, from sc_spec_list.inc and "
            "sc_specialization_data.inc. Role comes from the profiles' role= line, "
            "so a spec no tier has ever shipped has role 'unknown' rather than an "
            "assumed one. Hero tree names are not in simc's data at all -- a tree is "
            "named only when some build plays it."
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
