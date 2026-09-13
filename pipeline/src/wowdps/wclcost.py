"""What does asking for one more field cost? (#170's open measurement)

Warcraft Logs publishes **no cost formula**. This repository's rule has always been
to read ``rateLimitData`` and find out, and every cost figure in ``CLAUDE.md`` comes
from a bracketed pass. What has never been measured is the *marginal* cost of a
difference between two documents, and three of #170's open questions are exactly
that question in three disguises:

* does adding ``rateLimitData`` to a document cost points?
* does an unfiltered ``FIGHT_STRUCTURE`` cost more than a filtered one?
* does ``includeResources`` cost points?

**The first one gates a cron job's hot path.** ``PROGRESS_RANKINGS_QUERY`` and
``GUILD_PULLS_QUERY`` are the two documents in this package that carry no reading,
and the consequence is measured rather than supposed: because they cannot answer
"what does the counter say", both producers have to ask separately -- 180 of 432
queries (42%) on the chart producer before it was cut to one poll per boss, and one
standalone ``rate_limit()`` per guild in ``progresssweep`` to this day. If the field
is free, that poll can go.

WHY THE PAIR IS DERIVED AND NOT TYPED
--------------------------------------
A pair whose two sides are written out by hand is two documents that can drift from
the thing they claim to price -- and the drift would be invisible, because both
sides would still return plausible numbers. So the "with" side is **computed from
the shipped document** by ``add_rate_limit``, and the "without" side IS the shipped
document, imported. The pair therefore differs in exactly the block under test, by
construction rather than by care, and a test asserts the two sides differ in nothing
else.

WHAT THE PROBE REFUSES
-----------------------
* **A cache.** A cached response spends nothing, so a probe run against a warm store
  measures the disk. ``rate_limit`` has said this one function down since it was
  written; here it applies to every query, and the command refuses a cache directory
  rather than quietly producing zeros.
* **A counter that went backwards.** That is the hourly reset firing mid-probe, and
  every delta spanning it is meaningless. Never ``abs()``, never a clamp -- both
  replace a wrong number with a more plausible wrong number.
* **A counter that never moved.** Reported as **UNMEASURED**, never as "free". That
  distinction is this repository's oldest rule about this API and it has been broken
  here once already.

THE THREE-STATE VERDICT, AND WHY NOT TWO
-----------------------------------------
"The two sides cost the same" and "this sample cannot tell them apart" are different
claims, and collapsing them would let three repeats of a noisy counter read as proof
that a field is free -- which is the answer somebody would then act on by changing a
cron job. So ``compare`` returns ``separates`` only when the two sides' observed
ranges are **disjoint**, and ``inside-the-noise`` otherwise.

Disjoint ranges rather than a test statistic, deliberately: at three or four repeats
no test has power worth quoting, and a rule a reader can check by looking at the
printed numbers is worth more than one they have to trust.

AND THE VERDICT IS PUBLISHED WITH ITS SENSITIVITY
--------------------------------------------------
``inside-the-noise`` on its own says only that this sample could not separate the two
sides, which is true of a sample of one and of a sample of a thousand. What a reader
can act on is how far apart they would have had to be: ``sensitivity`` answers that
from the data and the unchanged rule, so it is derivable without knowing the answer
and it changes no verdict.

Measured on the three live runs of 2026-09-13, it is what separates them -- 0.01,
42.01 and 0.01 points against a query that costs about 1.01. Only the third is a
bound, because the first rests on a richer round that is itself dearer than every
control round in the run. See ``Sensitivity`` for why both directions matter.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

#: The block every measured document in this package carries, byte for byte.
#:
#: Taken from the shipped documents rather than retyped -- ``test_wclcost`` asserts
#: it appears verbatim in ``warcraftlogs.RATE_LIMIT_QUERY``, so a reformatting there
#: fails here instead of quietly pricing a block nobody sends.
RATE_LIMIT_BLOCK = "  rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }"

#: The finest increment any reading in this project has been observed to move by.
#:
#: Measured over the three live runs of 2026-09-13 (3, 6 and 12 repeats): every poll
#: delta is an exact integer and every query delta ends in ``.01``, so the counter
#: reports hundredths. It is a floor on what this METHOD may claim rather than a
#: property of the service -- a difference below what a reading can express is one no
#: number of repeats would surface -- so a sensitivity is never reported finer.
COUNTER_QUANTUM = 0.01


class CostProbeError(RuntimeError):
    """The probe cannot answer, and says why rather than returning a number."""


def add_rate_limit(document: str) -> str:
    """The same document with the reading block added, and nothing else changed.

    Inserted after the operation's opening brace, which is the first ``{`` in every
    document this package sends. Two refusals rather than a best effort:

    * a document that already carries the block is refused -- the caller has the
      pair the wrong way round, and silently returning it unchanged would price a
      pair whose two sides are identical and report "no difference" with total
      confidence;
    * a document with no brace is refused, because there is nowhere for the block to
      go and an appended one would be a syntax error the server answers to with an
      error rather than a price.
    """
    if "rateLimitData" in document:
        raise CostProbeError("this document already asks for rateLimitData; the pair is reversed")
    opening = document.find("{")
    if opening < 0:
        raise CostProbeError("no operation brace found; this is not a GraphQL document")
    return document[: opening + 1] + "\n" + RATE_LIMIT_BLOCK + document[opening + 1 :]


def differs_only_by_the_block(without: str, with_block: str) -> bool:
    """True when the two documents differ in the reading block and nothing else.

    The check the pair's whole meaning rests on, and it is one line: remove the block
    and the surrounding whitespace from the richer side and the two must be equal.
    Written as a function rather than left inside a test because the CLI asserts it
    too -- a probe that priced two documents differing in two things would produce a
    number, and the number would be about the wrong difference.
    """
    return with_block.replace("\n" + RATE_LIMIT_BLOCK, "", 1) == without


@dataclass(frozen=True)
class Variant:
    """One side of a pair: what to send, and what to call it."""

    name: str
    document: str
    variables: dict


@dataclass(frozen=True)
class Pair:
    """Two documents differing in exactly one thing, and the question they answer."""

    key: str
    question: str
    without: Variant
    with_block: Variant

    def is_sound(self) -> bool:
        return differs_only_by_the_block(self.without.document, self.with_block.document)


@dataclass
class Sample:
    """What the counter did around one variant, over N repeats."""

    name: str
    deltas: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def low(self) -> float | None:
        return min(self.deltas) if self.deltas else None

    @property
    def high(self) -> float | None:
        return max(self.deltas) if self.deltas else None

    @property
    def median(self) -> float | None:
        return statistics.median(self.deltas) if self.deltas else None


@dataclass
class PairResult:
    """A pair, both samples, the poll's own cost, and the verdict over them."""

    pair: Pair
    poll: Sample
    without: Sample
    with_block: Sample
    verdict: str
    sentence: str


