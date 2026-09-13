"""What the cost probe must get right, and the two ways it could lie quietly.

The instrument answers a question nobody has measured, so the tests are mostly
about its REFUSALS: a probe that returns a plausible number for the wrong
difference, or reads a motionless counter as "free", is worse than no probe --
somebody would change a cron job on it.
"""

from __future__ import annotations

import argparse

import pytest

from wowdps import cli, progresshours, warcraftlogs, wclcost

# --------------------------------------------------------------- the documents


def test_the_block_is_the_one_the_shipped_documents_actually_carry():
    """A ratchet, not a formality.

    `RATE_LIMIT_BLOCK` is what the probe ADDS, so if the shipped documents are ever
    reformatted the probe would price a block this package does not send -- and the
    answer would still look like an answer. Two documents rather than one, because
    one could be the odd spelling.
    """
    assert wclcost.RATE_LIMIT_BLOCK in warcraftlogs.RATE_LIMIT_QUERY
    assert wclcost.RATE_LIMIT_BLOCK in warcraftlogs.ENCOUNTER_NAME_QUERY


def test_the_shipped_pair_differs_in_the_block_and_in_nothing_else():
    """The claim the whole measurement rests on, checked against the real document."""
    (pair,) = wclcost.build_pairs(encounter=3421, difficulty=5, page=1)

    assert pair.is_sound()
    assert pair.without.document == progresshours.PROGRESS_RANKINGS_QUERY
    assert "rateLimitData" not in pair.without.document
    assert pair.with_block.document.count("rateLimitData") == 1
    # Balanced on both sides. NOT "the same number of braces" -- the block brings
    # its own pair, so that assertion is false by construction and the first
    # version of this test asserted it anyway. What is worth checking here is that
    # the insert produced a document rather than a syntax error; that it differs in
    # nothing ELSE is `is_sound`, one line up, and that check is exhaustive.
    for side in (pair.without.document, pair.with_block.document):
        assert side.count("{") == side.count("}")
    assert pair.with_block.document.count("{") == pair.without.document.count("{") + 1


def test_a_document_that_already_asks_for_the_reading_is_refused():
    """The pair the wrong way round.

    Returning it unchanged would price two IDENTICAL documents and report "no
    difference" with complete confidence -- the most convincing wrong answer this
    probe could give.
    """
    with pytest.raises(wclcost.CostProbeError, match="reversed"):
        wclcost.add_rate_limit(warcraftlogs.RATE_LIMIT_QUERY)


def test_a_document_with_no_operation_brace_is_refused():
    with pytest.raises(wclcost.CostProbeError, match="not a GraphQL document"):
        wclcost.add_rate_limit("query Broken")


# ------------------------------------------------------------------ the verdict


def _sample(name, deltas):
    return wclcost.Sample(name=name, deltas=list(deltas))


def test_a_counter_that_went_backwards_is_refused_rather_than_summarised():
    """The hourly reset firing mid-probe.

    Its deltas are arithmetically fine and mean nothing, so this is checked FIRST --
    a run holding one huge negative delta otherwise produces a perfectly plausible
    median.
    """
    verdict, sentence = wclcost.compare(
        _sample("without", [2.0, 2.0, -1500.0]), _sample("with", [2.0, 2.0, 2.0])
    )

    assert verdict == "counter-went-backwards"
    assert "reset" in sentence


def test_a_counter_that_never_moved_is_UNMEASURED_and_never_free():
    """This repository's oldest rule about this API, and it has been broken once."""
    verdict, sentence = wclcost.compare(
        _sample("without", [0.0, 0.0, 0.0]), _sample("with", [0.0, 0.0, 0.0])
    )

    assert verdict == "unmeasured"
    assert "free" in sentence


def test_overlapping_ranges_are_not_reported_as_the_same_cost():
    """Three states, never two.

    "They cost the same" and "this sample cannot tell them apart" are different
    claims, and only the first would justify changing a cron job.
    """
    verdict, sentence = wclcost.compare(
        _sample("without", [2.0, 3.0, 2.0]), _sample("with", [2.0, 4.0, 3.0])
    )

    assert verdict == "inside-the-noise"
    assert "NOT" in sentence


def test_disjoint_ranges_separate_and_name_the_direction():
    verdict, sentence = wclcost.compare(
        _sample("without", [2.0, 2.0, 2.5]), _sample("with", [4.0, 4.5, 5.0])
    )

    assert verdict == "separates"
    assert "MORE" in sentence


