"""Builds this project supplies for the tier's missing (spec, hero tree) cells.

The owner's requirement is that every damage spec appears in the ranking with two
distinct, valid hero-tree builds. simc does not ship that: MID2 ships nothing at
all for four damage specs (their stored hashes are refused by simc's own parser)
and one build of two for eight more. The machinery to fill each kind of gap exists
-- a repaired hash, a harvested real-player build, a hero-tree swap plus a search
-- and this module is only the *materialisation*: it turns a committed list of
cells into profile files the nightly's shards pick up exactly the way they pick up
the unvalidated profiles, with no flag threaded through twelve jobs.

The state travels **in the file**, for the same reason ``unvalidated.MARKER``
does: a sharded run materialises these separately in every job, and a side channel
could be set in one and forgotten in another. Three markers:

* ``# wowdps-unvalidated <generator>`` -- kept when the cell's *character* (gear,
  race, consumables) is simc's disabled generator profile. The claim it carries is
  about the character, and it stays true whatever the talents are.
* ``# wowdps-origin <origin>`` -- where the *talents* come from: ``repaired``
  (simc's own stored hash with the correction the trait table forces),
  ``harvested`` (a hash a real player was logged killing a boss with) or
  ``computed`` (a hero-tree swap of the shipped sibling, refined by a one-edit
  search). Read back by ``profiles.parse_profile`` and published on the row.
* ``# wowdps-origin-note <sentence>`` -- the evidence, in one sentence, published
  as the build's first caveat so a reader is never left inferring the claim from
  the mark alone.

What each cell is and is not
----------------------------

* A **second-tree cell on a shipped spec** wears the shipped sibling's gear
  verbatim -- the body is the sibling profile with only the player name and the
  ``talents=`` line changed. simc's own two-build specs differ in exactly that
  way (one APL, branching on ``talent.``), so the number is comparable the way
  simc's own numbers are, and no gear caveat fires.
* A **cell on an absent spec** is the disabled generator profile's character with
  working talents. Its gear is a whole tier behind the shipped band, so the
  existing caveat machinery (``gearComparable``, ``tierSetComparable``,
  ``unvalidated``) derives its flags exactly as it does for the disabled profiles
  themselves. Nothing here anchors gear into false comparability.
* A **harvested** cell carries a real player's *talents* and nothing else --
  ``harvest`` never equips the player's gear, and the note says so.

Refusals, all deliberate:

* a cell whose hash no longer decodes, states another spec, lands on a different
  hero tree than the cell claims, or violates simc's own spec rule is **not
  written** -- the pipeline must never materialise a profile simc will refuse or,
  worse, one that quietly plays the wrong tree;
* a shipped profile is **never overwritten** -- where simc publishes a file of
  the same name, that is the better claim and this module has nothing to add. A
  file this project itself materialised (any ``wowdps-`` marker) may be replaced,
  which is what lets a cell supersede the refused unvalidated profile of the same
  name once the ``unvalidated`` step has run;
* with **no trait table** the hashes cannot be checked, and the cells are written
  unchecked with a warning rather than dropped: simc itself is the final gate (a
  refused profile produces no row, visibly), while dropping the cells would cost
  the night's coverage for a missing include file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import talenttree, unvalidated
from .profiles import CLASS_TOKENS

log = logging.getLogger(__name__)

#: The talent provenance marker. ``profiles.parse_profile`` reads it back;
#: ``dataset`` publishes it on the row, emitted only when present so every tier
#: without extra builds produces the bytes it did before this existed.
ORIGIN_MARKER = "# wowdps-origin"
#: The evidence sentence beside it, published as the build's first caveat.
NOTE_MARKER = "# wowdps-origin-note"

#: The three talent provenances, in order of claim strength. Anything else in the
#: data file is a typo, and refusing it beats publishing a word nobody defined.
ORIGINS = ("repaired", "harvested", "computed")

#: The player declaration, anchored to simc's class tokens exactly as
#: ``profiles._PLAYER_LINE`` is -- a generic ``key=value`` match would take the
#: first option line of the file (``spec=``, a comment's remnant) for the player.
_PLAYER_LINE = re.compile(
    r"^(" + "|".join(CLASS_TOKENS) + r')\s*=\s*"?([^"\n]+?)"?\s*$',
    re.MULTILINE,
)
_TALENTS_LINE = re.compile(r"^talents\s*=\s*\S+\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ExtraBuild:
    """One cell: a profile this project supplies, and the evidence for it."""

    #: The simc profile name the file will declare, e.g.
    #: ``MID2_Warlock_Demonology_Diabolist``. The build id derives from it.
    profile: str
    #: Internal name of the profile whose *character* this cell wears -- a shipped
    #: sibling for a second-tree cell, the disabled generator profile for an
    #: absent spec.
    base: str
    #: The hero tree the talents play, cross-checked against the decoded hash.
    hero_tree: str
    #: ``repaired`` | ``harvested`` | ``computed``.
    origin: str
    talents: str
    #: Canonical spec id the hash must state (62 Arcane, 71 Arms, ...).
    spec_id: int
    #: One sentence of evidence, published as the build's first caveat.
    note: str
    #: Free-form provenance (report codes, kill dates, donor names). Kept in the
    #: data file for a reader; not written into the profile.
    evidence: dict | None = None
    #: Filename stem to write, when it differs from ``profile``. simc's own
    #: convention: ``MID2_Demon_Hunter_Havoc.simc`` declares itself
    #: ``MID2_Demon_Hunter_Havoc_Fel-Scarred``, and a cell superseding the
    #: refused materialised copy of that profile has to land on the same
    #: filename or both files -- one refused, one working, same build id --
    #: sit in the tier directory together.
    file: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.file or self.profile}.simc"


class ExtraBuildError(ValueError):
    """A cell that cannot be materialised, with the reason a person needs."""


def data_path() -> Path:
    return Path(str(resources.files("wowdps").joinpath("data/extra_builds.json")))


def load_cells(tier: str, path: Path | None = None) -> list[ExtraBuild]:
    """The tier's cells out of the committed data file. Empty for a tier without any."""
    source = path or data_path()
    if not source.exists():
        return []
    document = json.loads(source.read_text(encoding="utf-8"))
    cells: list[ExtraBuild] = []
    for raw in document.get("tiers", {}).get(tier, []):
        origin = raw["origin"]
        if origin not in ORIGINS:
            raise ExtraBuildError(
                f"{raw.get('profile', '?')}: origin {origin!r} is not one of {ORIGINS}"
            )
        cells.append(
            ExtraBuild(
                profile=raw["profile"],
                base=raw["base"],
                hero_tree=raw["heroTree"],
                origin=origin,
                talents=raw["talents"],
                spec_id=raw["specId"],
                note=raw["note"],
                evidence=raw.get("evidence"),
                file=raw.get("file"),
            )
        )
    return cells