def compare(without: Sample, with_block: Sample) -> tuple[str, str]:
    """Four states, and only one of them is a number a caller may act on.

    Order matters: a backwards counter is checked FIRST, because its deltas are
    arithmetically fine and mean nothing -- a reset mid-probe makes one delta hugely
    negative and every summary over it plausible.
    """
    every = without.deltas + with_block.deltas
    if not every:
        return "unmeasured", "no reading came back at all"
    if any(d < 0 for d in every):
        return (
            "counter-went-backwards",
            "the hourly counter fell during the probe -- the reset fired mid-run, "
            "and every delta spanning it is meaningless",
        )
    if all(d == 0 for d in every):
        return (
            "unmeasured",
            "the counter did not move on either side. UNMEASURED -- do not read "
            "this as 'the field is free'",
        )
    if without.low is None or with_block.low is None:
        return "unmeasured", "one side produced no reading"

    if with_block.low > without.high:
        return (
            "separates",
            f"asking for the block costs MORE: its cheapest run ({with_block.low:g}) "
            f"is above the other side's dearest ({without.high:g})",
        )
    if without.low > with_block.high:
        return (
            "separates",
            f"asking for the block costs LESS, which is a finding about the probe "
            f"rather than about the API: {without.low:g} > {with_block.high:g}",
        )
    return (
        "inside-the-noise",
        f"the two ranges overlap ({without.low:g}-{without.high:g} against "
        f"{with_block.low:g}-{with_block.high:g}), so this sample cannot tell them "
        f"apart. That is NOT 'they cost the same'",
    )


@dataclass(frozen=True)
class Sensitivity:
    """The smallest cost this sample COULD have caught, and what that number rests on.

    ``inside-the-noise`` is an honest verdict and an unhelpful one on its own: it says
    this sample cannot tell the two sides apart, and not how far apart they would have
    to be before it could. That distance is a property of the data and of the unchanged
    rule -- derivable without knowing the answer -- and it is what turns the verdict
    into a bound.

    Two things it is built from, and both are extremes rather than averages, because
    the rule it reports on compares extremes:

    * ``bar`` is the DEAREST control round, which another job spending against the
      shared hourly counter can only push up. A contaminated control round therefore
      WIDENS the sensitivity -- run 2 of 2026-09-13 was blind to a difference forty
      times the query's own cost for exactly that reason.
    * ``floor`` is the CHEAPEST richer round, which contamination can only push up
      too -- and that direction NARROWS the sensitivity, i.e. over-claims. So a small
      number is a bound only while the cheapest richer round is plausibly clean, which
      is what ``cheapest_rounds_agree`` reports and run 1 of 2026-09-13 fails.
    """

    smallest: float
    bar: float
    floor: float
    query_cost: float | None
    cheapest_rounds_agree: bool


