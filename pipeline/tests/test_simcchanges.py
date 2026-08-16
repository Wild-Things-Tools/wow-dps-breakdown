"""What simc changed between two runs, read from its own commit subjects.

simc ships no changelog file -- checked -- so the subjects are the only source. They
are unusually disciplined, which is what makes a grouped summary possible.
"""

from wowdps.simcchanges import describe, parse_log, summarise

LOG = [
    "8d59b02|2026-08-15|Update Generated Files fcb291e",
    "b8c1dad|2026-08-15|[profiles] renaming generated file, thanks Whispyr for approval",
    "1a2b3c4|2026-08-14|[Death Knight] Overrides for next weeks tuning",
    "5d6e7f8|2026-08-14|[DH] Havoc Midnight Season 2 APL (#11740)",
    "9a0b1c2|2026-08-14|[live] Game data update (Build 69299) (#11738)",
    "3d4e5f6|2026-08-14|update active profiles for raid mid2",
]


def test_automated_data_dumps_are_counted_not_listed():
    """A third of simc's stream is `Update Generated Files <sha>`, which says nothing
    a reader can act on. Counting them keeps the summary from implying the run was
    quiet when it was not."""
    changes, generated = parse_log(LOG)
    assert generated == 1
    assert all("Update Generated Files" not in change.subject for change in changes)
    assert len(changes) == 5


def test_the_bracket_tag_is_lifted_off_the_subject():
    changes, _ = parse_log(LOG)
    by_revision = {change.revision: change for change in changes}
    assert by_revision["1a2b3c4"].tag == "Death Knight"
    assert by_revision["1a2b3c4"].subject == "Overrides for next weeks tuning"
    assert by_revision["b8c1dad"].tag == "profiles"


def test_a_pull_request_number_becomes_a_field_not_part_of_the_subject():
    changes, _ = parse_log(LOG)
    havoc = next(c for c in changes if c.revision == "5d6e7f8")
    assert havoc.pull_request == 11740
    assert havoc.subject == "Havoc Midnight Season 2 APL"
    assert havoc.tag == "DH"


def test_an_untagged_commit_is_kept_rather_than_dropped():
    changes, _ = parse_log(LOG)
    plain = next(c for c in changes if c.revision == "3d4e5f6")
    assert plain.tag is None
    assert plain.subject == "update active profiles for raid mid2"


def test_the_summary_groups_by_tag():
    changes, generated = parse_log(LOG)
    summary = summarise(changes, generated)
    assert summary["commits"] == 5
    assert summary["generatedFiles"] == 1
    tags = {entry["tag"]: entry["commits"] for entry in summary["byTag"]}
    assert tags["Death Knight"] == 1
    assert tags["untagged"] == 1


def test_a_malformed_line_is_skipped_rather_than_crashing():
    changes, _ = parse_log(["not a log line", "", "abc|2026-01-01|[X] fine"])
    assert [c.revision for c in changes] == ["abc"]


def test_no_previous_revision_says_so_instead_of_claiming_nothing_changed(tmp_path):
    block = describe(tmp_path, None)
    assert block["since"] is None
    assert "no previously published" in block["why"]


def test_an_unreadable_checkout_does_not_claim_the_engine_was_quiet(tmp_path):
    """A `--depth 1` clone cannot see back to the published revision. That is not the
    same as "nothing changed", and must not print as it."""
    block = describe(tmp_path, "b642585")
    assert block["since"] == "b642585"
    assert "too shallow" in block["why"]
    assert "commits" not in block