@dataclass(frozen=True)
class BaseProfile:
    """The profile a cell's character comes from, and what kind of claim it is."""

    name: str
    body: str
    #: The generator file's name when the base is a disabled profile, None for a
    #: shipped one. Travels into the ``wowdps-unvalidated`` marker, so the flags a
    #: disabled character earns keep firing on the cell built from it.
    unvalidated_source: str | None


def find_base(simc_dir: Path, tier: str, name: str) -> BaseProfile:
    """The base profile's body, from simc's shipped files or its generators.

    Shipped files win over generator entries of the same name (the better claim),
    and both are read from what simc wrote -- never from a file this project
    materialised earlier, so re-running cannot compound its own output. That is
    what the marker check below is for.
    """
    tier_dir = simc_dir / "profiles" / tier
    for path in sorted(tier_dir.glob("*.simc")) if tier_dir.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("# wowdps-"):
            continue  # something this project wrote; never a base
        player = _PLAYER_LINE.search(text)
        if player and player.group(2).strip() == name:
            return BaseProfile(name=name, body=text, unvalidated_source=None)

    for disabled in unvalidated.extract_tier(simc_dir, tier):
        if disabled.name == name:
            return BaseProfile(
                name=name, body=disabled.body, unvalidated_source=disabled.source.name
            )

    raise ExtraBuildError(
        f"no shipped profile and no disabled generator entry named {name!r} in {tier}"
    )