def test_the_cheaper_richer_side_is_named_as_a_finding_about_the_probe():
    """A richer document costing LESS is not a fact about the API.

    It says the probe measured something else -- an ordering effect, a warm path, a
    reset that did not go far enough backwards to be caught. The verdict says so
    rather than publishing "adding a field saves points".
    """
    _, sentence = wclcost.compare(_sample("without", [5.0, 5.0]), _sample("with", [2.0, 2.0]))

    assert "about the probe" in sentence


# -------------------------------------------------------------------- the probe


class _Counter:
    """A fake counter: every poll costs `poll_cost`, every send costs `send_cost`."""

    def __init__(self, poll_cost=1.0, send_cost=2.0, extra_for_block=0.0):
        self.value = 100.0
        self.poll_cost = poll_cost
        self.send_cost = send_cost
        self.extra_for_block = extra_for_block
        self.calls: list[str] = []

    def poll(self):
        self.calls.append("poll")
        self.value += self.poll_cost
        return self.value

    def send(self, document, variables):
        self.calls.append("send")
        self.value += self.send_cost
        if "rateLimitData" in document:
            self.value += self.extra_for_block


def _pair():
    return wclcost.build_pairs(encounter=3421, difficulty=5, page=1)[0]


def test_the_probe_reads_sends_and_reads_again_for_every_repeat():
    """The method, pinned as a call sequence.

    Move the second read one loop out and every delta silently covers two sends;
    drop it and the probe measures the poll. Neither shows in the printed numbers.
    """
    counter = _Counter()
    pair = _pair()

    wclcost.probe(pair, poll=counter.poll, send=counter.send, repeats=2)

    # one leading poll plus two for the poll sample, then read/send/read per repeat
    per_side = ["poll", "send", "poll"] * 2
    assert counter.calls == ["poll", "poll", "poll"] + per_side + per_side


def test_a_pair_that_differs_in_more_than_the_block_is_refused_before_any_query():
    """The one failure a reader of the output could not detect.

    Two documents differing in two things yield a perfectly good number about the
    wrong difference. So this is checked before a single query is sent -- and the
    test asserts exactly that by counting the calls.
    """
    counter = _Counter()
    bad = wclcost.Pair(
        key="bent",
        question="?",
        without=wclcost.Variant("a", "query A { x }", {}),
        with_block=wclcost.Variant("b", "query B { y }", {}),
    )

    with pytest.raises(wclcost.CostProbeError, match="more than the reading block"):
        wclcost.probe(bad, poll=counter.poll, send=counter.send, repeats=2)

    assert counter.calls == []


def test_a_real_extra_cost_is_separated():
    """The end to end claim, against a counter that really does charge for the block."""
    counter = _Counter(poll_cost=1.0, send_cost=2.0, extra_for_block=5.0)

    result = wclcost.probe(_pair(), poll=counter.poll, send=counter.send, repeats=3)

    assert result.verdict == "separates"
    assert "MORE" in result.sentence


def test_an_equal_cost_is_reported_as_unmeasurable_rather_than_equal():
    """The control for the test above, and the more important of the two.

    A counter that charges the same for both sides produces IDENTICAL deltas, whose
    ranges overlap -- so the honest answer is that this sample cannot separate them.
    A probe that called this "the same" would be a probe that can only ever confirm.
    """
    counter = _Counter(poll_cost=1.0, send_cost=2.0, extra_for_block=0.0)

    result = wclcost.probe(_pair(), poll=counter.poll, send=counter.send, repeats=3)

    assert result.verdict == "inside-the-noise"


def test_a_send_that_raises_is_recorded_and_does_not_lose_the_other_repeats():
    counter = _Counter()

    def flaky(document, variables):
        if len([c for c in counter.calls if c == "send"]) == 1:
            counter.calls.append("send")
            raise RuntimeError("the service refused")
        counter.send(document, variables)

    result = wclcost.probe(_pair(), poll=counter.poll, send=flaky, repeats=3)

    assert any(result.with_block.errors or result.without.errors)
    assert result.with_block.deltas or result.without.deltas


# ---------------------------------------------------------------------- the CLI


