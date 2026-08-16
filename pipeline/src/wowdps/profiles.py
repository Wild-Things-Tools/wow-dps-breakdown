"""Discovery and identification of SimulationCraft profiles.

SimulationCraft ships a directory of tier profiles (``profiles/MID2/*.simc`` for
Midnight season 2). Their filenames encode class, spec and -- crucially for us --
the hero talent build::

    MID2_Mage_Fire_Frostfire.simc          -> Mage / Fire / Frostfire
    MID2_Death_Knight_Unholy_San'layn.simc -> Death Knight / Unholy / San'layn
    MID2_Mage_Fire.simc                    -> Mage / Fire / (simc default build)

That gives us the hero-tree axis for free, and it keeps updating itself: when the
simc devs add a spec or a new hero build for the current tier, the next pipeline
run picks it up with no code change.

Class and spec are read from the profile *body* rather than guessed from the
filename, because both contain underscores ("Death_Knight", "Beast_Mastery") and
splitting the filename alone is ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# simc's player-declaration tokens (the `mage="..."` key that opens a profile) mapped
# to display name and to the CamelCase_Underscore form used inside profile names.
# Note that simc writes `deathknight`/`demonhunter` unhyphenated while the profile
# *name* spells them `Death_Knight`/`Demon_Hunter` -- both forms are needed.
CLASS_TOKENS: dict[str, tuple[str, str]] = {
    "deathknight": ("Death Knight", "Death_Knight"),
    "demonhunter": ("Demon Hunter", "Demon_Hunter"),
    "druid": ("Druid", "Druid"),
    "evoker": ("Evoker", "Evoker"),
    "hunter": ("Hunter", "Hunter"),
    "mage": ("Mage", "Mage"),
    "monk": ("Monk", "Monk"),
    "paladin": ("Paladin", "Paladin"),
    "priest": ("Priest", "Priest"),
    "rogue": ("Rogue", "Rogue"),
    "shaman": ("Shaman", "Shaman"),
    "warlock": ("Warlock", "Warlock"),
    "warrior": ("Warrior", "Warrior"),
}

# Hero-talent names that simc profiles shorten. Anything not listed here is used
# verbatim (with underscores turned into spaces), so a newly added hero tree still
# shows up correctly without a code change -- just less prettily.
HERO_ALIASES: dict[str, str] = {
    "FS": "Flameshaper",
    "SB": "Scalecommander",
    "Rider": "Rider of the Apocalypse",
    "Conduit": "Conduit of the Celestials",
    "Herald": "Herald of the Sun",
    "Claw": "Druid of the Claw",
    "Elune": "Elune's Chosen",
    "Keeper": "Keeper of the Grove",
}

_PLAYER_LINE = re.compile(
    r"^(" + "|".join(CLASS_TOKENS) + r')\s*=\s*"?([^"\n]+)"?\s*$',
    re.MULTILINE,
)
_SPEC_LINE = re.compile(r"^spec\s*=\s*(\S+)\s*$", re.MULTILINE)
_ROLE_LINE = re.compile(r"^role\s*=\s*(\S+)\s*$", re.MULTILINE)
_TALENTS_LINE = re.compile(r"^talents\s*=\s*(\S+)\s*$", re.MULTILINE)


def _titleize(token: str) -> str:
    """``beast_mastery`` -> ``Beast_Mastery`` (the form used in filenames)."""
    return "_".join(part[:1].upper() + part[1:] for part in token.split("_"))


def _prettify(raw: str) -> str:
    """``Aldrachi_Reaver`` -> ``Aldrachi Reaver``; expand known abbreviations."""
    if raw in HERO_ALIASES:
        return HERO_ALIASES[raw]
    return raw.replace("_", " ").replace("-", "-").strip()


# Expansion prefixes of simc's tier directories, for display. A prefix we do not
# know is shown verbatim rather than guessed at, so a new expansion needs no code
# change to appear -- only to appear prettily.
TIER_EXPANSIONS: dict[str, str] = {
    "MID": "Midnight",
    "TWW": "The War Within",
    "DF": "Dragonflight",
    "SL": "Shadowlands",
    "BFA": "Battle for Azeroth",
}


def tier_label(tier: str) -> str:
    """``MID2`` -> ``Midnight Season 2``; unknown prefixes are returned as-is."""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", tier)
    if not match:
        return tier
    expansion = TIER_EXPANSIONS.get(match.group(1).upper())
    return f"{expansion} Season {match.group(2)}" if expansion else tier


def slugify(*parts: str) -> str:
    joined = "_".join(p for p in parts if p)
    slug = re.sub(r"[^a-z0-9]+", "_", joined.lower())
    return slug.strip("_")


@dataclass(frozen=True)
class SpecProfile:
    """One simc profile, identified."""

    path: Path
    tier: str
    wow_class: str  # "Death Knight"
    spec: str  # "Unholy"
    hero_talent: str | None  # "San'layn" -- the tree the build plays, always known
    role: str  # "spell" | "attack" | "tank" | "hybrid" | ...
    talent_hash: str | None
    #: The hero-tree slug simc's *profile name* carried, before resolution. ``None``
    #: for a build simc ships unnamed. This -- not ``hero_talent`` -- is what the id
    #: is built from, so resolving an unnamed build's real tree (see ``herotrees``)
    #: fixes what is shown without renaming the file every other dataset joins on.
    #: The id then names simc's build *slot* ("default") while ``hero_talent`` names
    #: the tree it plays ("Deathbringer"); both are true and neither moves the joins.
    name_hero: str | None = None

    @property
    def id(self) -> str:
        return slugify(self.wow_class, self.spec, self.name_hero or "default")

    @property
    def spec_id(self) -> str:
        """Identifier of the spec ignoring the hero-talent build."""
        return slugify(self.wow_class, self.spec)

    @property
    def hero_label(self) -> str:
        # Every spec plays a tree; the resolved one, or the profile-name one, and
        # only ``Default`` if a tier has not been through ``wowdps hero-trees`` yet.
        return self.hero_talent or "Default"

    @property
    def display_name(self) -> str:
        base = f"{self.spec} {self.wow_class}"
        return f"{base} ({self.hero_label})"

    @property
    def is_dps(self) -> bool:
        return self.role in {"spell", "attack", "hybrid", "dps"}


def parse_profile(
    path: Path, tier: str, hero_overrides: dict[str, str] | None = None
) -> SpecProfile | None:
    """Identify a single ``.simc`` profile. Returns None if it is not a player profile.

    ``hero_overrides`` maps a profile's internal name to the hero tree it actually
    plays, for the builds simc ships without a hero suffix. See ``herotrees``.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

    player = _PLAYER_LINE.search(text)
    spec_match = _SPEC_LINE.search(text)
    if not player or not spec_match:
        return None

    class_token = player.group(1)
    wow_class, class_filename_form = CLASS_TOKENS[class_token]
    spec_token = spec_match.group(1)
    spec = _prettify(_titleize(spec_token))

    role_match = _ROLE_LINE.search(text)
    role = role_match.group(1) if role_match else "attack"

    talents = _TALENTS_LINE.search(text)

    # Prefer the profile name declared inside the file over the filename. simc keeps
    # the internal name accurate when the recommended build changes but does not
    # always rename the file: MID1_Death_Knight_Unholy.simc declares itself as
    # "MID1_Death_Knight_Unholy_Rider", and Rider is genuinely the build being simmed.
    profile_name = player.group(2).strip()
    name_hero = _hero_from_name(profile_name, tier, class_filename_form, spec_token)
    if name_hero is None:
        name_hero = _hero_from_name(path.stem, tier, class_filename_form, spec_token)
    # simc ships a spec's default build with no hero suffix, but it still plays a
    # tree. The resolved name is detected from simc (`wowdps hero-trees`) and keyed
    # on the profile's internal name. It feeds the *display*, never the id.
    hero_talent = name_hero
    if hero_talent is None and hero_overrides:
        hero_talent = hero_overrides.get(profile_name) or hero_overrides.get(path.stem)

    return SpecProfile(
        path=path,
        tier=tier,
        wow_class=wow_class,
        spec=spec,
        hero_talent=hero_talent,
        role=role,
        talent_hash=talents.group(1) if talents else None,
        name_hero=name_hero,
    )


