"""Which hero tree a build actually plays, for the ones simc does not name.

Every specialisation in the game plays a hero-talent tree. SimulationCraft names
it in the profile for most builds (``MID2_Death_Knight_Frost_Rider``), but for the
build it treats as a spec's default it ships the profile with no suffix
(``MID2_Death_Knight_Frost``) -- and that used to surface on the site as a build
with *no* hero tree, which cannot exist. It has one; the name just does not say so.

The tree is encoded in the profile's talent hash. Decoding a WoW talent-loadout
string into node selections is a substantial piece of work that needs the tree
definition data, so this takes the shorter route SimulationCraft already gives us:
run the profile and read which hero-tree-gated abilities were active. The action
list branches on ``hero_tree.<slug>``, so only the tree the build actually took
produces damage and buffs, and the signature is unambiguous.

The result is written per tier to ``data/hero_trees.json`` by ``wowdps hero-trees``
and read back by ``profiles.discover``. It is data, generated from simc and
checked in, so a tier that has not been processed falls back to "no tree named"
rather than the site inventing one.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Per (class, spec), the abilities that are unique to each of the spec's two hero
#: trees -- a buff, proc, or damaging action that only exists when that tree is
#: taken. Only specs that ship an unnamed ("default") build need an entry; the
#: detector returns None for anything not listed, and the caller keeps the profile
#: as unnamed rather than guessing. Extend this when a new tier ships a default
#: build for a spec not here; `wowdps hero-trees -v` names the spec it could not
#: resolve.
HERO_TREE_SIGNATURES: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    ("Death Knight", "Frost"): {
        "Deathbringer": ("exterminate", "reapers_mark"),
        "Rider of the Apocalypse": ("apocalypse_now", "riders_champion", "mograine", "whitemane"),
    },
    ("Death Knight", "Unholy"): {
        "San'layn": ("vampiric_strike", "essence_of_the_blood_queen", "gift_of_the_sanlayn"),
        "Rider of the Apocalypse": ("apocalypse_now", "riders_champion", "mograine", "whitemane"),
    },
    ("Hunter", "Beast Mastery"): {
        "Pack Leader": ("heart_of_the_pack", "howl_of_the_pack", "hogstrider", "vicious_hunt"),
        "Dark Ranger": ("black_arrow", "withering_fire", "bleak_arrows", "shadow_hounds"),
    },
    ("Hunter", "Marksmanship"): {
        "Sentinel": ("sentinels_mark", "symphonic_arsenal", "lunar_storm"),
        "Dark Ranger": ("black_arrow", "withering_fire", "bleak_arrows"),
    },
    ("Hunter", "Survival"): {
        "Sentinel": ("sentinels_mark", "symphonic_arsenal", "lunar_storm"),
        "Pack Leader": ("heart_of_the_pack", "howl_of_the_pack", "hogstrider", "vicious_hunt"),
    },
    ("Rogue", "Subtlety"): {
        "Deathstalker": ("deathstalkers_mark", "fatal_intent", "darkest_night"),
        "Trickster": ("unseen_blade", "coup_de_grace", "flawless_form"),
    },
    ("Rogue", "Assassination"): {
        "Deathstalker": ("deathstalkers_mark", "fatal_intent", "darkest_night"),
        "Fatebound": ("fatebound_coin", "double_or_nothing", "inevitability"),
    },
    ("Rogue", "Outlaw"): {
        "Trickster": ("unseen_blade", "coup_de_grace", "flawless_form"),
        "Fatebound": ("fatebound_coin", "double_or_nothing", "inevitability"),
    },
}


def active_ability_slugs(report: dict) -> set[str]:
    """The slugs of everything the actor actually used or gained, from a json2 report.

    Buffs, procs and gains are taken as-is; stats are taken only when they did
    damage, so an ability that is in the action list but never fired (the other
    tree's) does not count as a signature.
    """
    player = ((report.get("sim") or {}).get("players") or [{}])[0]
    names: set[str] = set()
    for section in ("buffs", "procs", "gains"):
        for entry in player.get(section) or []:
            name = entry.get("name")
            if isinstance(name, str):
                names.add(name.lower())
    for entry in player.get("stats") or []:
        name = entry.get("name")
        did_damage = (entry.get("actual_amount") or {}).get("mean") or (
            entry.get("total_amount") or {}
        ).get("mean")
        if isinstance(name, str) and did_damage:
            names.add(name.lower())
    return names


def detect_hero_tree(report: dict, wow_class: str, spec: str) -> str | None:
    """Which hero tree this build took, or None when it cannot be told apart.

    None rather than a guess in three cases: the spec has no signature table, the
    signatures matched more than one tree (a table that has gone stale), or they
    matched none. The caller keeps the build unnamed and the run reports it, which
    is the honest outcome -- an invented tree would be worse than a blank.
    """
    signatures = HERO_TREE_SIGNATURES.get((wow_class, spec))
    if not signatures:
        return None
    active = active_ability_slugs(report)
    matched = [tree for tree, slugs in signatures.items() if any(s in active for s in slugs)]
    return matched[0] if len(matched) == 1 else None


def _data_file() -> Path:
    from importlib import resources

    return Path(str(resources.files("wowdps.data") / "hero_trees.json"))


def load_overrides(tier: str, path: Path | None = None) -> dict[str, str]:
    """The resolved ``{profile internal name: hero tree}`` map for one tier.

    Empty when nothing has been generated: `profiles.discover` then leaves an
    unnamed build unnamed, exactly as before this existed.
    """
    source = path or _data_file()
    if not source.is_file():
        return {}
    raw = json.loads(source.read_text(encoding="utf-8"))
    entry = (raw.get("tiers") or {}).get(tier) or {}
    return {str(k): str(v) for k, v in (entry.get("resolved") or {}).items()}


def write_overrides(tier: str, resolved: dict[str, str], path: Path | None = None) -> Path:
    """Merge one tier's resolved map into the checked-in data file."""
    target = path or _data_file()
    raw: dict = {}
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8"))
    raw.setdefault(
        "note", "Hero trees for builds simc ships unnamed, detected by wowdps hero-trees."
    )
    tiers = raw.setdefault("tiers", {})
    tiers[tier] = {"resolved": dict(sorted(resolved.items()))}
    target.write_text(json.dumps(raw, indent=1) + "\n", encoding="utf-8")
    return target