def materialise(cell: ExtraBuild, base: BaseProfile) -> str:
    """The cell's profile file: the base's character, the cell's talents, the markers.

    Everything except the player name and the ``talents=`` line is the base's,
    byte for byte -- gear lines included, which is what makes a second-tree cell
    comparable to its sibling the way simc's own two-build specs are.
    """
    player = _PLAYER_LINE.search(base.body)
    if not player:
        raise ExtraBuildError(f"{cell.profile}: base {base.name!r} has no player declaration")
    if not _TALENTS_LINE.search(base.body):
        raise ExtraBuildError(f"{cell.profile}: base {base.name!r} has no talents= line")

    body = base.body
    body = body.replace(player.group(0), f'{player.group(1)}="{cell.profile}"', 1)
    body = _TALENTS_LINE.sub(f"talents={cell.talents}", body, count=1)

    header = ""
    if base.unvalidated_source:
        # First line, because ``parse_profile`` tests `startswith` for the flag.
        header += f"{unvalidated.MARKER} {base.unvalidated_source}\n"
    header += f"{ORIGIN_MARKER} {cell.origin}\n{NOTE_MARKER} {cell.note}\n"
    return header + body


def validate_cell(
    cell: ExtraBuild,
    traits: list[talenttree.Trait],
    sub_tree_names: dict[int, talenttree.SubTree],
    class_name: str,
) -> str | None:
    """Why this cell must not be written, or None.

    The checks are the harvest's own, minus the ones only a log can answer: the
    hash decodes against the current trait table, states the cell's spec, lands on
    the hero tree the cell claims, and passes simc's own spec rule offline. A cell
    failing any of them would either produce no row (simc refuses it -- a night's
    coverage lost silently) or, worse, a row labelled with a tree it does not play.
    """
    class_id = talenttree.CLASS_IDS.get(class_name)
    if class_id is None:
        return f"unknown class {class_name!r}"
    nodes = talenttree.nodes_for_class(traits, class_id)
    try:
        loadout = talenttree.decode_loadout(cell.talents, nodes)
    except talenttree.TalentDecodeError as error:
        return f"hash does not decode: {error}"
    if loadout.spec_id != cell.spec_id:
        return f"hash states spec {loadout.spec_id}, the cell claims {cell.spec_id}"
    violation = talenttree.spec_rule_violation(loadout, nodes)
    if violation:
        return f"simc would refuse it: {violation}"
    sub_tree = loadout.sub_tree
    named = sub_tree_names.get(sub_tree).name if sub_tree in sub_tree_names else None
    if named != cell.hero_tree:
        return f"hash plays {named!r}, the cell claims {cell.hero_tree!r}"
    return None


def class_of_base(body: str) -> str | None:
    """Display class name from a base body's player declaration, or None."""
    player = _PLAYER_LINE.search(body)
    return CLASS_TOKENS[player.group(1)][0] if player else None


@dataclass
class WriteReport:
    written: list[Path]
    skipped: list[tuple[str, str]]  # (profile, reason)
    unchecked: bool = False


def write_cells(
    simc_dir: Path,
    tier: str,
    out_dir: Path,
    cells: list[ExtraBuild],
) -> WriteReport:
    """Materialise the tier's cells into ``out_dir``, refusing what must be refused."""
    report = WriteReport(written=[], skipped=[])

    traits: list[talenttree.Trait] | None = None
    names: dict[int, talenttree.SubTree] = {}
    try:
        traits = talenttree.parse_trait_data(simc_dir, ptr=False)
        names = talenttree.parse_sub_tree_names(simc_dir, ptr=False)
    except FileNotFoundError as error:
        # A bundle without the trait table must not cost the night: simc itself
        # still refuses a rotten hash, visibly, one cell at a time.
        log.warning("no trait table under %s (%s); writing cells unchecked", simc_dir, error)
        report.unchecked = True

    out_dir.mkdir(parents=True, exist_ok=True)
    for cell in cells:
        try:
            base = find_base(simc_dir, tier, cell.base)
        except (ExtraBuildError, FileNotFoundError) as error:
            report.skipped.append((cell.profile, str(error)))
            log.error("  %s: %s", cell.profile, error)
            continue

        if traits is not None:
            class_name = class_of_base(base.body)
            reason = validate_cell(cell, traits, names, class_name or "?")
            if reason:
                report.skipped.append((cell.profile, reason))
                log.error("  %s REFUSED: %s", cell.profile, reason)
                continue

        target = out_dir / cell.filename
        if target.exists() and not target.read_text(encoding="utf-8").startswith("# wowdps-"):
            # A shipped profile of the same name is the better claim, always.
            report.skipped.append((cell.profile, "a shipped profile of this name exists"))
            log.error("  %s: refusing to overwrite a shipped profile", cell.profile)
            continue

        target.write_text(materialise(cell, base), encoding="utf-8")
        report.written.append(target)
        log.info("  wrote %s (%s, from %s)", target.name, cell.origin, cell.base)
    return report