def sensitivity(
    without: Sample, with_block: Sample, *, poll: Sample | None = None
) -> Sensitivity | None:
    """What extra cost would this sample have separated, under the rule it already used?

    The verdict is asked of ``compare`` rather than re-derived, so the two cannot
    disagree: a sensitivity is reported for exactly the verdict it bounds, and a
    change to the comparison rule moves both at once.

    ``None`` for every other verdict. A sample that separated has its answer, and one
    that is UNMEASURED or backwards has no arithmetic to do -- publishing a number
    there would be a bound computed over readings the module has just refused.
    """
    verdict, _ = compare(without, with_block)
    if verdict != "inside-the-noise":
        return None
    if without.low is None or without.high is None or with_block.low is None:
        return None  # compare() already refuses this; kept so the arithmetic cannot

    # The rule is `with_block.low > without.high`. Raise every richer round by d and
    # it fires once d clears the gap -- plus one increment, because the test is strict
    # and a difference the counter cannot express is one this method cannot see.
    smallest = round(without.high - with_block.low + COUNTER_QUANTUM, 4)

    # What one query costs on its own: the cheapest round either side managed, less
    # the poll that rides inside every delta. Absent rather than guessed without a
    # poll reading, and absent when the subtraction does not leave anything -- a
    # ratio against zero is not a reading of anything.
    #
    # The poll's CHEAPEST round rather than its median, for the reason every other
    # extreme here is chosen: contamination can only add, so the smallest reading is
    # the cleanest one. It is not academic -- run 1 of 2026-09-13 polled 28, 1 and 2,
    # whose median is 2, and the query duly came out at "about 0.01" against the 1.01
    # the other twenty-three clean rounds agree on.
    query_cost: float | None = None
    if poll is not None and poll.low is not None:
        margin = round(min(without.low, with_block.low) - poll.low, 4)
        query_cost = margin if margin > 0 else None

    return Sensitivity(
        smallest=smallest,
        bar=without.high,
        floor=with_block.low,
        query_cost=query_cost,
        cheapest_rounds_agree=with_block.low <= without.low,
    )


def describe_sensitivity(measure: Sensitivity) -> list[str]:
    """The bound in words, with the two rounds it is computed from named.

    The rounds are printed because they are what makes the number checkable against
    the deltas listed three lines above it -- and because a bar that is plainly an
    outlier is the difference between a wide sensitivity and a broken run.
    """
    lines = [
        f"    sensitivity: a uniform extra cost of {measure.smallest:g} or more would "
        f"have separated here, and none did",
        f"                 the bar is the dearest 'as shipped' round ({measure.bar:g}) "
        f"against the cheapest richer one ({measure.floor:g})",
    ]
    if measure.query_cost:
        ratio = measure.smallest / measure.query_cost
        scale = (
            f"{ratio * 100:.2g}% of what one query costs"
            if ratio < 1
            else f"{ratio:.3g}x what one query costs"
        )
        # The verb follows the flag. A number that cannot be read as a bound must not
        # be printed in a sentence that calls it one, two lines above the clause that
        # withdraws it -- an output contradicting itself is worse than a bare figure.
        if not measure.cheapest_rounds_agree:
            claim = f"so that is {scale}"
        elif ratio < 1:
            claim = f"so this bounds the block under {scale}"
        else:
            claim = f"so this sample is blind to anything under {scale}"
        lines.append(
            f"                 one query itself costs about {measure.query_cost:g}, {claim}"
        )
    if not measure.cheapest_rounds_agree:
        lines.append(
            "                 read it as a counterfactual rather than a bound: the "
            "cheapest richer round is dearer than the cheapest 'as shipped' one, so "
            "the number above rests on a round that may itself be contaminated"
        )
    return lines


def measure(
    variant: Variant,
    *,
    poll: Callable[[], float | None],
    send: Callable[[str, dict], None],
    repeats: int,
) -> Sample:
    """Read, send, read -- ``repeats`` times, keeping every delta.

    The deltas are kept rather than averaged on the way, because the spread is what
    decides the verdict and a mean would hide a single wild reading inside a
    plausible number.
    """
    sample = Sample(name=variant.name)
    for _ in range(max(1, repeats)):
        before = poll()
        try:
            send(variant.document, variant.variables)
        except Exception as exc:  # noqa: BLE001 -- recorded, never swallowed
            sample.errors.append(str(exc))
            continue
        after = poll()
        if before is None or after is None:
            sample.errors.append("a reading did not come back")
            continue
        sample.deltas.append(round(after - before, 4))
    return sample