class _StubClient:
    """Answers what the CLIENT returns, and records whether anything was cached."""

    def __init__(self):
        self.value = 100.0
        self.cache_flags: list[bool] = []
        self.ledger = warcraftlogs.PointLedger()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rate_limit(self):
        self.value += 1.0
        return {"limitPerHour": 18000, "pointsSpentThisHour": self.value, "pointsResetIn": 900}

    def query(self, document, variables=None, label="query", cache=True):
        self.cache_flags.append(cache)
        self.value += 2.0
        return {}


def test_the_command_never_lets_a_query_touch_the_cache(monkeypatch, capsys):
    """The rule that makes the whole probe possible, pinned at the call site.

    A cached response spends nothing, so a probe over a warm store reports zeros --
    which `compare` correctly calls UNMEASURED and a reader would wrongly take for
    "the field is free". There is no `--cache` option; this asserts the queries
    agree with that.
    """
    stub = _StubClient()
    monkeypatch.setattr(warcraftlogs.Credentials, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(warcraftlogs, "WarcraftLogsClient", lambda credentials: stub)

    code = cli.cmd_wcl_cost(
        argparse.Namespace(encounter=3421, difficulty=5, page=1, repeats=2, point_ceiling=0.8)
    )

    assert code == 0
    assert stub.cache_flags, "no query was sent at all"
    assert not any(stub.cache_flags), "a probe query went through the response cache"
    assert "what the standalone reading itself costs" in capsys.readouterr().out


# -------------------------------------------------------------- the sensitivity


def test_the_sensitivity_is_the_smallest_uniform_cost_the_rule_would_have_caught():
    """The arithmetic, against the rule `compare` actually uses.

    Raise every richer round by the answer and `with_block.low > without.high` has to
    fire; raise it by one increment less and it must not. That is the whole claim, so
    the test asserts both halves rather than the number alone.
    """
    without = _sample("without", [2.01, 2.01, 44.01])
    with_block = _sample("with", [2.01, 2.01, 2.01])

    measure = wclcost.sensitivity(without, with_block)

    assert measure is not None
    assert measure.smallest == pytest.approx(42.01)

    lifted = _sample("with", [d + measure.smallest for d in with_block.deltas])
    assert wclcost.compare(without, lifted)[0] == "separates"
    short = _sample(
        "with", [d + measure.smallest - wclcost.COUNTER_QUANTUM for d in with_block.deltas]
    )
    assert wclcost.compare(without, short)[0] == "inside-the-noise"


def test_a_sample_that_already_separated_has_no_sensitivity_to_report():
    """It has its answer. A bound on a question that is settled is noise."""
    assert wclcost.sensitivity(_sample("without", [2.0, 2.0]), _sample("with", [5.0, 5.0])) is None


@pytest.mark.parametrize(
    "without, with_block",
    [
        ([0.0, 0.0], [0.0, 0.0]),  # UNMEASURED
        ([2.0, -1500.0], [2.0, 2.0]),  # the hourly reset fired mid-probe
        ([], []),  # nothing came back at all
    ],
)
def test_a_refused_sample_gets_no_sensitivity(without, with_block):
    """A bound computed over readings the module has just refused would be a number
    wearing a measurement's clothes -- exactly what the refusals exist to prevent."""
    assert wclcost.sensitivity(_sample("without", without), _sample("with", with_block)) is None


def test_the_verdict_and_its_bound_cannot_disagree():
    """The sensitivity asks `compare` rather than re-deriving the rule.

    Two implementations of one comparison is the duplication this repository keeps
    paying for, and here it would publish a bound under a verdict that contradicts it.
    """
    seen = set()
    for without, with_block in (
        ([2.01, 2.01], [2.01, 2.01]),
        ([2.0, 2.0], [5.0, 5.0]),
        ([5.0, 5.0], [2.0, 2.0]),
        ([0.0], [0.0]),
    ):
        a, b = _sample("without", without), _sample("with", with_block)
        verdict, _ = wclcost.compare(a, b)
        seen.add(verdict)
        assert (wclcost.sensitivity(a, b) is not None) == (verdict == "inside-the-noise")
    assert seen == {"inside-the-noise", "separates", "unmeasured"}


def test_a_dearer_cheapest_richer_round_is_named_rather_than_bounded():
    """Contamination on the richer side's MINIMUM narrows the sensitivity.

    That is the one direction in which this number over-claims, so a sample where the
    cheapest richer round is above the cheapest control round says so instead of
    publishing a tight bound. Run 1 of 2026-09-13 is exactly that shape.
    """
    # The poll is load-bearing rather than decoration: without it `query_cost` is None,
    # the ratio line is never printed, and the assertion below that the verb follows
    # the flag is vacuously true. It was, on the first attempt -- see CLAUDE.md.
    measure = wclcost.sensitivity(
        _sample("without", [3.01, 2.01, 2.01]),
        _sample("with", [11.01, 3.01, 4.01]),
        poll=_sample("poll", [28.0, 1.0, 2.0]),
    )

    assert measure is not None
    assert measure.smallest == pytest.approx(0.01)
    assert not measure.cheapest_rounds_agree
    printed = "\n".join(wclcost.describe_sensitivity(measure))
    assert "counterfactual rather than a bound" in printed
    # And the verb follows the flag: a line calling it a bound, two lines above the
    # clause withdrawing that, is an output that contradicts itself.
    assert "bounds the block" not in printed


def test_the_query_cost_needs_a_poll_and_is_absent_without_one():
    """The ratio is the useful half and it is not guessed.

    A delta contains the query AND the poll that brackets it, so the query's own cost
    is only expressible where the poll was measured. Absent, never assumed.
    """
    without, with_block = _sample("without", [2.01, 2.01]), _sample("with", [2.01, 2.01])

    assert wclcost.sensitivity(without, with_block).query_cost is None
    priced = wclcost.sensitivity(without, with_block, poll=_sample("poll", [1.0, 1.0]))
    assert priced.query_cost == pytest.approx(1.01)
    printed = "\n".join(wclcost.describe_sensitivity(priced))
    assert "bounds the block under 0.99% of what one query costs" in printed


def test_describe_prints_the_bound_only_under_the_verdict_it_bounds():
    pair = _pair()
    overlapping = wclcost.probe(pair, poll=_Counter().poll, send=_Counter().send, repeats=2)
    assert overlapping.verdict == "inside-the-noise"
    assert any("sensitivity:" in line for line in wclcost.describe(overlapping))

    counter = _Counter(extra_for_block=5.0)
    separating = wclcost.probe(pair, poll=counter.poll, send=counter.send, repeats=3)
    assert separating.verdict == "separates"
    assert not any("sensitivity:" in line for line in wclcost.describe(separating))


# The three live runs of 2026-09-13, read out of their own job logs. Runs 34761515445,
# 34761672707 and 34761774999 -- 3, 6 and 12 repeats of `3421`/Mythic/page 1.
MEASURED_RUNS = {
    "34761515445": {
        "poll": [28.0, 1.0, 2.0],
        "with": [11.01, 3.01, 4.01],
        "without": [3.01, 2.01, 2.01],
        "smallest": 0.01,
        "agree": False,
        "query": 1.01,  # the poll read 28/1/2; its MEDIAN would price a query at 0.01
    },
    "34761672707": {
        "poll": [1.0] * 6,
        "with": [2.01, 52.01, 2.01, 2.01, 2.01, 2.01],
        "without": [2.01, 2.01, 4.01, 44.01, 3.01, 2.01],
        "smallest": 42.01,
        "agree": True,
        "query": 1.01,
    },
    "34761774999": {
        "poll": [1.0] * 12,
        "with": [2.01] * 9 + [34.01, 2.01, 2.01],
        "without": [2.01] * 12,
        "smallest": 0.01,
        "agree": True,
        "query": 1.01,
    },
}


@pytest.mark.parametrize("run_id", sorted(MEASURED_RUNS))
def test_the_three_measured_runs_reproduce_their_published_sensitivity(run_id):
    """The figures in CLAUDE.md, pinned against the deltas the runs actually printed.

    They are the whole evidence for the claim that the block costs under 1% of its
    query, so a change to the arithmetic that quietly moved them would move a published
    conclusion. All three verdicts were `inside-the-noise` live, which is the control:
    a sensitivity is only defined there.
    """
    row = MEASURED_RUNS[run_id]
    without, with_block = _sample("without", row["without"]), _sample("with", row["with"])

    assert wclcost.compare(without, with_block)[0] == "inside-the-noise"
    measure = wclcost.sensitivity(without, with_block, poll=_sample("poll", row["poll"]))
    assert measure.smallest == pytest.approx(row["smallest"])
    assert measure.cheapest_rounds_agree is row["agree"]
    assert measure.query_cost == pytest.approx(row["query"])
