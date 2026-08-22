"""Ask Warcraft Logs what it actually offers, instead of guessing field names.

Every GraphQL document in this project was written against a *third-party mirror*
of the v2 schema, because ``warcraftlogs.com/v2-api-docs`` is 403 without a browser
session. CLAUDE.md says so and treats the first live run of anything as a schema
check. That worked for the queries we already have, and it is a bad way to answer
an open question like "is there a route to the first kill by date rather than by
damage" -- a guessed argument name comes back as an error at best and as a
plausible wrong answer at worst.

GraphQL can be asked. This runs introspection against the live endpoint and prints
the fields, arguments and enum values of the types that bear on that question, so
the next query is written from the server's own answer.

Introspection being *disabled* is also an answer, and the command says which it
got rather than falling back to a guess.

Why the question matters, restated so this module is readable on its own:
``characterRankings`` is sorted by damage and contains ranked parses only, so
"earliest kills" can only ever be computed by sorting a window of the best parses
by date. A guild that killed the boss on the first night with a slow pull ranks low
and can sit outside that window entirely. Any field that is natively ordered by
*time*, or any search that takes a date range, answers the question directly
instead of approximating it.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Types that bear on "which kills, and in what order". Introspection is one
#: request per type here, which is nothing; the list is short because a full
#: schema dump is thousands of lines nobody reads.
DEFAULT_TYPES = (
    # Where characterRankings lives. Measured on the first live run: it also carries
    # `fightRankings(metric: FightRankingMetricType)`, which is the fight-level
    # ranking -- a different question from "which player parsed highest" and the one
    # that bears on progress. Both ranking fields return an untyped `JSON` scalar,
    # so there is no `EncounterRankings` object type to introspect; the enums below
    # are where the orderings are actually named.
    "Encounter",
    "FightRankingMetricType",
    "CharacterRankingMetricType",
    # The route that would not need rankings at all: a report search bounded by a
    # date range returns logs in time order, and is not restricted to parses
    # Warcraft Logs chose to rank.
    "ReportData",
    "ReportPagination",
    "Report",
    "ReportFight",
    # Zone and partition metadata, which is how "when did this raid open" would be
    # answered without hard-coding a date.
    "WorldData",
    "Zone",
    "Partition",
)

TYPE_QUERY = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    kind
    description
    enumValues { name description }
    fields {
      name
      description
      type { ...TypeRef }
      args {
        name
        description
        defaultValue
        type { ...TypeRef }
      }
    }
    inputFields {
      name
      description
      defaultValue
      type { ...TypeRef }
    }
  }
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType { kind name ofType { kind name } }
  }
}
"""


def type_name(ref: dict | None) -> str:
    """Render a possibly-wrapped type reference as ``[Foo!]!``.

    Introspection nests NON_NULL and LIST wrappers rather than naming them, so a
    reader wanting to know whether an argument is required has to unwrap it. Doing
    that here is the difference between the output being usable and being a tree
    somebody has to decode by hand.
    """
    if not ref:
        return "?"
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return f"{type_name(ref.get('ofType'))}!"
    if kind == "LIST":
        return f"[{type_name(ref.get('ofType'))}]"
    return str(ref.get("name") or "?")


#: Argument and field names that would answer the ordering question. Matched as
#: substrings, case-folded, so `startTime`, `start_time` and `beforeDate` all hit.
_TIME_HINTS = ("time", "date", "start", "end", "since", "progress", "speed", "order", "sort")


def is_interesting(name: str) -> bool:
    """Does this name bear on ordering by time or progress?"""
    lowered = name.lower()
    return any(hint in lowered for hint in _TIME_HINTS)


def describe_type(payload: dict | None) -> list[str]:
    """Render one introspected type as lines, marking the time/progress-shaped bits.

    A type the server does not have comes back as ``None`` and is reported as
    absent, which is a real answer -- it says the route does not exist rather than
    that the query was malformed.
    """
    if not payload:
        return ["  (no such type on this schema)"]

    lines: list[str] = []
    if payload.get("description"):
        lines.append(f"  {payload['description'].strip().splitlines()[0]}")

    for value in payload.get("enumValues") or []:
        mark = "*" if is_interesting(value["name"]) else " "
        lines.append(f"  {mark} = {value['name']}")

    for field in payload.get("fields") or []:
        mark = "*" if is_interesting(field["name"]) else " "
        lines.append(f"  {mark} {field['name']}: {type_name(field.get('type'))}")
        for arg in field.get("args") or []:
            arg_mark = "*" if is_interesting(arg["name"]) else " "
            default = f" = {arg['defaultValue']}" if arg.get("defaultValue") else ""
            lines.append(f"      {arg_mark} {arg['name']}: {type_name(arg.get('type'))}{default}")

    for field in payload.get("inputFields") or []:
        mark = "*" if is_interesting(field["name"]) else " "
        lines.append(f"  {mark} .{field['name']}: {type_name(field.get('type'))}")

    return lines or ["  (no fields)"]
