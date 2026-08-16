"""Rendering an introspection payload so the answer is readable.

The point of this module is to stop guessing field names, so its tests are about
the two ways the output could mislead: a wrapped type printed as a tree nobody
decodes, and a type the server does not have rendered as though it were empty
rather than absent.
"""

from __future__ import annotations

from wowdps import wclschema


def test_wrapped_types_render_the_way_a_schema_is_written():
    """`[String!]!` beats a three-level nest of NON_NULL and LIST.

    Whether an argument is required is the first thing somebody writing the next
    query needs, and introspection hides it inside the wrappers.
    """
    ref = {
        "kind": "NON_NULL",
        "ofType": {
            "kind": "LIST",
            "ofType": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}},
        },
    }
    assert wclschema.type_name(ref) == "[Int!]!"
    assert wclschema.type_name({"kind": "SCALAR", "name": "Float"}) == "Float"
    assert wclschema.type_name(None) == "?"


def test_a_type_the_server_does_not_have_reads_as_absent_not_empty():
    """The distinction is the whole answer: it says the route does not exist."""
    assert wclschema.describe_type(None) == ["  (no such type on this schema)"]


def test_time_and_progress_shaped_names_are_marked():
    """The half of the output somebody running this is actually looking for."""
    assert wclschema.is_interesting("startTime")
    assert wclschema.is_interesting("endTime")
    assert wclschema.is_interesting("progress")
    assert wclschema.is_interesting("orderBy")
    assert not wclschema.is_interesting("className")
    assert not wclschema.is_interesting("page")


def test_fields_arguments_and_enum_values_all_appear():
    payload = {
        "name": "ReportData",
        "kind": "OBJECT",
        "description": "Report data.\nSecond line dropped.",
        "fields": [
            {
                "name": "reports",
                "type": {"kind": "OBJECT", "name": "ReportPagination"},
                "args": [
                    {"name": "zoneID", "type": {"kind": "SCALAR", "name": "Int"}},
                    {
                        "name": "startTime",
                        "type": {"kind": "SCALAR", "name": "Float"},
                        "defaultValue": "0",
                    },
                ],
            }
        ],
    }

    lines = wclschema.describe_type(payload)
    joined = "\n".join(lines)

    assert "Report data." in joined
    assert "Second line dropped." not in joined
    assert "reports: ReportPagination" in joined
    # The date argument is marked; the unrelated one is not.
    assert "* startTime: Float = 0" in joined
    assert "  zoneID: Int" in joined
    assert "* zoneID" not in joined


def test_enum_values_are_listed_and_marked():
    payload = {
        "name": "FightRankingMetricType",
        "kind": "ENUM",
        "enumValues": [{"name": "speed"}, {"name": "execution"}],
    }
    lines = wclschema.describe_type(payload)
    assert "  * = speed" in lines
    assert "    = execution" in lines
