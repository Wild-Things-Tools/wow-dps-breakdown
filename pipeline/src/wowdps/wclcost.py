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

import re
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


class CostProbeStopped(CostProbeError):
    """The service refused the rest of the hour; whatever was read is a prefix.

    Its own class rather than a message, because the two answers are different
    actions: a soundness refusal means this pair can never be priced and the run
    should end saying so, where a 429 means the HOUR is spent and the pass stops with
    what it has. `RateLimited` and `PointBudgetExhausted` are separate inheritance
    lines in this package, and three producers filed a 429 as three different wrong
    things before #199-#201; the caller translates once, here, rather than this
    module importing a client it otherwise never needs.
    """


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


@dataclass(frozen=True)
class Difference:
    """The ONE thing two sides of a pair differ in, named so a verdict can say it.

    The block question needed no such object: there was one difference, it was the
    block, and ``compare`` could print the word. The two questions #170 leaves open
    are about an ARGUMENT rather than a field -- does an unfiltered
    ``FIGHT_STRUCTURE`` cost more than a filtered one, does ``includeResources``
    cost anything -- and priced under the old rule both would have been refused,
    correctly, and under a loosened one both would have been reported in a sentence
    naming the block. A noun that travels with the pair is what stops the second.

    Three fields that are decisions rather than description:

    * ``fragments`` is exact text, never a pattern. An expression matching "the
      filter" would also match a filter elsewhere in the document and strip the
      wrong one, and the result is a perfectly good number about the wrong
      difference -- the one failure a reader of this output cannot detect.
    * ``written_on`` says which side's DOCUMENT carries them, and it is **not**
      always the dearer side. Dropping a filter makes the document SHORTER and the
      answer BIGGER, so text length and cost point in opposite directions on exactly
      the question this was built for.
    * ``answers_differ`` is the honesty flag. The block adds three numbers to a
      response, so a difference there is about the ASKING. An argument changes what
      comes back, so a difference there is a cost difference and says nothing about
      whether the charge is per field, per row or per byte. The probe prints that
      rather than leaving a reader to supply the stronger reading.
    """

    noun: str
    fragments: tuple[str, ...]
    written_on: str
    answers_differ: bool

    def carrier(self, lean: str, rich: str) -> tuple[str, str]:
        """The document that carries the fragments, and the one that does not."""
        return (rich, lean) if self.written_on == "rich" else (lean, rich)


def strip_fragments(document: str, difference: Difference) -> str:
    """The document without the text the difference names, each fragment removed once.

    Every fragment must appear EXACTLY once, and the two refusals are one rule
    pointing in opposite directions: a fragment that is absent means this document is
    not the side the difference says carries it, and one that appears twice means the
    text does not identify a single place -- removing the first is then a guess, and a
    guess here builds the other side of a pair out of the wrong edit.
    """
    stripped = document
    for fragment in difference.fragments:
        found = stripped.count(fragment)
        if found != 1:
            raise CostProbeError(
                f"{difference.noun}: the fragment {fragment.strip()!r} appears "
                f"{found} times in this document, and it has to appear exactly once "
                "for the two sides to differ in one place"
            )
        stripped = stripped.replace(fragment, "", 1)
    return stripped


def differs_only_by(lean: str, rich: str, difference: Difference) -> bool:
    """True when the two documents differ in the named difference and nothing else.

    The check a pair's whole meaning rests on. Written as a function rather than left
    inside a test because the CLI asserts it too -- a probe that priced two documents
    differing in two things would produce a number, and the number would be about the
    wrong difference.

    It is not a formality even for documents this package already ships. The two
    event documents differ in their OPERATION NAME as well as in the argument, so
    pairing ``EVENTS_QUERY`` with ``EVENTS_WITH_RESOURCES_QUERY`` as written is
    refused here -- which is why ``build_pairs`` constructs the richer side instead
    of pairing two documents somebody wrote separately.
    """
    carrier, other = difference.carrier(lean, rich)
    try:
        return strip_fragments(carrier, difference) == other
    except CostProbeError:
        return False


