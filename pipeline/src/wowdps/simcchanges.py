"""What changed in SimulationCraft between two published runs.

Three questions a reader has about the engine behind the numbers, and this answers
all three from simc's own git history:

- *Are we on the newest simc?* The workflows clone `--depth 1` with no branch, so
  every run builds simc's default branch at HEAD. Yes, always -- but "always newest"
  is only reassuring if you can see **which** newest, and when it was written.
- *When did it last change?* Not the same as when we built it. `buildDate` is when CI
  compiled the binary, which moves every night whether simc changed or not. The
  revision's own commit date is the honest answer and is what this captures.
- *What changed?* simc ships **no changelog file** -- checked, there is none in the
  repository -- so the commit subjects are the only source. They turn out to be
  unusually disciplined: almost every one carries a `[Tag]` prefix naming the class,
  spec or subsystem it touches, which is what makes a grouped summary possible rather
  than a wall of text.

What is deliberately dropped: simc's automated data-dump commits (`Update Generated
Files <sha>`), which are a third of the stream and say nothing a reader can act on.
They are counted, not listed, so the summary never implies the run was quiet when it
was not.

Nothing here interprets a change. "Fifteen commits touched Death Knight" is a fact;
"Death Knight was buffed" is a reading of a diff this module does not do, and being
confidently wrong about it is worse than being silent -- the same rule the patch-state
panel follows about Blizzard's own notes.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: simc's own automation. A third of the stream and not a change anybody can read.
_GENERATED = re.compile(r"^Update Generated Files\b", re.IGNORECASE)

#: `[Death Knight] ...`, `[gear] ...`, `[live] Game data update ...`. Everything
#: before the first colon-free bracket group.
_TAG = re.compile(r"^\[([^\]]+)\]\s*")

#: Trailing `(#11740)` pull-request numbers -- useful as a link, noise in a subject.
_PR = re.compile(r"\s*\(#(\d+)\)\s*$")


@dataclass(frozen=True)
class Change:
    """One simc commit, as far as this module is willing to read it."""

    revision: str
    date: str
    tag: str | None
    subject: str
    pull_request: int | None = None

    def to_json(self) -> dict:
        out: dict = {"revision": self.revision, "date": self.date, "subject": self.subject}
        if self.tag:
            out["tag"] = self.tag
        if self.pull_request:
            out["pullRequest"] = self.pull_request
        return out


def parse_log(lines: list[str]) -> tuple[list[Change], int]:
    """Read `git log --format=%h|%cs|%s` output. Returns the changes and how many
    automated commits were skipped."""
    changes: list[Change] = []
    generated = 0
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        revision, date, subject = (part.strip() for part in parts)
        if not revision or _GENERATED.match(subject):
            generated += 1
            continue
        pull = _PR.search(subject)
        subject = _PR.sub("", subject).strip()
        tag_match = _TAG.match(subject)
        tag = tag_match.group(1).strip() if tag_match else None
        if tag_match:
            subject = subject[tag_match.end() :].strip()
        changes.append(
            Change(
                revision=revision,
                date=date,
                tag=tag,
                subject=subject,
                pull_request=int(pull.group(1)) if pull else None,
            )
        )
    return changes, generated


def summarise(changes: list[Change], generated: int, limit: int = 12) -> dict:
    """Group by tag, keep the busiest, and count the rest.

    Grouped rather than listed because between two nightly runs simc takes tens of
    commits, and the useful shape is "which parts of the game moved" rather than every
    subject line.
    """
    tags = Counter(change.tag or "untagged" for change in changes)
    return {
        "commits": len(changes),
        "generatedFiles": generated,
        "byTag": [{"tag": tag, "commits": count} for tag, count in tags.most_common(limit)],
        "otherTags": max(0, len(tags) - limit),
        "recent": [change.to_json() for change in changes[:limit]],
    }


def read_log(simc_dir: Path, since: str, until: str = "HEAD") -> list[str]:
    """`git log since..until` in the simc checkout, one line per commit.

    Fails soft: a shallow clone that does not contain `since` is the normal case for
    a `--depth 1` checkout, and the caller publishes "unknown" rather than nothing.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(simc_dir), "log", "--format=%h|%cs|%s", f"{since}..{until}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def revision_date(simc_dir: Path, revision: str = "HEAD") -> str | None:
    """When the revision was committed -- not when we compiled it.

    `buildDate` moves every night whether simc changed or not, so it cannot answer
    "when did the engine last change".
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(simc_dir), "log", "-1", "--format=%cI", revision],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def describe(simc_dir: Path, previous: str | None, current: str = "HEAD") -> dict:
    """The block the manifest carries: when simc last changed, and what moved."""
    block: dict = {"revisionDate": revision_date(simc_dir, current)}
    if not previous:
        block["since"] = None
        block["why"] = "no previously published simc revision to compare against"
        return block

    lines = read_log(simc_dir, previous, current)
    if not lines:
        block["since"] = previous
        # Genuinely unchanged and "the shallow clone cannot see that far back" are
        # different answers, and the second must not print as the first.
        block["why"] = (
            "no commits between the published revision and this one, or the checkout "
            "is too shallow to tell"
        )
        return block

    changes, generated = parse_log(lines)
    block["since"] = previous
    block.update(summarise(changes, generated))
    return block
