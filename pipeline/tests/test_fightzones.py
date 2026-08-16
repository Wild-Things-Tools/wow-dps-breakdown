"""Which season a boss list belongs to, and the repair when it belongs to another.

The bug this module exists for shipped: nine encounters were filed under ``MID2``
that Warcraft Logs places in a raid whose season had ended. So the tests are mostly
about the two things that make that recoverable -- the placement check finding it
without anybody asserting the right answer, and the move carrying the owner's hand
facts across intact.
"""

from __future__ import annotations

import json

import pytest

from wowdps import fightzones


def zone_payload() -> list[dict]:
    """Two raids as ``worldData.zones`` returns them: last season's and this one's.

    Oldest first, which is the order the service uses and the order
    ``suggest_current_zone`` depends on.
    """
    return [
        {
            "id": 38,
            "name": "The Voidspire",
            "frozen": True,
            "encounters": [
                {"id": 3176, "name": "Imperator Averzian"},
                {"id": 3180, "name": "Lightblinded Vanguard"},
            ],
        },
        {
            "id": 42,
            "name": "The Venomous Abyss",
            "frozen": False,
            "encounters": [
                {"id": 3201, "name": "The Lost Explorers"},
                {"id": 3202, "name": "Vashnik the Malignant"},
            ],
        },
    ]


def test_parse_keeps_service_order_and_the_frozen_flag() -> None:
    zones = fightzones.parse_zones(zone_payload())
    assert [zone.name for zone in zones] == ["The Voidspire", "The Venomous Abyss"]
    assert [zone.frozen for zone in zones] == [True, False]
    assert zones[1].encounter_ids == {3201, 3202}


def test_a_zone_with_no_encounters_survives_parsing() -> None:
    """The state a raid is in between being announced and being killed.

    Dropping it would hide exactly the zone somebody checking on a season turn is
    looking for.
    """
    zones = fightzones.parse_zones([{"id": 43, "name": "The Tidebound Grotto", "frozen": False}])
    assert len(zones) == 1
    assert zones[0].encounters == ()


def test_locate_names_the_frozen_zone_a_misfiled_tier_sits_in() -> None:
    """The check that catches the shipped bug, with nothing asserted up front."""
    zones = fightzones.parse_zones(zone_payload())
    placement = fightzones.locate("MID2", [3176, 3180], zones)

    assert len(placement.zones) == 1
    zone, hits = placement.zones[0]
    assert zone.name == "The Voidspire"
    assert zone.frozen is True
    assert hits == 2
    assert placement.unplaced == ()


def test_locate_reports_an_encounter_no_zone_claims() -> None:
    zones = fightzones.parse_zones(zone_payload())
    placement = fightzones.locate("MID2", [3176, 99999], zones)
    assert placement.unplaced == (99999,)


def test_locate_spans_two_zones_rather_than_picking_one() -> None:
    """A raid split across two journal instances is real -- MID2's gear side is one."""
    zones = fightzones.parse_zones(zone_payload())
    placement = fightzones.locate("MID2", [3176, 3201, 3202], zones)
    assert [zone.name for zone, _ in placement.zones] == ["The Venomous Abyss", "The Voidspire"]
    assert [hits for _, hits in placement.zones] == [2, 1]


def test_suggestion_is_the_newest_live_zone_and_states_its_reasoning() -> None:
    zones = fightzones.parse_zones(zone_payload())
    suggestion = fightzones.suggest_current_zone(zones)
    assert suggestion.zone is not None
    assert suggestion.zone.name == "The Venomous Abyss"
    assert "not frozen" in suggestion.reason


def test_no_live_zone_suggests_nothing_rather_than_the_newest_frozen_one() -> None:
    payload = [dict(entry, frozen=True) for entry in zone_payload()]
    suggestion = fightzones.suggest_current_zone(fightzones.parse_zones(payload))
    assert suggestion.zone is None
    assert "frozen" in suggestion.reason


def hand_fact() -> dict:
    return {
        "value": {"baseline": 3, "constant": True},
        "provenance": {"source": "hand", "statedBy": "owner", "detail": "played it"},
    }


