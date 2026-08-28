"""``wowdps loot-sources``: derive where each item drops, and what this season runs.

Two facts the loot sweep depends on were asserted by hand, and both rot every season:

1. **Which pool an item belongs to.** Inferred from item id blocks and base item
   level, because simc ships no item source of any kind. The inference is wrong in
   both directions -- see ``equipment.py`` -- and nobody can check it without an
   outside source.
2. **Which dungeons the Mythic+ season runs.** Typed in from a news article.

Blizzard's Game Data API carries both. ``journal-encounter/{id}`` lists the items an
encounter drops, ``journal-expansion/{id}`` splits every instance into ``raids`` and
``dungeons``, and the current season's dungeon rotation falls out of the Mythic
Keystone leaderboard index. So this module derives what was previously stated.

Derivation does not overwrite assertion
---------------------------------------
The project's provenance discipline applies here exactly as it does to fight
profiles: a derived fact is marked derived, an asserted fact stays asserted, and a
**disagreement is a finding, not a merge conflict to resolve silently**. So each item
gains a ``derived`` block beside its hand-written ``source``, the two are compared,
and the differences are printed and written out. Nothing edits ``source``. If the API
says a trinket the structural inference called Mythic+ is a raid drop, a human should
read that sentence before the pool changes shape underneath the sweep.

The same holds for the rotation: ``dungeonRotationDerived`` sits beside whatever
``dungeonRotation`` a human typed, and the two lists are diffed rather than merged.

Where each fact comes from
--------------------------
======================================  ===============================================
Fact                                    Endpoint
======================================  ===============================================
instance is a raid or a dungeon         ``journal-expansion/{id}`` -> ``raids``/``dungeons``
which encounter drops an item           ``journal-encounter/{id}`` -> ``items[].item.id``
which expansion an instance belongs to  ``journal-expansion/{id}`` -> ``name``
which season is current                 ``mythic-keystone/season/index`` -> ``current_season``
which dungeons the season runs          ``connected-realm/{id}/mythic-leaderboard/index``
keystone dungeon -> journal instance    ``mythic-keystone/dungeon/{id}`` -> ``dungeon.key``
======================================  ===============================================

Two of those need a word.

**The season endpoint does not list dungeons.** ``mythic-keystone/season/{id}``
returns periods and a name, nothing about which dungeons are in it. The rotation
comes from ``current_leaderboards`` instead: a Mythic Keystone leaderboard exists for
exactly the dungeons the current season runs. That is an interpretation of what the
endpoint means rather than a field labelled "rotation", and it is stated in the
output as one. It is also per connected realm while the rotation is region-wide, so
any realm answers for all of them -- the lowest id is used, for reproducibility.

**Keystone dungeon ids are not journal instance ids.** They are challenge-mode ids
and share nothing. ``mythic-keystone/dungeon/{id}`` carries a ``dungeon`` link that
points at ``/data/wow/journal-instance/{id}``, which is the join. Reading the href
rather than the sibling ``id`` is deliberate: only the href says which kind of id it
is.

Season-proofing
---------------
Nothing here pins a season, an expansion or an instance. The current season comes
from ``current_season``; the rotation from the leaderboards that exist right now; the
raid/dungeon split from whichever expansion owns the instance. The default walk
covers every encounter in the game, so a new raid in a new expansion needs no
configuration -- ``--expansion`` exists to make a re-run cheap, not to make it
correct.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import equipment, profiles
from .blizzard import (
    BlizzardClient,
    BlizzardError,
    Credentials,
    RequestBudgetExhausted,
    id_from_href,
)

log = logging.getLogger(__name__)

#: The journal's own categories, mapped to the vocabulary the pool file already uses.
#: ``WORLD_BOSS`` and ``EVENT`` map to nothing on purpose: an item from either is not
#: a raid drop and not a dungeon drop, and inventing a third pool to hold it would be
#: answering a question nobody asked.
KIND_TO_POOL: dict[str, str] = {"raid": "raid", "dungeon": "mythicplus"}


@dataclass(frozen=True)
class InstancePlacement:
    """One instance, and the expansion that owns it."""

    instance_id: int
    instance: str
    expansion: str
    #: "raid" or "dungeon", from which array of the expansion it appeared in.
    kind: str


@dataclass(frozen=True)
class ItemDrop:
    """One item, dropped by one encounter. An item may have several of these."""

    item_id: int
    item_name: str
    encounter_id: int
    encounter: str
    instance_id: int | None
    instance: str
    expansion: str | None
    kind: str | None

    @property
    def pool(self) -> str | None:
        return KIND_TO_POOL.get(self.kind or "")

    def summary(self) -> str:
        where = f"{self.encounter}, {self.instance}" if self.instance else self.encounter
        return f"{where} ({self.expansion or 'expansion unknown'})"


@dataclass
class LootIndex:
    """Item id -> the encounters that drop it, plus how much of the game was read."""

    drops: dict[int, list[ItemDrop]] = field(default_factory=dict)
    placements: dict[int, InstancePlacement] = field(default_factory=dict)
    encounters_read: int = 0
    encounters_offered: int = 0
    scope: str = ""
    #: Set when a request ceiling ended the walk early. A partial index is still
    #: worth having, but "no encounter drops this" and "the walk stopped before it
    #: got there" are different sentences and must not print the same.
    truncated: bool = False

    def add(self, drop: ItemDrop) -> None:
        self.drops.setdefault(drop.item_id, []).append(drop)

    def pools_for(self, item_id: int) -> set[str]:
        return {drop.pool for drop in self.drops.get(item_id, ()) if drop.pool}


@dataclass(frozen=True)
class Season:
    season_id: int
    name: str | None
    start_timestamp: int | None
    end_timestamp: int | None
    is_current: bool

    def to_json(self) -> dict:
        return {
            "id": self.season_id,
            # Frequently null in the API. Left null rather than filled in with the
            # tier's label, which would be the tier's label wearing a season's badge.
            "name": self.name,
            "startTimestamp": self.start_timestamp,
            "endTimestamp": self.end_timestamp,
            "isCurrent": self.is_current,
        }


@dataclass(frozen=True)
class RotationDungeon:
    keystone_id: int
    name: str
    instance_id: int | None
    instance: str | None


@dataclass
class Rotation:
    """This season's dungeons, and how confident the derivation is about them."""

    season: Season | None = None
    connected_realm_id: int | None = None
    dungeons: tuple[RotationDungeon, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def instance_ids(self) -> frozenset[int]:
        return frozenset(d.instance_id for d in self.dungeons if d.instance_id is not None)

    @property
    def names(self) -> tuple[str, ...]:
        """Journal instance names where known, keystone names otherwise.

        The instance name is preferred because that is what an item's drop source
        will be called; the keystone name is usually identical and occasionally is
        not.
        """
        return tuple(d.instance or d.name for d in self.dungeons)

    def to_json(self) -> dict:
        return {
            "season": self.season.to_json() if self.season else None,
            "connectedRealmId": self.connected_realm_id,
            "dungeons": [
                {
                    "keystoneId": d.keystone_id,
                    "name": d.name,
                    "instanceId": d.instance_id,
                    "instance": d.instance,
                }
                for d in self.dungeons
            ],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------------
# Extraction: pure functions over API payloads
# --------------------------------------------------------------------------------


def placements_from_expansion(payload: dict) -> list[InstancePlacement]:
    """Every instance of one expansion, already split into raids and dungeons.

    This is the cheap half of the raid/dungeon question: one request classifies a
    whole expansion's instances, where asking each instance would be dozens. The
    encounter payload carries its own ``category.type`` as a cross-check, but the
    expansion's arrays are the authority here -- being *in* the raids list is a
    stronger statement than a category label that also has to describe world bosses.
    """
    expansion = str(payload.get("name") or "").strip()
    found: list[InstancePlacement] = []
    for key, kind in (("raids", "raid"), ("dungeons", "dungeon")):
        for entry in payload.get(key) or []:
            if not isinstance(entry, dict):
                continue
            instance_id = entry.get("id") or id_from_href(
                (entry.get("key") or {}).get("href"), "journal-instance"
            )
            if not isinstance(instance_id, int):
                continue
            found.append(
                InstancePlacement(
                    instance_id=instance_id,
                    instance=str(entry.get("name") or instance_id),
                    expansion=expansion,
                    kind=kind,
                )
            )
    return found


def drops_from_encounter(payload: dict, placements: dict[int, InstancePlacement]) -> list[ItemDrop]:
    """The items one encounter drops, with the instance they come from attached.

    ``items[].id`` is the *journal* entry's id and ``items[].item.id`` is the real
    item id; reading the wrong one silently produces a table that joins to nothing.
    """
    encounter_id = payload.get("id")
    if not isinstance(encounter_id, int):
        return []
    encounter = str(payload.get("name") or encounter_id)

    instance_node = payload.get("instance") or {}
    instance_id = instance_node.get("id") or id_from_href(
        (instance_node.get("key") or {}).get("href"), "journal-instance"
    )
    placement = placements.get(instance_id) if isinstance(instance_id, int) else None
    instance = str(instance_node.get("name") or (placement.instance if placement else "")) or ""

    kind = placement.kind if placement else _kind_from_category(payload)
    expansion = placement.expansion if placement else None

    found: list[ItemDrop] = []
    for entry in payload.get("items") or []:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item") or {}
        item_id = item.get("id") or id_from_href((item.get("key") or {}).get("href"), "item")
        if not isinstance(item_id, int):
            continue
        found.append(
            ItemDrop(
                item_id=item_id,
                item_name=str(item.get("name") or ""),
                encounter_id=encounter_id,
                encounter=encounter,
                instance_id=instance_id if isinstance(instance_id, int) else None,
                instance=instance,
                expansion=expansion,
                kind=kind,
            )
        )
    return found


def _kind_from_category(payload: dict) -> str | None:
    """Fallback when no expansion claimed the instance: the encounter's own category."""
    raw = ((payload.get("category") or {}).get("type") or "").strip().lower()
    return raw or None


def rotation_from_leaderboards(
    entries: list[dict],
    dungeon_payloads: dict[int, dict | None],
) -> tuple[tuple[RotationDungeon, ...], tuple[str, ...]]:
    """``current_leaderboards`` plus one keystone-dungeon fetch each -> the rotation.

    A dungeon whose journal instance cannot be resolved is *kept*, with a warning:
    it is still in the rotation, and dropping it would quietly shorten a list whose
    whole purpose is to be complete.
    """
    dungeons: list[RotationDungeon] = []
    warnings: list[str] = []
    for entry in entries:
        keystone_id = entry.get("id")
        if not isinstance(keystone_id, int):
            continue
        name = str(entry.get("name") or keystone_id)
        payload = dungeon_payloads.get(keystone_id) or {}
        node = payload.get("dungeon") or {}
        instance_id = id_from_href((node.get("key") or {}).get("href"), "journal-instance")
        if instance_id is None and isinstance(node.get("id"), int):
            instance_id = node["id"]
        if instance_id is None:
            warnings.append(
                f"{name}: no journal instance behind keystone dungeon {keystone_id}, so its "
                f"items cannot be recognised as this season's"
            )
        dungeons.append(
            RotationDungeon(
                keystone_id=keystone_id,
                name=name,
                instance_id=instance_id,
                instance=str(node.get("name")) if node.get("name") else None,
            )
        )
    return tuple(dungeons), tuple(warnings)


def season_from_index(index: dict, detail: dict | None) -> Season | None:
    current = index.get("current_season") or {}
    season_id = current.get("id") or id_from_href((current.get("key") or {}).get("href"), "season")
    if not isinstance(season_id, int):
        return None
    detail = detail or {}
    return Season(
        season_id=season_id,
        name=detail.get("season_name") or None,
        start_timestamp=detail.get("start_timestamp"),
        end_timestamp=detail.get("end_timestamp"),
        is_current=True,
    )


# --------------------------------------------------------------------------------
# Walking the API
# --------------------------------------------------------------------------------


def derive_rotation(client: BlizzardClient) -> Rotation:
    """This season's dungeons, resolved from the API with no id pinned anywhere."""
    index = client.mythic_keystone_season_index()
    season_id = (index.get("current_season") or {}).get("id")
    detail = client.mythic_keystone_season(season_id) if isinstance(season_id, int) else None
    season = season_from_index(index, detail)

    realms = client.connected_realm_index()
    if not realms:
        return Rotation(
            season=season,
            warnings=("no connected realm was returned, so the rotation could not be read",),
        )
    # Any realm answers: the rotation is region-wide. The lowest id keeps the cache
    # key and therefore the whole pass reproducible.
    realm_id = realms[0]

    entries = client.mythic_leaderboard_index(realm_id)
    payloads = {
        entry["id"]: client.mythic_keystone_dungeon(entry["id"])
        for entry in entries
        if isinstance(entry.get("id"), int)
    }
    dungeons, warnings = rotation_from_leaderboards(entries, payloads)
    if not dungeons:
        warnings = (
            *warnings,
            f"connected realm {realm_id} listed no current leaderboards; between seasons "
            f"this is what an unstarted season looks like",
        )
    return Rotation(
        season=season,
        connected_realm_id=realm_id,
        dungeons=dungeons,
        warnings=warnings,
    )


def derive_index(
    client: BlizzardClient,
    expansions: tuple[str, ...] = (),
    extra_instances: frozenset[int] = frozenset(),
) -> LootIndex:
    """Walk instances and encounters, and record what drops where.

    With no ``expansions`` filter this reads every encounter the journal knows --
    about fifteen hundred requests, four percent of the hourly budget, and no
    assumption anywhere about which expansion or raid is current. That is the
    season-proof default.

    With a filter it reads only the named expansions' instances plus
    ``extra_instances`` (the rotation's, so legacy dungeons in the rotation stay in
    scope), which is a couple of hundred requests. Cheaper and narrower: an item
    outside the scope comes back unresolved rather than misplaced.
    """
    index = LootIndex()
    expansion_payloads = []
    for entry in client.journal_expansion_index():
        expansion_id = entry.get("id") or id_from_href(
            (entry.get("key") or {}).get("href"), "journal-expansion"
        )
        if not isinstance(expansion_id, int):
            continue
        payload = client.journal_expansion(expansion_id)
        if payload:
            expansion_payloads.append(payload)

    for payload in expansion_payloads:
        for placement in placements_from_expansion(payload):
            index.placements[placement.instance_id] = placement

    try:
        encounter_ids = _encounters_in_scope(client, index, expansions, extra_instances)
        index.encounters_offered = len(encounter_ids)
        index.scope = (
            "every encounter in the journal"
            if not expansions
            else "expansions " + ", ".join(expansions) + " plus this season's rotation"
        )

        for encounter_id in encounter_ids:
            payload = client.journal_encounter(encounter_id)
            if not payload:
                continue
            index.encounters_read += 1
            for drop in drops_from_encounter(payload, index.placements):
                index.add(drop)
    except RequestBudgetExhausted as exc:
        log.warning("%s", exc)
        index.truncated = True

    return index


def _encounters_in_scope(
    client: BlizzardClient,
    index: LootIndex,
    expansions: tuple[str, ...],
    extra_instances: frozenset[int],
) -> list[int]:
    if not expansions:
        return [
            entry["id"]
            for entry in client.journal_encounter_index()
            if isinstance(entry.get("id"), int)
        ]

    wanted = {name.strip().casefold() for name in expansions}
    instance_ids = {
        placement.instance_id
        for placement in index.placements.values()
        if placement.expansion.casefold() in wanted
    } | set(extra_instances)
    if not instance_ids:
        log.warning(
            "no instance matched --expansion %s; known expansions are %s",
            ", ".join(expansions),
            ", ".join(sorted({p.expansion for p in index.placements.values()})),
        )

    encounter_ids: list[int] = []
    for instance_id in sorted(instance_ids):
        payload = client.journal_instance(instance_id)
        for entry in (payload or {}).get("encounters") or []:
            encounter_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(encounter_id, int):
                encounter_ids.append(encounter_id)
    return encounter_ids


# --------------------------------------------------------------------------------
# Merging into the pool file
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Disagreement:
    item_id: int
    name: str
    asserted: str
    derived: str
    evidence: str

    def to_json(self) -> dict:
        return {
            "id": self.item_id,
            "name": self.name,
            "asserted": self.asserted,
            "derived": self.derived,
            "evidence": self.evidence,
        }


@dataclass
class MergeReport:
    """What the derivation agreed with, disagreed with, and could not see."""

    tier: str
    items_total: int = 0
    placed: int = 0
    agreements: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    name_mismatches: list[dict] = field(default_factory=list)
    multi_source: list[dict] = field(default_factory=list)
    obtainable: int = 0
    unobtainable: list[dict] = field(default_factory=list)
    rotation_only_derived: tuple[str, ...] = ()
    rotation_only_asserted: tuple[str, ...] = ()
    rotation_asserted: tuple[str, ...] = ()
    changed: bool = False

    def to_json(self) -> dict:
        return {
            "tier": self.tier,
            "itemsTotal": self.items_total,
            "itemsPlaced": self.placed,
            "agreements": self.agreements,
            "disagreements": [d.to_json() for d in self.disagreements],
            "unresolved": self.unresolved,
            "nameMismatches": self.name_mismatches,
            "multiSource": self.multi_source,
            "obtainableThisSeason": self.obtainable,
            "notObtainableThisSeason": self.unobtainable,
            "rotationAsserted": list(self.rotation_asserted),
            "rotationOnlyDerived": list(self.rotation_only_derived),
            "rotationOnlyAsserted": list(self.rotation_only_asserted),
            "changed": self.changed,
        }


def merge_into_pools(
    raw: dict,
    tier: str,
    index: LootIndex,
    rotation: Rotation,
    meta: dict,
) -> tuple[dict, MergeReport]:
    """Fold the derivation into a parsed ``gear_pools.json`` without overwriting it.

    Returns the new document and the report. Three rules, all of them the project's
    existing provenance discipline rather than anything new here:

    * A derived fact goes in a ``derived`` block. ``source`` is never touched.
    * A disagreement is recorded, printed, and left standing.
    * An item the walk did not place keeps whatever it had, and is listed as
      unresolved *by this pass* -- which, after a scoped run, is not news.
    """
    document = json.loads(json.dumps(raw))  # a copy: the caller's document stays intact
    entry = (document.get("tiers") or {}).get(tier)
    if entry is None:
        raise KeyError(f"no gear pools defined for tier {tier!r}")

    report = MergeReport(tier=tier)
    rotation_instances = rotation.instance_ids

    for slot_entry in (entry.get("slots") or {}).values():
        for item in slot_entry.get("items") or []:
            report.items_total += 1
            item_id = int(item["id"])
            drops = index.drops.get(item_id) or []
            if not drops:
                item.pop("derived", None)
                report.unresolved.append({"id": item_id, "name": item.get("name", "")})
                continue

            report.placed += 1
            pools = {drop.pool for drop in drops if drop.pool}
            primary = drops[0]
            derived_pool = sorted(pools)[0] if len(pools) == 1 else None
            if len(pools) > 1:
                report.multi_source.append(
                    {
                        "id": item_id,
                        "name": item.get("name", ""),
                        "pools": sorted(pools),
                        "from": [drop.summary() for drop in drops],
                    }
                )

            # The rotation is a statement about *dungeons*, so it can only answer for
            # a dungeon drop. Computing it for a raid item yields False by
            # construction -- no raid is ever in the Mythic+ rotation -- and that
            # False is indistinguishable from "cannot be farmed this season", which
            # is how the pool reads it. Measured the hard way on the first live run:
            # every one of MID2's fifteen raid trinkets came back `inRotation: false`
            # and the candidate pool emptied itself. `None` is the honest answer, and
            # the field already means "unknown".
            in_rotation = None
            if rotation_instances and any(drop.kind == "dungeon" for drop in drops):
                in_rotation = any(drop.instance_id in rotation_instances for drop in drops)
            item["derived"] = {
                "source": derived_pool,
                "encounter": primary.encounter,
                "encounterId": primary.encounter_id,
                "instance": primary.instance,
                "instanceId": primary.instance_id,
                "expansion": primary.expansion,
                "inRotation": in_rotation,
                **(
                    {"alsoDroppedBy": [drop.summary() for drop in drops[1:]]}
                    if len(drops) > 1
                    else {}
                ),
            }

            asserted = item.get("source")
            if derived_pool and asserted and derived_pool != asserted:
                report.disagreements.append(
                    Disagreement(
                        item_id=item_id,
                        name=item.get("name", ""),
                        asserted=asserted,
                        derived=derived_pool,
                        evidence=primary.summary(),
                    )
                )
            elif derived_pool and derived_pool == asserted:
                report.agreements += 1

            api_name = primary.item_name
            if api_name and item.get("name") and api_name != item["name"]:
                report.name_mismatches.append(
                    {"id": item_id, "pool": item["name"], "api": api_name}
                )

            # Obtainability is judged on the *derived* pool, not the asserted one: an
            # item the pool wrongly calls Mythic+ is not "a dungeon item outside the
            # rotation", it is a raid item, and listing it here would turn one
            # mistake into two.
            if in_rotation is True:
                report.obtainable += 1
            elif in_rotation is False and derived_pool == "mythicplus":
                report.unobtainable.append(
                    {
                        "id": item_id,
                        "name": item.get("name", ""),
                        "instance": primary.instance,
                    }
                )

    derived_names = rotation.names
    asserted_rotation = tuple(entry.get("dungeonRotation") or ())
    report.rotation_asserted = asserted_rotation
    if asserted_rotation and derived_names:
        report.rotation_only_asserted = tuple(
            name for name in asserted_rotation if name not in derived_names
        )
        report.rotation_only_derived = tuple(
            name for name in derived_names if name not in asserted_rotation
        )

    if derived_names:
        entry["dungeonRotationDerived"] = list(derived_names)
        entry["dungeonRotationDerivedNote"] = (
            "Derived from Blizzard's Game Data API: the Mythic Keystone leaderboards "
            "that exist right now are the dungeons the current season runs. Any "
            "'dungeonRotation' beside this is what a human typed; the two are compared "
            "by `wowdps loot-sources` and never merged."
        )

    entry["lootSources"] = _provenance_block(meta, rotation, index)
    _settle(document, raw, tier, report)
    return document, report


def _provenance_block(meta: dict, rotation: Rotation, index: LootIndex) -> dict:
    return {
        "derivedAt": meta["derivedAt"],
        "api": "Blizzard Game Data API",
        "region": meta["region"],
        "locale": meta["locale"],
        "namespaces": meta["namespaces"],
        "scope": index.scope,
        "encountersRead": index.encounters_read,
        "truncated": index.truncated,
        "season": rotation.season.to_json() if rotation.season else None,
        "tierSeason": meta["tierSeason"],
        "note": (
            "Each item's 'derived' block says which encounter drops it, per "
            "journal-encounter; 'source' beside it is what a human asserted and is "
            "never overwritten. Where they disagree, the disagreement is the finding."
        ),
    }


def _settle(document: dict, previous: dict, tier: str, report: MergeReport) -> None:
    """Keep the published ``derivedAt`` when nothing else moved.

    The same trap ``settle_provenance`` exists for in ``dataset.py``: a wall-clock
    stamp rewrites itself every run, so every run commits, and a diff stops meaning
    anything. Here it would be worse than noise -- the whole value of a committed
    derivation is that a change in it is a change in the game.
    """
    entry = document["tiers"][tier]
    old_entry = (previous.get("tiers") or {}).get(tier) or {}
    old_stamp = ((old_entry.get("lootSources") or {}).get("derivedAt")) or None

    def without_stamp(node: dict) -> str:
        copy = json.loads(json.dumps(node))
        (copy.get("lootSources") or {}).pop("derivedAt", None)
        return json.dumps(copy, sort_keys=True)

    if old_stamp and without_stamp(entry) == without_stamp(old_entry):
        entry["lootSources"]["derivedAt"] = old_stamp
        report.changed = False
        return
    report.changed = True


def tier_season_note(tier: str, rotation: Rotation) -> dict:
    """Tie the dataset's tier to the API's season, explicitly and without guessing.

    ``MID2`` means "Midnight Season 2" to this project (``profiles.tier_label``) and
    means nothing at all to Blizzard, whose season is an integer that is usually not
    named. So the association is stated rather than derived, and the comparison is
    reported so a human can see when it stops holding -- which is exactly what a new
    season looks like from here.
    """
    label = profiles.tier_label(tier)
    season = rotation.season
    if season is None:
        return {
            "tier": tier,
            "tierLabel": label,
            "seasonId": None,
            "seasonName": None,
            "agrees": None,
            "note": "the API did not report a current season, so nothing was compared",
        }
    if not season.name:
        return {
            "tier": tier,
            "tierLabel": label,
            "seasonId": season.season_id,
            "seasonName": None,
            "agrees": None,
            "note": (
                f"the API does not name season {season.season_id}, so tying it to {tier} "
                f"({label}) is asserted by the --tier flag, not derived"
            ),
        }
    agrees = season.name.strip().casefold() == label.strip().casefold()
    return {
        "tier": tier,
        "tierLabel": label,
        "seasonId": season.season_id,
        "seasonName": season.name,
        "agrees": agrees,
        "note": (
            "the API's season name matches the tier label"
            if agrees
            else f"the API calls the current season {season.name!r} where {tier} means "
            f"{label!r}; check that the sweep is running against the right tier"
        ),
    }


# --------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------


def render(report: MergeReport, rotation: Rotation, index: LootIndex, meta: dict) -> list[str]:
    lines: list[str] = []
    add = lines.append

    add(f"=== loot sources: {report.tier} ===")
    add(
        f"region {meta['region']}, locale {meta['locale']}, namespaces "
        f"{meta['namespaces']['journal']} / {meta['namespaces']['mythicKeystone']}"
    )
    add(f"scope: {index.scope}")
    add(
        f"encounters read: {index.encounters_read} of {index.encounters_offered} offered"
        + ("  [WALK TRUNCATED by the request ceiling]" if index.truncated else "")
    )

    add("")
    add("-- this season ------------------------------------------------------------")
    season = rotation.season
    if season is None:
        add("  the API reported no current season")
    else:
        started = _stamp(season.start_timestamp)
        add(
            f"  season {season.season_id}: {season.name or 'unnamed in the API'}, started {started}"
        )
    tie = meta["tierSeason"]
    add(f"  tier {tie['tier']} ({tie['tierLabel']}) vs season: {tie['note']}")

    add("")
    add("-- Mythic+ rotation, derived ----------------------------------------------")
    if not rotation.dungeons:
        add("  none: no current leaderboards came back")
    for dungeon in rotation.dungeons:
        instance = (
            f"journal-instance {dungeon.instance_id} ({dungeon.instance or 'unnamed'})"
            if dungeon.instance_id
            else "NO JOURNAL INSTANCE -- its items cannot be recognised"
        )
        add(f"  {dungeon.name[:34]:<34} keystone {dungeon.keystone_id:<6} -> {instance}")
    for warning in rotation.warnings:
        add(f"  warning: {warning}")

    add("")
    add("-- rotation, derived vs asserted ------------------------------------------")
    if not report.rotation_asserted:
        add("  nothing asserted in the pool file, so there is nothing to disagree with")
    elif not (report.rotation_only_asserted or report.rotation_only_derived):
        add(f"  the two lists agree on all {len(report.rotation_asserted)} dungeons")
    else:
        for name in report.rotation_only_asserted:
            add(f"  ASSERTED ONLY  {name}  (the API's leaderboards do not list it)")
        for name in report.rotation_only_derived:
            add(f"  DERIVED ONLY   {name}  (nobody typed it in)")

    add("")
    add("-- item sources -----------------------------------------------------------")
    add(
        f"  {report.items_total} items in the pool: {report.placed} placed, "
        f"{len(report.unresolved)} unresolved"
    )
    add(
        f"  asserted source agreed on {report.agreements}, disagreed on {len(report.disagreements)}"
    )
    for disagreement in report.disagreements:
        add(
            f"  DISAGREEMENT  {disagreement.name[:32]:<32} asserted {disagreement.asserted} "
            f"-> derived {disagreement.derived}: {disagreement.evidence}"
        )
    for item in report.unresolved:
        add(
            f"  UNRESOLVED    {str(item['name'])[:32]:<32} id {item['id']}: no encounter in "
            f"scope drops it"
        )
    for item in report.multi_source:
        add(
            f"  TWO POOLS     {str(item['name'])[:32]:<32} drops in {', '.join(item['pools'])}: "
            + "; ".join(item["from"])
        )
    for item in report.name_mismatches:
        add(
            f"  NAME DIFFERS  id {item['id']}: pool says {item['pool']!r}, API says {item['api']!r}"
        )

    add("")
    add("-- obtainable this season -------------------------------------------------")
    if not rotation.instance_ids:
        add("  unknown: no rotation was derived, so no item could be placed in or out of it")
    else:
        add(f"  {report.obtainable} of {report.placed} placed items drop in a rotation dungeon")
        for item in report.unobtainable:
            add(
                f"  NOT THIS SEASON  {str(item['name'])[:32]:<32} drops in {item['instance']}, "
                f"which this season does not run"
            )

    return lines


def _stamp(milliseconds: int | None) -> str:
    if not milliseconds:
        return "unknown"
    return datetime.fromtimestamp(milliseconds / 1000, UTC).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------------


def cmd_loot_sources(args: argparse.Namespace) -> int:
    try:
        credentials = Credentials.from_env()
    except BlizzardError as exc:
        log.error("%s", exc)
        return 1

    pool_path = Path(args.pools) if args.pools else equipment.pools_file()
    raw = json.loads(pool_path.read_text(encoding="utf-8"))
    if args.tier not in (raw.get("tiers") or {}):
        log.error("no gear pools defined for tier %s in %s", args.tier, pool_path)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache) if args.cache else out_dir / "cache"

    with BlizzardClient(
        credentials,
        region=args.region,
        locale=args.locale,
        cache_dir=cache_dir,
        rate_per_second=args.rate,
        max_requests=args.max_requests,
    ) as client:
        try:
            rotation = derive_rotation(client)
        except RequestBudgetExhausted as exc:
            log.error("%s", exc)
            return 2
        except BlizzardError as exc:
            log.error("%s", exc)
            return 1

        index = derive_index(
            client,
            expansions=tuple(args.expansion or ()),
            extra_instances=rotation.instance_ids,
        )
        cost = client.ledger.to_json()

    meta = {
        "derivedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "region": args.region,
        "locale": args.locale,
        "namespaces": {
            "journal": f"static-{args.region}",
            "mythicKeystone": f"dynamic-{args.region}",
        },
        "tierSeason": tier_season_note(args.tier, rotation),
    }

    document, report = merge_into_pools(raw, args.tier, index, rotation, meta)
    transcript = render(report, rotation, index, meta)
    transcript.extend(_cost_lines(cost))
    text = "\n".join(transcript)
    print(text)

    (out_dir / f"loot-sources-{args.tier}.txt").write_text(text + "\n", encoding="utf-8")
    (out_dir / f"loot-sources-{args.tier}.json").write_text(
        json.dumps(
            {
                "generatedAt": meta["derivedAt"],
                "tier": args.tier,
                "region": args.region,
                "locale": args.locale,
                "scope": index.scope,
                "rotation": rotation.to_json(),
                "tierSeason": meta["tierSeason"],
                "report": report.to_json(),
                "cost": cost,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )

    if args.dry_run:
        log.info("--dry-run: %s not written", pool_path)
    elif report.changed:
        pool_path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
        log.info("updated %s", pool_path)
    else:
        log.info("%s already carries this derivation; nothing written", pool_path)

    # A disagreement is a finding to be read, not a failure: the pool file is
    # unchanged where it disagrees, so the sweep keeps working either way.
    if index.truncated:
        return 2
    return 0


def _cost_lines(cost: dict) -> list[str]:
    return [
        "",
        "-- cost -------------------------------------------------------------------",
        f"  {cost['requests']} requests ({cost['cacheHits']} served from cache, "
        f"{cost['retries']} retried), "
        f"{cost['shareOfHourlyLimit']:.1%} of the {cost['limitPerHour']}/hour limit",
        "  " + ", ".join(f"{name}={count}" for name, count in cost["byEndpoint"].items()),
        *(
            [f"  {len(cost['failures'])} request(s) came back empty; see the JSON report"]
            if cost["failures"]
            else []
        ),
    ]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tier", default="MID2", help="tier whose pool file is enriched")
    parser.add_argument("--pools", help="alternative gear_pools.json to read and write")
    parser.add_argument("--out", default="loot-sources", help="where the report is written")
    parser.add_argument("--cache", help="response cache directory (default: <out>/cache)")
    parser.add_argument(
        "--region",
        default="us",
        help="API region; sets the static-<region> and dynamic-<region> namespaces",
    )
    parser.add_argument(
        "--locale",
        default="en_US",
        help="names come back in this locale, and they are the join key against simc",
    )
    parser.add_argument(
        "--expansion",
        action="append",
        help="limit the walk to one expansion by name (repeatable). The default reads "
        "every encounter in the journal, which needs no knowledge of which expansion "
        "is current; this makes a re-run cheap, not more correct",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=20.0,
        help="requests per second (Blizzard's cap is 100; default 20)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        help="stop after this many requests and report what was collected",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report without touching the pool file",
    )