def measure_poll(*, poll: Callable[[], float | None], repeats: int) -> Sample:
    """What one standalone reading costs, measured before anything else.

    Load-bearing rather than context. A ``read, send, read`` delta contains the
    query AND whatever the second reading costs, and nobody here has established
    whether a response's own counter includes that response. Measuring the poll
    alone first makes the question unnecessary: the same poll sits in both sides, so
    it cancels in the comparison -- and printing it lets a reader check that it did.
    """
    sample = Sample(name="rate_limit() alone")
    previous = poll()
    for _ in range(max(1, repeats)):
        current = poll()
        if previous is None or current is None:
            sample.errors.append("a reading did not come back")
        else:
            sample.deltas.append(round(current - previous, 4))
        previous = current
    return sample


def probe(
    pair: Pair,
    *,
    poll: Callable[[], float | None],
    send: Callable[[str, dict], None],
    repeats: int = 3,
    poll_sample: Sample | None = None,
) -> PairResult:
    """Price both sides of one pair and judge them.

    The pair is checked for soundness FIRST and refused rather than measured: a pair
    whose sides differ in two things yields a perfectly good number about the wrong
    difference, which is the one failure a reader of the output cannot detect.
    """
    if not pair.is_sound():
        raise CostProbeError(
            f"pair {pair.key!r}: the two documents differ in more than the reading "
            "block, so any number this produced would be about the wrong difference"
        )
    poll_result = poll_sample or measure_poll(poll=poll, repeats=repeats)
    # The richer side goes FIRST. If the hour resets mid-probe the run is refused
    # either way, but ordering the expensive side first means a budget stop lands
    # before the cheap side rather than after it, leaving a pair with one side.
    with_block = measure(pair.with_block, poll=poll, send=send, repeats=repeats)
    without = measure(pair.without, poll=poll, send=send, repeats=repeats)
    verdict, sentence = compare(without, with_block)
    return PairResult(
        pair=pair,
        poll=poll_result,
        without=without,
        with_block=with_block,
        verdict=verdict,
        sentence=sentence,
    )


def _row(sample: Sample) -> str:
    if not sample.deltas:
        return f"    {sample.name:<34} no reading"
    body = " ".join(f"{d:g}" for d in sample.deltas)
    return (
        f"    {sample.name:<34} {body}"
        f"   (low {sample.low:g}, median {sample.median:g}, high {sample.high:g})"
    )


def describe_poll(sample: Sample) -> list[str]:
    """What one standalone reading costs, printed once for the whole run.

    Printed separately from the pairs because it is a property of the API rather
    than of any pair, and because it is the number that makes the rest checkable: if
    the poll costs 1 and both sides read 2, the query costs 1 on each side and the
    comparison rests on nothing else.
    """
    lines = ["=== what the standalone reading itself costs", _row(sample)]
    if sample.deltas and all(d == 0 for d in sample.deltas):
        lines.append(
            "    the poll did not move the counter. That is a finding about the "
            "counter's granularity, not a free query"
        )
    for message in sample.errors:
        lines.append(f"    ! {message}")
    return lines


def describe(result: PairResult) -> list[str]:
    """The printed answer: every reading, then the verdict over them.

    Every delta is printed rather than a summary, because the verdict's rule is
    "do the ranges overlap" and a reader has to be able to check it by looking.
    """
    lines = [
        f"=== {result.pair.key}",
        f"    {result.pair.question}",
        "",
        _row(result.poll),
        _row(result.with_block),
        _row(result.without),
        "",
        f"    verdict: {result.verdict} -- {result.sentence}",
    ]
    measure = sensitivity(result.without, result.with_block, poll=result.poll)
    if measure is not None:
        lines.append("")
        lines.extend(describe_sensitivity(measure))
    for sample in (result.poll, result.with_block, result.without):
        for message in sample.errors:
            lines.append(f"    ! {sample.name}: {message}")
    return lines


def build_pairs(*, encounter: int, difficulty: int, page: int) -> list[Pair]:
    """The pairs this probe ships, built from the documents it prices.

    Only the two that #170 names as blocked, and they are the two the ratchet's
    ``_DOCUMENTS_WITHOUT_A_READING`` lists for a reason a measurement can remove.
    ``GUILD_PULLS_QUERY`` is deliberately absent: it needs a guild id and a zone,
    which means discovery, and its answer is the same question as the first pair's
    unless the cost depends on the document rather than on the block -- which is
    itself the thing the first pair establishes.
    """
    from . import progresshours

    rankings = progresshours.PROGRESS_RANKINGS_QUERY
    variables = {"e": encounter, "d": difficulty, "p": page}
    return [
        Pair(
            key="progress-rankings + rateLimitData",
            question=(
                "Does adding the reading block to PROGRESS_RANKINGS_QUERY cost points? "
                "If not, progresssweep can drop its standalone rate_limit() per guild."
            ),
            without=Variant("as shipped", rankings, variables),
            with_block=Variant("+ rateLimitData", add_rate_limit(rankings), variables),
        )
    ]