def _hero_from_name(name: str, tier: str, class_filename_form: str, spec_token: str) -> str | None:
    """Strip ``<tier>_<Class>_<Spec>`` off a profile name; the remainder is the hero build."""
    rest = name
    for prefix in (f"{tier}_", f"{class_filename_form}_", _titleize(spec_token)):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :]
        else:
            # Name does not follow the convention (e.g. a hand-written profile).
            # Report "no hero build identified" rather than inventing one.
            return None
    rest = rest.lstrip("_")
    return _prettify(rest) if rest else None


def spec_coverage(profiles_dir: Path, tier: str) -> dict:
    """Which damage specs this tier ships a profile for, and which it does not yet.

    The question a reader asks the week a season opens: *is my spec missing, or is it
    just bad?* Those look identical on a ranking that only draws what it has.

    The reference list -- what "all specs" means -- is **derived from the other tiers
    simc ships**, not written down here. A hard-coded table of the game's damage specs
    would need editing whenever Blizzard adds one (Midnight adds Devourer) and would
    silently go stale in exactly the patch where this matters most. What simc shipped
    for a previous tier and has not yet shipped for this one is the honest reading of
    "not there yet", and it needs no maintenance.

    Consequence worth knowing: a spec that has *never* had a profile in any shipped
    tier cannot appear as missing, because nothing here knows it exists. That is the
    right failure -- it under-claims rather than inventing a spec list.
    """
    covered: set[tuple[str, str]] = set()
    known: set[tuple[str, str]] = set()
    for candidate in available_tiers(profiles_dir):
        specs = {(p.wow_class, p.spec) for p in discover(profiles_dir, candidate, dps_only=True)}
        known |= specs
        if candidate == tier:
            covered = specs

    missing = sorted(known - covered)
    return {
        "damageSpecs": len(covered),
        "damageSpecsKnown": len(known),
        "missing": [{"class": wow_class, "spec": spec} for wow_class, spec in missing],
        "comparedWith": sorted(t for t in available_tiers(profiles_dir) if t != tier),
    }


