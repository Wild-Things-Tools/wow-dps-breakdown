"""Profiles SimulationCraft has written and not switched on.

MID2 ships 15 of the game's 26 damage specs, and the reason is not what it looks
like. It is not that the missing specs' action lists are stale -- those live in
simc's class modules and exist for every spec. It is that
``profiles/generators/<tier>/<Class>.simc`` contains a **complete profile** for each
of them -- talent hash, every gear slot with gems and enchants, the ``save=`` line
naming the file it would produce -- with every line commented out. Measured on
2026-08-17: Warrior 0 active and 3 commented, Monk 0 and 3, Druid 0 and 2, Evoker
0 and 2, Demon Hunter 1 and 5.

simc's authors disable a generator entry while a profile or a rotation is not
validated for the tier. So the data exists and carries a warning, which is a
different thing from being absent, and this module makes the distinction usable
rather than resolving it: it writes the commented profiles out so they can be
simulated, and every result from them has to be labelled as coming from a profile
simc did not publish.

**This is not the same claim as a shipped profile.** A shipped one is simc's
authors saying "this is what the spec looks like this season". One of these is
saying "here is the character we had written down when we stopped". Both are
useful; presenting them as the same number would not be.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: A commented profile opens with the player declaration and closes with the
#: ``save=`` that names its file. Both forms are matched behind the comment mark.
#:
#: **The quotes are optional, and requiring them published a false claim.** simc's
#: generators write both forms -- ``# paladin="MID2_Paladin_Retribution_Herald"`` in
#: most files and ``# paladin=MID2_Paladin_Retribution_Herald`` in
#: ``MID2_Generate_Paladin.simc`` -- and a pattern that insisted on quotes found 14
#: disabled blocks where 17 exist (measured 2026-08-22, simc 22b442e). The three it
#: missed were both Retribution Paladin builds and Guardian Druid, so the coverage
#: panel reported *"simc has no profile for Retribution at all"* while simc shipped
#: two complete ones in the generator -- exactly the wrong answer that panel exists
#: to prevent. Do not "simplify" the quotes back to mandatory.
_OPEN = re.compile(r"^#\s*([a-z_]+)\s*=\s*\"?([^\"\s][^\"]*?)\"?\s*$")
_SAVE = re.compile(r"^#\s*save\s*=\s*(\S+\.simc)\s*$")
_COMMENT = re.compile(r"^#\s?")

#: Written into every profile this module emits, and read back by
#: ``profiles.parse_profile``. The state travels **in the file** rather than
#: through a flag on the command line: a sharded run materialises these
#: separately in every job, and a side channel would have to be threaded through
#: all of them and could disagree between two of them. A profile that says what
#: it is cannot.
MARKER = "# wowdps-unvalidated"


@dataclass(frozen=True)
class DisabledProfile:
    """One profile simc wrote, commented out, and did not ship."""

    name: str
    filename: str
    body: str
    source: Path

    @property
    def spec_line(self) -> str | None:
        for line in self.body.splitlines():
            if line.startswith("spec="):
                return line.split("=", 1)[1].strip()
        return None


def extract(path: Path) -> list[DisabledProfile]:
    """Every commented-out profile in one generator file.

    A block runs from the player declaration to its ``save=`` line. Anything
    between them that is not a comment ends the block without emitting it: a
    generator that interleaves live and commented entries must not have the two
    spliced together, which would produce a profile nobody wrote.
    """
    found: list[DisabledProfile] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    index = 0
    while index < len(lines):
        opened = _OPEN.match(lines[index])
        if not opened:
            index += 1
            continue

        body: list[str] = [f'{opened.group(1)}="{opened.group(2)}"']
        cursor = index + 1
        filename = None
        while cursor < len(lines):
            line = lines[cursor]
            if not line.strip():
                # Generators separate the header, the gear and the save line with
                # blank lines. Treating those as the end of the block found nothing
                # at all -- the first version of this returned zero profiles from a
                # file with three in it.
                cursor += 1
                continue
            if not line.startswith("#"):
                # A live line inside a commented block: the block is malformed or
                # the file mixes the two. Abandon it rather than splice.
                break
            save = _SAVE.match(line)
            if save:
                filename = save.group(1)
                break
            stripped = _COMMENT.sub("", line)
            if stripped.strip():
                body.append(stripped)
            cursor += 1

        if filename:
            found.append(
                DisabledProfile(
                    name=opened.group(2),
                    filename=filename,
                    body="\n".join(body) + "\n",
                    source=path,
                )
            )
            index = cursor + 1
        else:
            index += 1
    return found


def extract_tier(simc_dir: Path, tier: str) -> list[DisabledProfile]:
    """Every disabled profile across a tier's generator files."""
    generators = simc_dir / "profiles" / "generators" / tier
    if not generators.is_dir():
        raise FileNotFoundError(f"no generator directory at {generators}")
    found: list[DisabledProfile] = []
    for path in sorted(generators.glob("*.simc")):
        found.extend(extract(path))
    return found


def write_profiles(
    profiles: list[DisabledProfile], out_dir: Path, shipped: set[str] | None = None
) -> list[Path]:
    """Write the disabled profiles as real ones, skipping any simc already ships.

    Never overwrites a shipped profile: where simc publishes one, that is the
    better claim and this module has nothing to add. The skip is by filename,
    which is what both sides key on.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    already = shipped or set()
    written: list[Path] = []
    for profile in profiles:
        if profile.filename in already:
            log.info("  %s is shipped by simc; leaving it alone", profile.filename)
            continue
        path = out_dir / profile.filename
        path.write_text(marked(profile), encoding="utf-8")
        written.append(path)
    return written


def marked(profile: DisabledProfile) -> str:
    """The profile's body with the provenance header that labels it."""
    return f"{MARKER} {profile.source.name}\n{profile.body}"
