"""Deriving item sources and the season rotation, against hand-written payloads.

Everything here is offline. The API cannot be reached from a development checkout,
so the fixtures are written by hand from the published response shapes -- the same
approach ``test_fightextract.py`` takes with log events. What that buys is the whole
extraction and, more importantly, the *merge*: the rule that a derived fact never
overwrites an asserted one and that a disagreement is reported rather than resolved.

The fixtures are shaped after MID2: a raid whose trinkets the pool calls "raid", a
current-expansion dungeon and a legacy dungeon in the rotation, and one item the
structural inference put in the wrong pool.
"""

from __future__ import annotations

import argparse
import json

import pytest

from wowdps import equipment, lootsources
from wowdps.lootsources import (
    LootIndex,
    Rotation,
    RotationDungeon,
    Season,
    drops_from_encounter,
    merge_into_pools,
    placements_from_expansion,
    rotation_from_leaderboards,
    season_from_index,
    tier_season_note,
)

HOST = "https://us.api.blizzard.com/data/wow"


def href(kind: str, entity_id: int) -> dict:
    return {"key": {"href": f"{HOST}/{kind}/{entity_id}?namespace=static-us"}}


def named(kind: str, entity_id: int, name: str) -> dict:
    return {**href(kind, entity_id), "name": name, "id": entity_id}


def expansion_payload(name: str, raids: list[tuple[int, str]], dungeons: list[tuple[int, str]]):
    return {
        "id": 516,
        "name": name,
        "raids": [named("journal-instance", i, n) for i, n in raids],
        "dungeons": [named("journal-instance", i, n) for i, n in dungeons],
    }


def encounter_payload(
    encounter_id: int,
    name: str,
    instance: tuple[int, str] | None,
    items: list[tuple[int, str]],
    category: str | None = None,
):
    payload: dict = {
        "id": encounter_id,
        "name": name,
        # The journal entry's own id in `id`, the real item id one level down. Reading
        # the outer one produces a table that joins to nothing.
        "items": [
            {"id": 900000 + index, "item": named("item", item_id, item_name)}
            for index, (item_id, item_name) in enumerate(items)
        ],
    }
    if instance:
        payload["instance"] = named("journal-instance", *instance)
    if category:
        payload["category"] = {"type": category}
    return payload


def keystone_payload(keystone_id: int, instance_id: int, instance: str):
    return {
        "id": keystone_id,
        "name": instance,
        "dungeon": {
            "key": {"href": f"{HOST}/journal-instance/{instance_id}?namespace=static-us"},
            "name": instance,
            "id": instance_id,
        },
        "keystone_upgrades": [],
        "is_tracked": True,
    }


# --------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------


def test_an_expansion_classifies_its_instances():
    placements = placements_from_expansion(
        expansion_payload("Midnight", [(1400, "Sszorak's Reach")], [(1401, "Altar of Fangs")])
    )
    by_id = {p.instance_id: p for p in placements}
    assert by_id[1400].kind == "raid"
    assert by_id[1401].kind == "dungeon"
    assert by_id[1400].expansion == "Midnight"


def test_an_instance_id_can_come_from_the_href_alone():
    payload = {"name": "Midnight", "raids": [href("journal-instance", 1400)], "dungeons": []}
    assert [p.instance_id for p in placements_from_expansion(payload)] == [1400]


def test_drops_read_the_item_id_not_the_journal_entry_id():
    placements = {1400: lootsources.InstancePlacement(1400, "Sszorak's Reach", "Midnight", "raid")}
    drops = drops_from_encounter(
        encounter_payload(2500, "Belo'ren", (1400, "Sszorak's Reach"), [(270160, "Shellward")]),
        placements,
    )
    assert [d.item_id for d in drops] == [270160]
    assert drops[0].pool == "raid"
    assert drops[0].instance == "Sszorak's Reach"
    assert drops[0].expansion == "Midnight"


def test_a_dungeon_drop_maps_to_the_mythicplus_pool():
    placements = {
        1401: lootsources.InstancePlacement(1401, "Altar of Fangs", "Midnight", "dungeon")
    }
    drops = drops_from_encounter(
        encounter_payload(2600, "Fangcaller", (1401, "Altar of Fangs"), [(250248, "Medicine")]),
        placements,
    )
    assert drops[0].pool == "mythicplus"