#: The difference this probe was built for: one field group, the same answer either way.
BLOCK = Difference(
    noun="the reading block",
    fragments=("\n" + RATE_LIMIT_BLOCK,),
    written_on="rich",
    answers_differ=False,
)


def differs_only_by_the_block(without: str, with_block: str) -> bool:
    """The block case of :func:`differs_only_by`, kept under the name that names it."""
    return differs_only_by(without, with_block, BLOCK)


def with_argument(document: str, *, after: str, argument: str) -> tuple[str, Difference]:
    """The same document with one argument added, and the difference that describes it.

    The counterpart of ``add_rate_limit`` for the argument questions, and it exists
    for the same reason: the probe CONSTRUCTS the richer side, so "the two sides
    differ in one thing" is true by construction rather than by comparison.

    That is not theoretical. ``EVENTS_QUERY`` and ``EVENTS_WITH_RESOURCES_QUERY`` are
    both shipped, both real, and differ in TWO things -- the argument and the
    operation name (``FightEvents`` against ``FightEventsWithResources``). Priced
    against each other they would have answered a question nobody asked.

    The document and the difference are returned **together** because the inserted
    line takes the anchor's own indentation, so the exact text the difference has to
    name is not known until the insertion has happened. Deriving it a second time in
    the caller is how the two drift apart.

    Two refusals:

    * an argument whose NAME the document already carries -- the pair is reversed,
      and returning the document unchanged would price two identical sides and report
      "no difference" with total confidence;
    * an anchor that is absent, or that appears more than once: the insertion point is
      then a guess, and the argument could land in another field's list.
    """
    # Matched as an ARGUMENT -- the name, then a colon -- rather than as a substring.
    # A bare `in` is what the first version did, and a one-character name then matched
    # inside any word of the document: `r` is in `query`, so a pair that differed in
    # `r: true` was refused as reversed before the anchor was ever looked at. Found by
    # the test written for the anchor, which is the useful direction for that to fail in.
    name = argument.split(":", 1)[0].strip()
    if name and re.search(rf"\b{re.escape(name)}\s*:", document):
        raise CostProbeError(f"this document already carries {name!r}; the pair is reversed")
    lines = document.splitlines()
    hits = [i for i, line in enumerate(lines) if after in line]
    if len(hits) != 1:
        raise CostProbeError(
            f"the anchor {after.strip()!r} appears {len(hits)} time(s); it has to appear "
            "exactly once or the argument could land in another field's list"
        )
    index = hits[0]
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    added = f"{indent}{argument}"
    lines.insert(index + 1, added)
    richer = "\n".join(lines) + ("\n" if document.endswith("\n") else "")
    difference = Difference(
        noun=f"`{argument}`",
        fragments=("\n" + added,),
        written_on="rich",
        # An argument changes what comes back. See `Difference`.
        answers_differ=True,
    )
    return richer, difference


@dataclass(frozen=True)
class Variant:
    """One side of a pair: what to send, and what to call it."""

    name: str
    document: str
    variables: dict


@dataclass(frozen=True)
class Pair:
    """Two documents differing in exactly one thing, and the question they answer.

    ``lean`` and ``rich`` are named for the ANSWER rather than for the document:
    ``rich`` is the side asking for more back, which for the filter question is the
    side whose document is *shorter*. They were ``without``/``with_block`` while the
    block was the only difference this could price, and those names would now be
    false on two of the three pairs -- a name promising more than its computation
    delivers is the failure this repository already records under ``inRotation``.
    """

    key: str
    question: str
    difference: Difference
    lean: Variant
    rich: Variant

    def is_sound(self) -> bool:
        return differs_only_by(self.lean.document, self.rich.document, self.difference)


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
    lean: Sample
    rich: Sample
    verdict: str
    sentence: str