def profiles_raw() -> dict:
    return {
        "note": "n",
        "tiers": {
            "MID2": {
                "difficulty": 5,
                "encounters": [
                    {"encounterId": 3176, "name": "Imperator Averzian", "facts": {}},
                    {
                        "encounterId": 3180,
                        "name": "Lightblinded Vanguard",
                        "facts": {"targets": hand_fact()},
                    },
                ],
            }
        },
    }


def test_move_carries_hand_facts_across_and_empties_the_source() -> None:
    """The facts are about the fight; only the label on it was wrong."""
    raw = profiles_raw()
    moved = fightzones.move_tier(raw, "MID2", "MID1")

    assert moved == 2
    assert "MID2" not in raw["tiers"]
    landed = raw["tiers"]["MID1"]["encounters"]
    assert [item["encounterId"] for item in landed] == [3176, 3180]
    assert landed[1]["facts"]["targets"]["provenance"]["source"] == "hand"


def test_move_never_overwrites_an_encounter_the_destination_already_has() -> None:
    raw = profiles_raw()
    raw["tiers"]["MID1"] = {
        "difficulty": 5,
        "encounters": [{"encounterId": 3180, "name": "already here", "facts": {}}],
    }

    moved = fightzones.move_tier(raw, "MID2", "MID1")

    assert moved == 1
    assert [item["name"] for item in raw["tiers"]["MID1"]["encounters"]] == [
        "Imperator Averzian",
        "already here",
    ]
    # The one it refused to overwrite stays under the source rather than vanishing.
    assert [item["encounterId"] for item in raw["tiers"]["MID2"]["encounters"]] == [3180]


def test_move_from_a_tier_that_does_not_exist_is_an_error() -> None:
    with pytest.raises(KeyError):
        fightzones.move_tier(profiles_raw(), "MID9", "MID1")


def test_seeding_adds_the_zone_and_leaves_existing_facts_alone() -> None:
    raw = profiles_raw()
    zones = fightzones.parse_zones(zone_payload())
    fightzones.move_tier(raw, "MID2", "MID1")

    result = fightzones.seed_tier(raw, "MID2", zones[1])

    assert [entry.encounter_id for entry in result.added] == [3201, 3202]
    assert result.changed is True
    assert raw["tiers"]["MID1"]["encounters"][1]["facts"]["targets"]["provenance"]["source"] == (
        "hand"
    )


def test_seeding_twice_changes_nothing_the_second_time() -> None:
    """A re-run has to be free, or the workflow cannot be scheduled."""
    raw = profiles_raw()
    zone = fightzones.parse_zones(zone_payload())[0]

    fightzones.seed_tier(raw, "MID1", zone)
    before = json.dumps(raw, sort_keys=True)
    again = fightzones.seed_tier(raw, "MID1", zone)

    assert again.changed is False
    assert sorted(again.kept) == [3176, 3180]
    assert json.dumps(raw, sort_keys=True) == before


def test_seeding_keeps_an_encounter_the_zone_does_not_list() -> None:
    """Deleting it would take its hand facts with it, which is never worth doing."""
    raw = profiles_raw()
    zone = fightzones.parse_zones(zone_payload())[1]

    result = fightzones.seed_tier(raw, "MID2", zone)

    assert result.absent == [3176, 3180]
    assert {item["encounterId"] for item in raw["tiers"]["MID2"]["encounters"]} == {
        3176,
        3180,
        3201,
        3202,
    }


def test_seeded_encounters_are_written_in_id_order() -> None:
    raw = {"tiers": {}}
    zone = fightzones.Zone(
        zone_id=1,
        name="z",
        frozen=False,
        encounters=(
            fightzones.Encounter(9, "last"),
            fightzones.Encounter(2, "first"),
        ),
    )
    fightzones.seed_tier(raw, "MID3", zone)
    assert [item["encounterId"] for item in raw["tiers"]["MID3"]["encounters"]] == [2, 9]