def test_the_encounter_category_is_the_fallback_when_no_expansion_claimed_it():
    """A scoped walk can meet an instance no fetched expansion listed."""
    drops = drops_from_encounter(
        encounter_payload(2700, "A Boss", (9999, "Somewhere"), [(1, "Thing")], category="RAID"),
        {},
    )
    assert drops[0].pool == "raid"


def test_a_world_boss_lands_in_no_pool_rather_than_a_wrong_one():
    drops = drops_from_encounter(
        encounter_payload(2800, "Big Elemental", None, [(2, "Thing")], category="WORLD_BOSS"),
        {},
    )
    assert drops[0].pool is None


def test_the_rotation_joins_keystone_dungeons_to_journal_instances():
    entries = [{"id": 542, "name": "Altar of Fangs"}, {"id": 244, "name": "Kings' Rest"}]
    payloads = {
        542: keystone_payload(542, 1401, "Altar of Fangs"),
        244: keystone_payload(244, 1023, "Kings' Rest"),
    }
    dungeons, warnings = rotation_from_leaderboards(entries, payloads)
    assert [d.instance_id for d in dungeons] == [1401, 1023]
    assert not warnings


def test_a_dungeon_with_no_instance_is_kept_and_warned_about():
    """Dropping it would quietly shorten a list whose whole point is completeness."""
    dungeons, warnings = rotation_from_leaderboards(
        [{"id": 542, "name": "Altar of Fangs"}], {542: {}}
    )
    assert len(dungeons) == 1 and dungeons[0].instance_id is None
    assert "Altar of Fangs" in warnings[0]


def test_the_current_season_comes_from_the_index():
    season = season_from_index(
        {"current_season": {"id": 15}, "seasons": [{"id": 14}, {"id": 15}]},
        {"id": 15, "season_name": None, "start_timestamp": 1_755_000_000_000},
    )
    assert season is not None
    assert season.season_id == 15 and season.is_current and season.name is None


# --------------------------------------------------------------------------------
# Tier to season
# --------------------------------------------------------------------------------


def test_an_unnamed_season_is_reported_as_asserted_not_derived():
    note = tier_season_note("MID2", Rotation(season=Season(15, None, None, None, True)))
    assert note["agrees"] is None
    assert "asserted by the --tier flag" in note["note"]


def test_a_named_season_that_matches_the_tier_agrees():
    note = tier_season_note("MID2", Rotation(season=Season(15, "Midnight Season 2", 0, 0, True)))
    assert note["agrees"] is True


def test_a_named_season_that_does_not_match_says_so():
    note = tier_season_note("MID2", Rotation(season=Season(16, "Midnight Season 3", 0, 0, True)))
    assert note["agrees"] is False
    assert "MID2" in note["note"]


# --------------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------------


def pool_document() -> dict:
    """A pool file shaped like the shipped one, with one item asserted wrongly."""
    return {
        "tiers": {
            "MID2": {
                "itemLevels": [{"id": "mythic", "label": "top", "ilevel": 344}],
                "slots": {
                    "trinket": {
                        "baselineSource": "mythicplus",
                        "candidateSource": "raid",
                        "items": [
                            {"id": 270160, "name": "Shellward", "slug": "s", "source": "raid"},
                            {"id": 250248, "name": "Medicine", "slug": "m", "source": "mythicplus"},
                            # Structurally inferred as Mythic+; the API says raid.
                            {"id": 250253, "name": "Whisper", "slug": "w", "source": "mythicplus"},
                            # In a dungeon this season does not run.
                            {"id": 250259, "name": "Sapling", "slug": "sa", "source": "mythicplus"},
                            # Nothing in the walk drops it.
                            {"id": 111111, "name": "Ghost", "slug": "g", "source": "mythicplus"},
                        ],
                    }
                },
            }
        }
    }


def built_index() -> LootIndex:
    index = LootIndex(scope="test", encounters_read=4, encounters_offered=4)
    placements = {
        1400: lootsources.InstancePlacement(1400, "Sszorak's Reach", "Midnight", "raid"),
        1401: lootsources.InstancePlacement(1401, "Altar of Fangs", "Midnight", "dungeon"),
        1402: lootsources.InstancePlacement(1402, "The Blinding Vale", "Midnight", "dungeon"),
    }
    index.placements = placements
    for payload in (
        encounter_payload(2500, "Belo'ren", (1400, "Sszorak's Reach"), [(270160, "Shellward")]),
        # The disagreement: a "mythicplus" item that the raid actually drops.
        encounter_payload(2501, "Vashnik", (1400, "Sszorak's Reach"), [(250253, "Whisper")]),
        encounter_payload(2600, "Fangcaller", (1401, "Altar of Fangs"), [(250248, "Medicine")]),
        encounter_payload(2700, "Valekeeper", (1402, "The Blinding Vale"), [(250259, "Sapling")]),
    ):
        for drop in drops_from_encounter(payload, placements):
            index.add(drop)
    return index