def discover(profiles_dir: Path, tier: str, dps_only: bool = True) -> list[SpecProfile]:
    """Find and identify every profile of a tier, sorted by class then spec then build."""
    tier_dir = profiles_dir / tier
    if not tier_dir.is_dir():
        raise FileNotFoundError(
            f"profile directory {tier_dir} not found -- is the simc checkout complete "
            f"and is tier {tier!r} correct?"
        )

    from . import herotrees

    hero_overrides = herotrees.load_overrides(tier)
    found: list[SpecProfile] = []
    for path in sorted(tier_dir.glob("*.simc")):
        profile = parse_profile(path, tier, hero_overrides)
        if profile is None:
            continue
        if dps_only and not profile.is_dps:
            continue
        found.append(profile)

    found.sort(key=lambda p: (p.wow_class, p.spec, p.hero_talent or ""))
    return found


def available_tiers(profiles_dir: Path) -> list[str]:
    """Every non-empty tier directory, oldest first.

    Tier directories are named like ``MID1``, ``MID2``. Directories that do not follow
    that shape (``PreRaids``, ``generators``) are not tiers and are skipped, so the
    ordering is well defined.
    """
    candidates: list[tuple[str, int, str]] = []
    for entry in profiles_dir.iterdir():
        if not entry.is_dir():
            continue
        match = re.fullmatch(r"([A-Za-z]+)(\d+)", entry.name)
        if not match:
            continue
        if not any(entry.glob("*.simc")):
            continue
        candidates.append((match.group(1), int(match.group(2)), entry.name))

    candidates.sort(key=lambda c: (c[0], c[1]))
    return [c[2] for c in candidates]


def latest_tier(profiles_dir: Path) -> str:
    """Newest tier that actually contains profiles, so the site follows the raid tier."""
    tiers = available_tiers(profiles_dir)
    if not tiers:
        raise FileNotFoundError(f"no tier profile directories found under {profiles_dir}")
    return tiers[-1]


def previous_tier(profiles_dir: Path) -> str:
    """The tier before the current one.

    Named rather than hard-coded so that scheduled runs asking for "last season" keep
    meaning last season after the next tier ships.
    """
    tiers = available_tiers(profiles_dir)
    if len(tiers) < 2:
        raise FileNotFoundError(
            f"only {len(tiers)} tier(s) under {profiles_dir}; there is no previous one"
        )
    return tiers[-2]


#: What "an old tier's profiles rot" looks like on the wire.
#:
#: It is an *initialization* error rather than a parse error -- the profile reads
#: fine and then produces no actor -- and it comes in at least two shapes, both of
#: which are the same thing: the stored talent hash no longer fits the tree.
#: Measured on MID1 against simc 1210-01, 15 profiles fail and they split 9/6:
#:
#:     Selected node N entry M is not available to player's spec
#:     Node N is not a choice node but has index selection
#:
#: So the test is not either message but the pair "initialization error" and a
#: quoted hash, which covers both and whatever simc words it as next season.
_INIT_ERROR = "Initialization error"
_HASH_QUOTED = "Hash '"


@dataclass(frozen=True)
class ProfileHealth:
    """Whether one profile still produces an actor against current spell data."""

    profile: SpecProfile
    loads: bool
    #: simc's own first error line, trimmed. None when the profile loaded.
    reason: str | None = None
    #: True when the reason is specifically a talent hash the spec no longer offers,
    #: which is the failure an ageing tier produces and the one worth counting.
    rotten_talents: bool = False


def check_loads(simc: Path, profile: SpecProfile, timeout: int = 120) -> ProfileHealth:
    """Run one profile for a single iteration and report whether it produced an actor.

    One iteration is enough: what is being tested is whether simc can build the
    player at all, which happens before any combat is simulated.

    Two simc traps are baked in here rather than left to each caller:

    * ``html=`` with an empty value suppresses the HTML report, but ``json2=`` with
      an empty value is a **setup failure** -- "Missing JSON report output file
      name". A check that passes both reports every profile in the game as broken,
      which is exactly what the first version of this did.
    * The interesting failure is an *initialization* error, not a non-zero exit on
      its own, so the output is read rather than just the return code.
    """
    import subprocess

    completed = subprocess.run(
        [str(simc), str(profile.path), "iterations=1", "threads=1", "html="],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    for marker in ("Error:", "Setup failure", "Unable to generate"):
        if marker not in output:
            continue
        line = next((row for row in output.splitlines() if marker in row), "").strip()
        rotten = _INIT_ERROR in line and _HASH_QUOTED in line
        return ProfileHealth(profile, loads=False, reason=line[:200], rotten_talents=rotten)
    return ProfileHealth(profile, loads=True)