def verdict_of(lean: Sample, rich: Sample) -> str:
    """The four states, with no prose attached.

    Split out of ``compare`` so ``sensitivity`` can ask for the verdict without being
    handed a noun it would then have to print correctly and never prints at all. A
    function that takes a name it does not use is a name waiting to be wrong -- and
    the noun stopped being a constant the moment a second kind of difference existed.

    Order matters: a backwards counter is checked FIRST, because its deltas are
    arithmetically fine and mean nothing -- a reset mid-probe makes one delta hugely
    negative and every summary over it plausible.
    """
    every = lean.deltas + rich.deltas
    if not every:
        return "unmeasured"
    if any(d < 0 for d in every):
        return "counter-went-backwards"
    if all(d == 0 for d in every):
        return "unmeasured"
    if lean.low is None or rich.low is None:
        return "unmeasured"
    if rich.low > lean.high:
        return "separates"
    if lean.low > rich.high:
        return "separates"
    return "inside-the-noise"


def compare(lean: Sample, rich: Sample, *, noun: str = "the reading block") -> tuple[str, str]:
    """The verdict, and the sentence that names what the two sides differ in.

    ``noun`` carries a default only so a caller pricing the block reads the sentence
    it always read; every pair built here passes its own, off ``Pair.difference``.
    """
    verdict = verdict_of(lean, rich)
    if verdict == "counter-went-backwards":
        return verdict, (
            "the hourly counter fell during the probe -- the reset fired mid-run, "
            "and every delta spanning it is meaningless"
        )
    if verdict == "unmeasured":
        if not (lean.deltas + rich.deltas):
            return verdict, "no reading came back at all"
        if lean.low is None or rich.low is None:
            return verdict, "one side produced no reading"
        return verdict, (
            "the counter did not move on either side. UNMEASURED -- do not read "
            f"this as '{noun} is free'"
        )
    if verdict == "separates":
        if rich.low is not None and lean.high is not None and rich.low > lean.high:
            return verdict, (
                f"asking for {noun} costs MORE: its cheapest run ({rich.low:g}) "
                f"is above the other side's dearest ({lean.high:g})"
            )
        return verdict, (
            f"asking for {noun} costs LESS, which is a finding about the probe "
            f"rather than about the API: {lean.low:g} > {rich.high:g}"
        )
    return verdict, (
        f"the two ranges overlap ({lean.low:g}-{lean.high:g} against "
        f"{rich.low:g}-{rich.high:g}), so this sample cannot tell them "
        f"apart. That is NOT 'they cost the same'"
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


def sensitivity(lean: Sample, rich: Sample, *, poll: Sample | None = None) -> Sensitivity | None:
    """What extra cost would this sample have separated, under the rule it already used?

    The verdict is asked of ``verdict_of`` rather than re-derived, so the two cannot
    disagree: a sensitivity is reported for exactly the verdict it bounds, and a
    change to the comparison rule moves both at once.

    ``None`` for every other verdict. A sample that separated has its answer, and one
    that is UNMEASURED or backwards has no arithmetic to do -- publishing a number
    there would be a bound computed over readings the module has just refused.
    """
    if verdict_of(lean, rich) != "inside-the-noise":
        return None
    if lean.low is None or lean.high is None or rich.low is None:
        return None  # verdict_of() already refuses this; kept so the arithmetic cannot

    # The rule is `rich.low > lean.high`. Raise every richer round by d and
    # it fires once d clears the gap -- plus one increment, because the test is strict
    # and a difference the counter cannot express is one this method cannot see.
    smallest = round(lean.high - rich.low + COUNTER_QUANTUM, 4)

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
        margin = round(min(lean.low, rich.low) - poll.low, 4)
        query_cost = margin if margin > 0 else None

    return Sensitivity(
        smallest=smallest,
        bar=lean.high,
        floor=rich.low,
        query_cost=query_cost,
        cheapest_rounds_agree=rich.low <= lean.low,
    )


def describe_sensitivity(measure: Sensitivity, *, noun: str = "the reading block") -> list[str]:
    """The bound in words, with the two rounds it is computed from named.

    The rounds are printed because they are what makes the number checkable against
    the deltas listed three lines above it -- and because a bar that is plainly an
    outlier is the difference between a wide sensitivity and a broken run.

    ``noun`` is here for the same reason ``compare`` takes one, and it was missing for
    a reason worth keeping: ``verdict_of`` was split out so ``sensitivity`` need not be
    handed a name it never prints, and that argument was read one function too far.
    This renderer DOES print one, and it printed "the block" on all three pairs of run
    34774586069 -- including the two whose difference is an argument. The
    justification ("it never prints it") stopped being true in the same change that
    added a second kind of difference, and the run written to exercise it is what
    said so.
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
            claim = f"so this bounds {noun} under {scale}"
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
        except CostProbeStopped:
            # Never a per-round error. A 429 is a fact about the HOUR, so walking
            # past it would drop the rounds the hour ran out on and leave a verdict
            # computed over the ones that happened to get in first -- a BIASED
            # subset rather than a smaller one, and nothing in the output could say
            # so. Same two inheritance lines, same wrong filing as #199-#201.
            raise
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
            f"pair {pair.key!r}: the two documents differ in more than "
            f"{pair.difference.noun}, so any number this produced would be about "
            "the wrong difference"
        )
    poll_result = poll_sample or measure_poll(poll=poll, repeats=repeats)
    # The richer side goes FIRST. If the hour resets mid-probe the run is refused
    # either way, but ordering the expensive side first means a budget stop lands
    # before the cheap side rather than after it, leaving a pair with one side.
    rich = measure(pair.rich, poll=poll, send=send, repeats=repeats)
    lean = measure(pair.lean, poll=poll, send=send, repeats=repeats)
    verdict, sentence = compare(lean, rich, noun=pair.difference.noun)
    return PairResult(
        pair=pair,
        poll=poll_result,
        lean=lean,
        rich=rich,
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
        _row(result.rich),
        _row(result.lean),
        "",
        f"    verdict: {result.verdict} -- {result.sentence}",
    ]
    if result.pair.difference.answers_differ:
        # Printed on every such pair rather than only on a separating one: a reader
        # who sees `inside-the-noise` has to know the sides return different data
        # too, or the bound below reads as a bound on the ASKING.
        lines.append(
            "    note: the two sides do not return the same data, so a difference "
            "here is a cost difference and says nothing about whether the charge is "
            "per field, per row or per byte"
        )
    measure = sensitivity(result.lean, result.rich, poll=result.poll)
    if measure is not None:
        lines.append("")
        lines.extend(describe_sensitivity(measure, noun=result.pair.difference.noun))
    for sample in (result.poll, result.rich, result.lean):
        for message in sample.errors:
            lines.append(f"    ! {sample.name}: {message}")
    return lines


#: The two pairs that need a report code, named so a run without one says which
#: questions it did not ask. A probe that silently priced one of three and reported
#: success would be the shape this whole module exists to refuse.
NEEDS_A_REPORT = ("fight-structure filter", "events + includeResources")

#: The filter `FIGHT_STRUCTURE_QUERY` carries, as the two exact fragments that state
#: it. Written on the LEAN side: the filtered document is the longer one and asks for
#: fewer fights back, so stripping these builds the side that costs more, if either
#: does. Both must be removed together -- GraphQL refuses an operation that declares a
#: variable it does not use, so dropping the argument without its declaration produces
#: a document the server rejects rather than a cheaper question.
FIGHT_STRUCTURE_FILTER = Difference(
    noun="the encounter/difficulty filter",
    fragments=(
        ", $encounterId: Int!, $difficulty: Int!",
        "encounterID: $encounterId, difficulty: $difficulty, ",
    ),
    written_on="lean",
    answers_differ=True,
)


def build_pairs(
    *,
    encounter: int,
    difficulty: int,
    page: int,
    report: str | None = None,
    fight: int | None = None,
    events_limit: int = 300,
    event_window_ms: int = 10_000_000,
) -> list[Pair]:
    """The pairs this probe ships, built from the documents it prices.

    The first is #170's block question. The other two are the two measurements #170
    leaves open, and both are about an ARGUMENT rather than a field -- which is why
    they could not be built until ``Difference`` existed: ``differs_only_by_the_block``
    refuses them, correctly, and loosening it to let them through would have priced
    them under a sentence naming the block.

    Both need a **report code**, so both are absent without one. That is the rule
    ``GUILD_PULLS_QUERY`` is still excluded under -- a pair whose inputs need
    discovery is a pair whose cost includes the discovery -- with the difference that
    a report code is not discovered here: it is read off the committed
    ``fights.json`` and passed in, which costs no query at all.

    What they can and cannot answer is worth stating before a number exists. Both
    sides of both pairs return **different amounts of data**, so a separation is a
    cost difference and not evidence about what is being charged for. The block pair
    is the only one of the three where the two sides return the same answer, and that
    is exactly why it was the first one built.
    """
    from . import progresshours
    from .warcraftlogs import EVENTS_QUERY, FIGHT_STRUCTURE_QUERY

    rankings = progresshours.PROGRESS_RANKINGS_QUERY
    pairs = [
        Pair(
            key="progress-rankings + rateLimitData",
            question=(
                "Does adding the reading block to PROGRESS_RANKINGS_QUERY cost points? "
                "If not, progresssweep can drop its standalone rate_limit() per guild."
            ),
            difference=BLOCK,
            lean=Variant("as shipped", rankings, {"e": encounter, "d": difficulty, "p": page}),
            rich=Variant(
                "+ rateLimitData",
                add_rate_limit(rankings),
                {"e": encounter, "d": difficulty, "p": page},
            ),
        )
    ]
    if not report:
        return pairs

    # The unfiltered side declares two variables fewer, so it is sent two fewer. An
    # undeclared variable is ignored by most servers and by none of them reliably;
    # sending only what the document declares removes the question.
    unfiltered = strip_fragments(FIGHT_STRUCTURE_QUERY, FIGHT_STRUCTURE_FILTER)
    pairs.append(
        Pair(
            key="fight-structure filter",
            question=(
                "Does an UNFILTERED fights() cost more than a filtered one? The "
                "catalogue splits its stages on the answer: REPORT_KILLS_QUERY takes "
                "only $code and so answers every boss and difficulty at once, which is "
                "the saving Stufe 3 rests on."
            ),
            difference=FIGHT_STRUCTURE_FILTER,
            lean=Variant(
                "filtered, as shipped",
                FIGHT_STRUCTURE_QUERY,
                {"code": report, "encounterId": encounter, "difficulty": difficulty},
            ),
            rich=Variant("unfiltered", unfiltered, {"code": report}),
        )
    )

    if fight is None:
        return pairs

    with_resources, resources_difference = with_argument(
        EVENTS_QUERY, after="limit: $limit", argument="includeResources: true"
    )
    variables = {
        "code": report,
        "fightId": fight,
        "dataType": "DamageTaken",
        "hostility": "Enemies",
        "startTime": 0,
        "endTime": event_window_ms,
        "limit": events_limit,
    }
    pairs.append(
        Pair(
            key="events + includeResources",
            question=(
                "Does includeResources cost points? spawn-probe cannot ask for a "
                "coordinate without it, so whatever it costs is the floor under every "
                "spawn pass -- and the answer decides whether a shared cache between "
                "the two probes would have been worth anything."
            ),
            difference=resources_difference,
            lean=Variant("as shipped", EVENTS_QUERY, variables),
            rich=Variant("+ includeResources", with_resources, variables),
        )
    )
    return pairs