def rotation_without_the_vale() -> Rotation:
    return Rotation(
        season=Season(15, None, 1_755_000_000_000, None, True),
        connected_realm_id=11,
        dungeons=(RotationDungeon(542, "Altar of Fangs", 1401, "Altar of Fangs"),),
    )


def meta(stamp: str = "2026-08-14T10:00:00+00:00") -> dict:
    return {
        "derivedAt": stamp,
        "region": "us",
        "locale": "en_US",
        "namespaces": {"journal": "static-us", "mythicKeystone": "dynamic-us"},
        "tierSeason": {"tier": "MID2", "tierLabel": "Midnight Season 2", "note": "x"},
    }


def merged():
    return merge_into_pools(
        pool_document(), "MID2", built_index(), rotation_without_the_vale(), meta()
    )


def items_by_id(document: dict) -> dict[int, dict]:
    return {item["id"]: item for item in document["tiers"]["MID2"]["slots"]["trinket"]["items"]}


def test_a_derived_source_never_overwrites_the_asserted_one():
    """The whole provenance rule, in one assertion."""
    document, report = merged()
    whisper = items_by_id(document)[250253]
    assert whisper["source"] == "mythicplus"  # untouched
    assert whisper["derived"]["source"] == "raid"
    assert [d.item_id for d in report.disagreements] == [250253]
    assert report.disagreements[0].evidence.startswith("Vashnik, Sszorak's Reach")


def test_agreements_are_counted_rather_than_listed():
    _, report = merged()
    assert report.agreements == 3
    assert report.placed == 4 and report.items_total == 5


def test_an_item_no_encounter_drops_is_reported_not_guessed():
    document, report = merged()
    assert [u["id"] for u in report.unresolved] == [111111]
    assert "derived" not in items_by_id(document)[111111]


def test_rotation_membership_is_derived_per_item():
    document, report = merged()
    by_id = items_by_id(document)
    assert by_id[250248]["derived"]["inRotation"] is True
    assert by_id[250259]["derived"]["inRotation"] is False
    assert [u["id"] for u in report.unobtainable] == [250259]
    assert report.obtainable == 1


def test_a_misfiled_raid_item_is_not_reported_as_out_of_rotation():
    """Otherwise one mistake in the pool file would print as two findings."""
    _, report = merged()
    assert 250253 not in [u["id"] for u in report.unobtainable]
    assert [d.item_id for d in report.disagreements] == [250253]


def test_no_rotation_means_unknown_rather_than_false():
    """An empty rotation must not read as 'nothing is obtainable this season'."""
    document, report = merge_into_pools(pool_document(), "MID2", built_index(), Rotation(), meta())
    assert items_by_id(document)[250248]["derived"]["inRotation"] is None
    assert report.unobtainable == []


def test_the_derived_rotation_is_written_beside_any_asserted_one():
    raw = pool_document()
    raw["tiers"]["MID2"]["dungeonRotation"] = ["Altar of Fangs", "Murder Row"]
    document, report = merge_into_pools(
        raw, "MID2", built_index(), rotation_without_the_vale(), meta()
    )
    entry = document["tiers"]["MID2"]
    assert entry["dungeonRotation"] == ["Altar of Fangs", "Murder Row"]  # untouched
    assert entry["dungeonRotationDerived"] == ["Altar of Fangs"]
    assert report.rotation_only_asserted == ("Murder Row",)
    assert report.rotation_only_derived == ()


def test_nothing_asserted_means_nothing_to_disagree_with():
    _, report = merged()
    assert report.rotation_asserted == ()
    assert report.rotation_only_asserted == () and report.rotation_only_derived == ()


def test_a_name_the_api_spells_differently_is_flagged():
    raw = pool_document()
    raw["tiers"]["MID2"]["slots"]["trinket"]["items"][0]["name"] = "Shelward"
    _, report = merge_into_pools(raw, "MID2", built_index(), rotation_without_the_vale(), meta())
    assert report.name_mismatches == [{"id": 270160, "pool": "Shelward", "api": "Shellward"}]


def test_the_caller_document_is_not_mutated():
    raw = pool_document()
    merge_into_pools(raw, "MID2", built_index(), rotation_without_the_vale(), meta())
    assert "derived" not in raw["tiers"]["MID2"]["slots"]["trinket"]["items"][0]


def test_an_unchanged_derivation_keeps_its_timestamp():
    """Same trap as ``generatedAt``: a stamp that churns makes every diff meaningless."""
    first, report_one = merge_into_pools(
        pool_document(), "MID2", built_index(), rotation_without_the_vale(), meta()
    )
    assert report_one.changed

    second, report_two = merge_into_pools(
        first, "MID2", built_index(), rotation_without_the_vale(), meta("2026-09-01T00:00:00+00:00")
    )
    assert not report_two.changed
    assert second["tiers"]["MID2"]["lootSources"]["derivedAt"] == "2026-08-14T10:00:00+00:00"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_a_real_change_takes_the_new_timestamp():
    first, _ = merge_into_pools(
        pool_document(), "MID2", built_index(), rotation_without_the_vale(), meta()
    )
    moved = Rotation(
        season=Season(16, None, None, None, True),
        connected_realm_id=11,
        dungeons=(RotationDungeon(543, "Murder Row", 1402, "Murder Row"),),
    )
    second, report = merge_into_pools(
        first, "MID2", built_index(), moved, meta("2026-09-01T00:00:00+00:00")
    )
    assert report.changed
    assert second["tiers"]["MID2"]["lootSources"]["derivedAt"] == "2026-09-01T00:00:00+00:00"


def test_an_unknown_tier_is_refused():
    with pytest.raises(KeyError):
        merge_into_pools(pool_document(), "MID9", built_index(), Rotation(), meta())


# --------------------------------------------------------------------------------
# What the pool loader does with the result
# --------------------------------------------------------------------------------


def test_load_pools_reads_the_derived_block_and_reports_disagreements(tmp_path):
    document, _ = merged()
    document["tiers"]["MID2"]["slots"]["trinket"]["items"] = [
        {**item, "primaryStat": "intellect", "baseIlevel": 219, "baseQuality": 4}
        for item in document["tiers"]["MID2"]["slots"]["trinket"]["items"]
    ]
    path = tmp_path / "pools.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    pools = equipment.load_pools("MID2", path)
    assert [item.item_id for item in pools.source_disagreements()] == [250253]
    assert pools.derived_rotation == ("Altar of Fangs",)
    assert pools.dungeon_rotation == ()


def test_the_rotation_disagreement_needs_both_lists(tmp_path):
    pools = equipment.GearPools(tier="MID2", dungeon_rotation=("A", "B"))
    assert pools.rotation_disagreement() == ((), ())
    pools = equipment.GearPools(
        tier="MID2", dungeon_rotation=("A", "B"), derived_rotation=("B", "C")
    )
    assert pools.rotation_disagreement() == (("A",), ("C",))


# --------------------------------------------------------------------------------
# Walking, against a fake client
# --------------------------------------------------------------------------------


class FakeClient:
    """Answers the handful of calls the walk makes, and records the order."""

    def __init__(self, **payloads):
        self.payloads = payloads
        self.calls: list[str] = []
        self.ledger = FakeLedger()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def mythic_keystone_season_index(self):
        self.calls.append("season-index")
        return {"current_season": {"id": 15}}

    def mythic_keystone_season(self, season_id):
        self.calls.append(f"season:{season_id}")
        return {"id": season_id, "season_name": None, "start_timestamp": 1}

    def connected_realm_index(self):
        self.calls.append("realms")
        return self.payloads.get("realms", [11, 121])

    def mythic_leaderboard_index(self, realm_id):
        self.calls.append(f"leaderboards:{realm_id}")
        return self.payloads.get("leaderboards", [])

    def mythic_keystone_dungeon(self, dungeon_id):
        self.calls.append(f"keystone:{dungeon_id}")
        return self.payloads.get("keystones", {}).get(dungeon_id)

    def journal_expansion_index(self):
        self.calls.append("expansion-index")
        return [{"id": 516}]

    def journal_expansion(self, expansion_id):
        self.calls.append(f"expansion:{expansion_id}")
        return self.payloads.get("expansion")

    def journal_encounter_index(self):
        self.calls.append("encounter-index")
        return [{"id": e["id"]} for e in self.payloads.get("encounters", [])]

    def journal_encounter(self, encounter_id):
        self.calls.append(f"encounter:{encounter_id}")
        return next(
            (e for e in self.payloads.get("encounters", []) if e["id"] == encounter_id), None
        )

    def journal_instance(self, instance_id):
        self.calls.append(f"instance:{instance_id}")
        return self.payloads.get("instances", {}).get(instance_id)


def test_derive_rotation_uses_the_lowest_connected_realm():
    """Any realm answers, so the pass picks one deterministically and stays cached."""
    client = FakeClient(
        leaderboards=[{"id": 542, "name": "Altar of Fangs"}],
        keystones={542: keystone_payload(542, 1401, "Altar of Fangs")},
    )
    rotation = lootsources.derive_rotation(client)
    assert rotation.connected_realm_id == 11
    assert rotation.names == ("Altar of Fangs",)
    assert rotation.season is not None and rotation.season.season_id == 15


def test_an_empty_leaderboard_index_is_called_out():
    rotation = lootsources.derive_rotation(FakeClient(leaderboards=[]))
    assert rotation.dungeons == ()
    assert "unstarted season" in rotation.warnings[0]


def test_the_default_walk_reads_every_encounter_in_the_index():
    encounters = [
        encounter_payload(2500, "Belo'ren", (1400, "Sszorak's Reach"), [(270160, "Shellward")]),
        encounter_payload(2600, "Fangcaller", (1401, "Altar of Fangs"), [(250248, "Medicine")]),
    ]
    client = FakeClient(
        expansion=expansion_payload(
            "Midnight", [(1400, "Sszorak's Reach")], [(1401, "Altar of Fangs")]
        ),
        encounters=encounters,
    )
    index = lootsources.derive_index(client)
    assert index.encounters_read == 2
    assert index.drops[270160][0].pool == "raid"
    assert index.drops[250248][0].pool == "mythicplus"
    assert "every encounter" in index.scope


def test_a_scoped_walk_still_covers_rotation_dungeons_from_older_expansions():
    """The reason the scope takes the rotation's instances as well: legacy dungeons."""
    legacy = encounter_payload(3000, "Kings", (1023, "Kings' Rest"), [(160000, "Old Trinket")])
    current = encounter_payload(2600, "Fangcaller", (1401, "Altar of Fangs"), [(250248, "Med")])
    client = FakeClient(
        expansion=expansion_payload("Midnight", [], [(1401, "Altar of Fangs")]),
        encounters=[current, legacy],
        instances={
            1401: {"encounters": [{"id": 2600}]},
            1023: {"encounters": [{"id": 3000}]},
        },
    )
    index = lootsources.derive_index(
        client, expansions=("Midnight",), extra_instances=frozenset({1023})
    )
    assert set(index.drops) == {250248, 160000}
    assert "encounter-index" not in client.calls  # scoped: no full walk


# --------------------------------------------------------------------------------
# The command, end to end with the client stubbed out
# --------------------------------------------------------------------------------


def run_command(tmp_path, monkeypatch, **flags):
    """``wowdps loot-sources`` with a fake client, writing to a temporary pool file."""
    pool_path = tmp_path / "pools.json"
    pool_path.write_text(json.dumps(pool_document()), encoding="utf-8")

    fake = FakeClient(
        expansion=expansion_payload(
            "Midnight",
            [(1400, "Sszorak's Reach")],
            [(1401, "Altar of Fangs"), (1402, "The Blinding Vale")],
        ),
        encounters=[
            encounter_payload(2500, "Belo'ren", (1400, "Sszorak's Reach"), [(270160, "Shellward")]),
            encounter_payload(2501, "Vashnik", (1400, "Sszorak's Reach"), [(250253, "Whisper")]),
            encounter_payload(2600, "Fangcaller", (1401, "Altar of Fangs"), [(250248, "Medicine")]),
            encounter_payload(
                2700, "Valekeeper", (1402, "The Blinding Vale"), [(250259, "Sapling")]
            ),
        ],
        leaderboards=[{"id": 542, "name": "Altar of Fangs"}],
        keystones={542: keystone_payload(542, 1401, "Altar of Fangs")},
    )
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "secret")
    monkeypatch.setattr(lootsources, "BlizzardClient", lambda *a, **k: fake)

    options = {
        "tier": "MID2",
        "pools": str(pool_path),
        "out": str(tmp_path / "out"),
        "cache": None,
        "region": "us",
        "locale": "en_US",
        "expansion": None,
        "rate": 0.0,
        "max_requests": None,
        "dry_run": False,
        **flags,
    }
    return lootsources.cmd_loot_sources(argparse.Namespace(**options)), pool_path, tmp_path / "out"


class FakeLedger:
    def to_json(self) -> dict:
        return {
            "requests": 9,
            "cacheHits": 0,
            "retries": 0,
            "byEndpoint": {"journal-encounter": 4},
            "limitPerHour": 36000,
            "shareOfHourlyLimit": 0.00025,
            "failures": [],
        }


def test_the_command_writes_the_pool_file_and_a_report(tmp_path, monkeypatch, capsys):
    status, pool_path, out_dir = run_command(tmp_path, monkeypatch)
    assert status == 0

    document = json.loads(pool_path.read_text(encoding="utf-8"))
    entry = document["tiers"]["MID2"]
    assert entry["dungeonRotationDerived"] == ["Altar of Fangs"]
    assert entry["lootSources"]["namespaces"]["journal"] == "static-us"
    assert items_by_id(document)[250253]["source"] == "mythicplus"

    report = json.loads((out_dir / "loot-sources-MID2.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in report["report"]["disagreements"]] == [250253]
    assert "DISAGREEMENT" in capsys.readouterr().out


def test_the_command_leaves_the_file_alone_on_a_dry_run(tmp_path, monkeypatch):
    status, pool_path, _ = run_command(tmp_path, monkeypatch, dry_run=True)
    assert status == 0
    assert "lootSources" not in json.loads(pool_path.read_text(encoding="utf-8"))["tiers"]["MID2"]


def test_missing_credentials_stop_the_command_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("BLIZZARD_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET", raising=False)
    args = argparse.Namespace(tier="MID2", pools=None, out=str(tmp_path))
    assert lootsources.cmd_loot_sources(args) == 1


def test_in_rotation_is_left_unknown_for_a_raid_drop():
    """Answering it False would be indistinguishable from "cannot be farmed", which
    is how the pool reads the field -- and no raid is ever in a dungeon rotation."""
    from wowdps.lootsources import ItemDrop, LootIndex, Rotation, RotationDungeon, merge_into_pools

    index = LootIndex(encounters_read=2, encounters_offered=2)
    index.add(
        ItemDrop(
            item_id=270160,
            item_name="Raid Trinket",
            encounter_id=1,
            encounter="Vorasius",
            instance_id=1300,
            instance="The Venomous Abyss",
            expansion="Midnight",
            kind="raid",
        )
    )
    index.add(
        ItemDrop(
            item_id=250215,
            item_name="Dungeon Trinket",
            encounter_id=2,
            encounter="A boss",
            instance_id=1200,
            instance="Murder Row",
            expansion="Midnight",
            kind="dungeon",
        )
    )
    rotation = Rotation(
        dungeons=(
            RotationDungeon(
                keystone_id=1, name="Murder Row", instance_id=1200, instance="Murder Row"
            ),
        )
    )
    raw = {
        "tiers": {
            "MID2": {
                "itemLevels": [],
                "slots": {
                    "trinket": {
                        "baselineSource": "mythicplus",
                        "candidateSource": "raid",
                        "items": [
                            {"id": 270160, "name": "Raid Trinket", "source": "raid"},
                            {"id": 250215, "name": "Dungeon Trinket", "source": "mythicplus"},
                        ],
                    }
                },
            }
        }
    }
    meta = {
        "derivedAt": "2026-08-15T00:00:00+00:00",
        "region": "us",
        "locale": "en_US",
        "namespaces": {"journal": "static-us", "mythicKeystone": "dynamic-us"},
        "tierSeason": {"tier": "MID2", "note": "asserted"},
    }
    document, _ = merge_into_pools(raw, "MID2", index, rotation, meta)
    items = document["tiers"]["MID2"]["slots"]["trinket"]["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id[270160]["derived"]["inRotation"] is None
    assert by_id[250215]["derived"]["inRotation"] is True
