"""Searching for a talent build, and only claiming what the search can back.

simc runs an action list; it does not optimise. So a build nobody has written down has
to be *constructed* -- ``talenttree.encode_loadout`` writes the hash, ``talentedit``
makes the edits, and this module decides which edits are worth simulating and how to
tell a real difference from Monte Carlo noise.

Which builds this searches over, and why that is the whole design
----------------------------------------------------------------
**Only edits that leave the set of selected nodes exactly as it was.** That is one
sentence and it is the load-bearing decision in this file, so here is what it buys and
what it costs.

simc validates almost nothing (``talentedit``'s docstring lists all eleven checks
``parse_traits_hash`` performs). It has **no unlock edges, no point gates and no point
budget**, and it will happily simulate for ten minutes a build that takes a capstone
with nothing leading to it. The number that comes back is a real simulation of a
character no player could make -- which is worse than no number, because it publishes
as one.

Two of those three, this project can check: ``req_points`` is column 6 of
``trait_data.inc`` and ``talentedit.derive_point_budget`` reads a ceiling off the
tier's own shipped profiles. The third, unlock edges, **is not in simc's data at all**
(checked on ``625a591``, 2026-08-23: ``engine/dbc/generated/`` ships
``__trait_data_data``, ``__trait_definition_effect_data`` and
``__trait_sub_tree_data`` and no edge table), and the only file-reachable copy of
Blizzard's is one class of thirteen, seven months stale. So a search that adds or drops
a node **cannot know whether the result is reachable**.

The way out is not to check the edges but to make the question not arise:

    If the seed's node set is legal, and an edit does not change which nodes are
    selected, then every unlock edge satisfied by the seed is satisfied by the
    result -- because an unlock edge is a statement about which nodes are taken,
    and that set is identical.

That is a proof rather than a heuristic, it needs no edge table, and it is asserted
directly in ``test_buildsearch.py`` over generated mutations rather than argued. The
same invariance carries the other two checks for free: ``move_rank`` preserves each
tree's total (``talentedit._totals_preserved`` asserts it), and a gate is a threshold
on a tree's total, so gates and budget survive as well.

**What it costs is real and is stated wherever a result is published**: the search
cannot discover that a different *set* of talents is better. It searches the choice
nodes and the rank distribution of a build somebody already wrote, and it is
emphatically not "the best build for this spec" -- calling it that would be the kind of
claim this repository exists not to make.

How big that space actually is, measured on simc ``625a591`` over MID2's 39 searchable
builds rather than estimated:

* **flippable choice nodes: 1 minimum, 9 median, 16 maximum**, so the choice axis runs
  from 2 builds to 65,536. The floor is not a rounding case: both Frost Death Knight
  builds carry exactly **one**, which is the same two profiles ``talenttree`` already
  flags for spending 10 class points, and a search over two builds is barely a search.
* **rank moves are almost always unavailable: 33 of the 39 builds offer none at all**,
  and the most any build offers is 4 pairs. MID2's profiles put nodes at full rank or
  at one rank, so there is rarely a rank to move that would not empty its source.

So in practice this is a **choice-node search**, and the rank-move machinery earns its
place on six builds. That is worth stating plainly rather than describing the space as
though both axes were live: a reader told "choices and ranks" would reasonably assume
the second contributes, and on five builds in six it contributes nothing.

Blind calibration
-----------------
A search has to earn trust before it is used where nobody knows the answer, and the way
to earn it is to run it where somebody does: on the specs simc ships, blind, and see
whether it finds what simc found.

**Blind here means blind in the dimension being searched.** ``scramble`` overwrites
every choice node with a deterministic pseudo-random entry before round 0, so the
starting point carries *no information* about which entries simc picked -- the search
must rediscover them. The node set is inherited and is therefore **not** part of the
claim: the search does not search over node sets, so it can be neither right nor wrong
about them, and a calibration that pretended otherwise would be measuring something it
does not do. That limit is published with every calibration row.

Pruning is on evidence, never on noise
--------------------------------------
Successive halving: a wide field at low precision, then half the field at four times
the iterations, and so on. The saving is real -- most candidates are eliminated at
300 iterations, which cost about a tenth of a 3000-iteration run each.

The trap is that at 300 iterations the standard error is around 0.3%, and real talent
differences are often smaller than that. A rule that keeps "the top half by DPS" will
therefore throw away the eventual winner whenever it happens to roll low, and the
search will then converge, confidently, on something else. So the rule here is the
project's tie rule and nothing else:

    a candidate is eliminated only when the round's leader beats it by more than
    ``hypot(errorLeader, errorCandidate)``.

Everything inside the band survives, however many that is. The field shrinks anyway,
because quadrupling the iterations halves the errors and the band closes -- but if it
does not shrink, the honest answer is *the field did not separate*, and this reports
that rather than picking one. When a round's survivors exceed what the budget can
carry, the excess is dropped **for budget** and counted separately from what was
pruned on evidence, because those are two different sentences and only one of them is
a finding.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from . import talentedit, talenttree
from .talenttree import Loadout, Selection, Trait

if TYPE_CHECKING:  # pragma: no cover -- import cycle at runtime, types only
    from pathlib import Path

    from .scenarios import SimSettings
    from .simc_runner import SimRequest

log = logging.getLogger(__name__)

#: Iterations the first round runs at. simc's error at 300 is around 0.3%, which
#: resolves a difference of roughly 1% -- enough to throw out a badly wrong build and
#: nowhere near enough to rank two good ones, which is exactly what the tie rule is
#: there to stop the search forgetting.
START_ITERATIONS = 300
#: Iterations the last round runs at. 3000 measures at ~0.05%, the figure every other
#: sweep in this project uses, and resolves ~0.1%.
FINAL_ITERATIONS = 3000
#: Iterations multiply by this each round; the field halves.
ITERATION_FACTOR = 4
#: How many one-edit improvements the opening phase will chase before handing the
#: field to the halving rounds. A blind seed scrambles a median of nine choice nodes and
#: each step corrects at most one, so this is what bounds a blind search's reach -- and
#: a run that spends all of them says so rather than presenting a truncated climb as a
#: converged one.
CLIMB_STEPS = 12
#: How many builds the last rounds carry. Not 1: the runner-up is published so that a
#: near-tie at the cut is visible from outside the file, and a schedule that narrows to
#: a single survivor has thrown that away before anyone can look at it.
FINAL_KEEP = 3
#: Most variants one simc invocation is asked to carry. Beyond this the round is split
#: -- profilesets parallelise across threads, and a single enormous run is a single
#: enormous thing to lose.
MAX_VARIANTS_PER_ROUND = 48

#: Origins, matching the display contract's ``DpsComputedOrigin``.
ORIGIN_SIMC = "simc"
ORIGIN_HARVEST = "harvest"
ORIGIN_SEARCH = "search"


class SearchError(ValueError):
    """A search that cannot be run as asked."""


# --------------------------------------------------------------------------------
# Candidates and measurements
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One build to simulate, and where it came from.

    ``key`` is the profileset name, so it has to survive simc's own option parsing:
    short, and no characters that mean something in an option line.
    """

    key: str
    label: str
    origin: str
    loadout: Loadout
    talent_hash: str
    #: The edits that produced it, in order, for the search evidence block.
    lineage: tuple[str, ...] = ()
    #: The candidate this one was derived from, or None for a seed.
    parent: str | None = None

    @property
    def nodes_taken(self) -> frozenset[int]:
        """The invariant the whole legality argument rests on. See the module docstring."""
        return frozenset(s.node_id for s in self.loadout.selections)


@dataclass(frozen=True)
class Measurement:
    """One candidate's DPS and the error that bounds it. Both, always."""

    key: str
    dps: float
    #: Standard error of the mean, in percent -- simc's own ``dpsError``.
    dps_error: float
    iterations: int
    priority_dps: float | None = None


def tie_band(error_a_pct: float, error_b_pct: float) -> float:
    """The two means' standard errors added in quadrature, as a fraction.

    The project's uncertainty convention, and deliberately not a fixed percentage: the
    band tracks the precision a run actually achieved, so a 3000-iteration round
    separates builds a 300-iteration round has to call tied.
    """
    return math.hypot(error_a_pct / 100.0, error_b_pct / 100.0)


def separated(leader: Measurement, trailer: Measurement) -> bool:
    """True when the leader beats the trailer by more than the band. Equal is a tie."""
    if trailer.dps <= 0:
        return True
    margin = leader.dps / trailer.dps - 1
    return margin > tie_band(leader.dps_error, trailer.dps_error)


# --------------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Round:
    iterations: int
    #: How many survivors this round may carry into the next one.
    keep: int


def plan_rounds(
    field: int,
    *,
    start: int = START_ITERATIONS,
    final: int = FINAL_ITERATIONS,
    factor: int = ITERATION_FACTOR,
) -> tuple[Round, ...]:
    """Successive halving: quadruple the iterations, halve the field, per round.

    The last round is clamped to ``final`` rather than allowed to overshoot: from 1200
    the next quadrupling is 4800, which costs 60% more than 3000 for precision the
    project's other sweeps do not buy either. Clamping keeps the schedule's promise --
    the final round resolves what a 3000-iteration run resolves -- without paying past
    it.

    A field of one still gets one round, at ``final``. There is nothing to prune, and
    the point of the run is then the measurement rather than the ranking.
    """
    if field < 1:
        raise SearchError("cannot plan a search over an empty field")
    if start < 1 or final < start:
        raise SearchError(f"iteration schedule must rise: start {start}, final {final}")
    if factor < 2:
        raise SearchError(f"iterations must at least double each round, not {factor}x")

    rounds: list[Round] = []
    iterations, keep = start, field
    while iterations < final and keep > FINAL_KEEP:
        keep = max(FINAL_KEEP, math.ceil(keep / 2))
        rounds.append(Round(iterations=iterations, keep=keep))
        iterations = min(iterations * factor, final)
    rounds.append(Round(iterations=final, keep=FINAL_KEEP))
    return tuple(rounds)


@dataclass(frozen=True)
class RoundOutcome:
    """What one round did, with pruning and truncation kept apart.

    ``pruned`` is a finding -- those candidates lost by more than the band. ``truncated``
    is a budget decision and says nothing about the builds it dropped. Reporting them as
    one number would turn "we could not afford to carry these" into "these were worse".
    """

    iterations: int
    entered: int
    measured: int
    pruned: int
    truncated: int
    survived: int
    leader: str | None
    #: True when nothing separated: every measured candidate tied with the leader.
    undivided: bool

    def to_json(self) -> dict:
        return {
            "iterations": self.iterations,
            "entered": self.entered,
            "measured": self.measured,
            "prunedOnEvidence": self.pruned,
            "truncatedForBudget": self.truncated,
            "survived": self.survived,
            "leader": self.leader,
            "fieldDidNotSeparate": self.undivided,
        }


def prune(measurements: Sequence[Measurement], keep: int) -> tuple[list[str], list[str], list[str]]:
    """``(survivors, pruned on evidence, truncated for budget)``, in that order.

    The tie rule decides the first two; only what is left over after it decides the
    third. Survivors come back ranked by DPS so a caller can report a leader, and the
    order is stable on the key for a tie so a re-run picks the same one.
    """
    if not measurements:
        return [], [], []
    ranked = sorted(measurements, key=lambda m: (-m.dps, m.key))
    leader = ranked[0]

    survivors = [m for m in ranked if not separated(leader, m)]
    pruned = [m.key for m in ranked if separated(leader, m)]

    carried = survivors[: max(1, keep)]
    truncated = [m.key for m in survivors[len(carried) :]]
    return [m.key for m in carried], pruned, truncated


# --------------------------------------------------------------------------------
# The mutations -- every one leaves the node set alone
# --------------------------------------------------------------------------------


def flippable(loadout: Loadout, nodes: dict[int, list[Trait]]) -> tuple[Selection, ...]:
    """Selected choice nodes whose entry can actually be changed.

    Three filters, and the middle one is the severe case ``talentedit`` documents: a
    **granted** node's wire record ends at the purchased bit, so the format has nowhere
    to put a choice index, and flipping one encodes to *the build you started from*.
    ``set_choice`` refuses it; asking for one here would raise mid-search, so they are
    not offered.
    """
    found: list[Selection] = []
    for selection in loadout.selections:
        entries = nodes.get(selection.node_id) or []
        if len(entries) < 2:
            continue
        if entries[0].node_type not in (talenttree.NODE_CHOICE, talenttree.NODE_SELECTION):
            continue
        if not selection.purchased:
            continue
        if selection.tree_index == talenttree.TREE_SELECTION:
            # The hero-tree selection node names which hero tree is played. Flipping it
            # is a hero swap, which moves fourteen points into a tree the build has not
            # spent them in -- a different edit with a different legality argument.
            continue
        found.append(selection)
    return tuple(sorted(found, key=lambda s: s.node_id))


def movable_ranks(loadout: Loadout, nodes: dict[int, list[Trait]]) -> tuple[tuple[int, int], ...]:
    """``(source, target)`` pairs a rank can move between without changing the node set.

    Both ends must already be selected, the source must keep at least one rank after
    giving one away, and the target must have room under its maximum. Those three are
    exactly what makes the move node-set-preserving: ``move_rank`` on its own will drop
    an emptied source and take an unselected target, both of which change the set and
    void the legality argument in the module docstring.
    """
    pairs: list[tuple[int, int]] = []
    for source in loadout.selections:
        if source.rank < 2 or not source.purchased:
            continue
        source_entries = nodes.get(source.node_id) or []
        if not source_entries:
            continue
        for target in loadout.selections:
            if target.node_id == source.node_id or not target.purchased:
                continue
            target_entries = nodes.get(target.node_id) or []
            if not target_entries:
                continue
            if source_entries[0].tree_index != target_entries[0].tree_index:
                continue
            if source.tree_index == talenttree.TREE_HERO and source.sub_tree != target.sub_tree:
                continue
            if target.rank >= talenttree.max_ranks_of(target_entries):
                continue
            pairs.append((source.node_id, target.node_id))
    return tuple(sorted(set(pairs)))


def scramble(loadout: Loadout, nodes: dict[int, list[Trait]], rng: random.Random) -> Loadout:
    """Overwrite every flippable choice with a random entry, keeping the node set.

    This is what makes a calibration *blind*: after it, the build carries none of the
    information simc's own build carried about which entries to take, in the one
    dimension the search moves. It is deterministic given ``rng``, so a calibration run
    is reproducible from its seed.
    """
    result = loadout
    for selection in flippable(loadout, nodes):
        entries = nodes[selection.node_id]
        result = talentedit.set_choice(
            result, nodes, selection.node_id, rng.randrange(len(entries))
        )
    return result


def neighbours(
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    rng: random.Random,
    count: int,
) -> list[tuple[Loadout, str]]:
    """Up to ``count`` distinct one-edit neighbours, each with a description.

    Distinct on the *selections*, not on the object: two different edits can land on
    one build (flipping a node back, moving a rank the way it came), and simulating the
    same build twice under two names spends a variant to learn nothing.
    """
    flips = flippable(loadout, nodes)
    moves = movable_ranks(loadout, nodes)
    if not flips and not moves:
        return []

    seen = {fingerprint(loadout)}
    found: list[tuple[Loadout, str]] = []
    # Bounded rather than while-True: with a small neighbourhood the sampler would
    # otherwise spin looking for a distinct build that does not exist.
    for _ in range(count * 12):
        if len(found) >= count:
            break
        use_flip = bool(flips) and (not moves or rng.random() < 0.7)
        try:
            if use_flip:
                selection = flips[rng.randrange(len(flips))]
                entries = nodes[selection.node_id]
                choices = [i for i in range(len(entries)) if i != selection.choice_index]
                if not choices:
                    continue
                index = choices[rng.randrange(len(choices))]
                mutant = talentedit.set_choice(loadout, nodes, selection.node_id, index)
                what = f"choice node {selection.node_id} -> entry index {index}"
            else:
                source, target = moves[rng.randrange(len(moves))]
                mutant = talentedit.move_rank(
                    loadout, nodes, source_node=source, target_node=target, ranks=1
                )
                what = f"rank {source} -> {target}"
        except talentedit.TalentEditError:
            # A neighbour the tree will not express is not an error in a search; it is
            # a neighbour that does not exist. Skipping keeps one refusal from ending
            # a run that has hundreds of other moves available.
            continue
        mark = fingerprint(mutant)
        if mark in seen:
            continue
        seen.add(mark)
        found.append((mutant, what))
    return found


def fingerprint(loadout: Loadout) -> tuple:
    """What makes two builds the same build: the selections, and nothing about framing."""
    return tuple(sorted((s.node_id, s.entry_id, s.rank, s.purchased) for s in loadout.selections))


# --------------------------------------------------------------------------------
# The search
# --------------------------------------------------------------------------------

#: Signature of the thing that measures a field. Taking it as an argument is what makes
#: every round of this module testable without simc: the tests drive real loadouts and
#: real encodings through the real pruning rule against a stub that returns numbers.
Runner = Callable[[Sequence[Candidate], int], dict[str, Measurement]]


@dataclass
class SearchOutcome:
    """What a search found, what it cost, and what it could not say."""

    spec_id: str
    build_id: str
    #: Best first. Empty when nothing could be measured at all.
    ranked: list[tuple[Candidate, Measurement]] = field(default_factory=list)
    rounds: list[RoundOutcome] = field(default_factory=list)
    seeds: dict[str, int] = field(default_factory=dict)
    variants_evaluated: int = 0
    seed_value: int | None = None
    blind: bool = False
    notes: list[str] = field(default_factory=list)
    #: How many rounds the opening climb ran, and at what precision. Published in
    #: ``method`` because a run that took nine steps and one that took none are very
    #: different runs and the halving rounds look identical either way.
    climb_rounds: int = 0
    climb_iterations: int = 0

    @property
    def best(self) -> tuple[Candidate, Measurement] | None:
        return self.ranked[0] if self.ranked else None

    @property
    def runner_up(self) -> tuple[Candidate, Measurement] | None:
        return self.ranked[1] if len(self.ranked) > 1 else None

    @property
    def settled(self) -> bool:
        """True when the winner separated from the runner-up. False is not a failure --
        it is the finding that the field did not divide, and it belongs in the caveats
        rather than being resolved by taking the larger number."""
        best, second = self.best, self.runner_up
        if best is None:
            return False
        if second is None:
            return True
        return separated(best[1], second[1])

    def method(self) -> str:
        """How the winner was found, in one line a reader could reproduce from.

        **The climb is named.** An earlier version reported only the halving rounds --
        "2 round(s) from 1200 to 3000" -- which omits the phase that does most of the
        work on a blind run and would leave anyone trying to reproduce the result
        wondering how a build nine edits from its seed was reached.
        """
        opening = (
            f"a {self.climb_rounds}-round climb at {self.climb_iterations} iterations, then "
            if self.climb_rounds
            else ""
        )
        low = self.rounds[0].iterations if self.rounds else 0
        high = self.rounds[-1].iterations if self.rounds else 0
        return (
            f"Node-set-preserving talent edits: {opening}"
            f"{len(self.rounds)} halving round(s) from {low} to {high} deterministic "
            f"iterations, pruning on the tie rule"
        )

    def description(self) -> str:
        sources = ", ".join(f"{count} from {name}" for name, count in sorted(self.seeds.items()))
        blind = (
            "; blind -- every choice node was scrambled before the first round, so the "
            "search began with none of simc's own choices"
            if self.blind
            else ""
        )
        return (
            f"{self.variants_evaluated} variant(s) evaluated from {sum(self.seeds.values())} "
            f"seed(s) ({sources}){blind}"
        )

    def caveats(self) -> list[str]:
        notes = [
            "The search varies which entry each choice node takes and how ranks are "
            "distributed among the nodes the seed already takes. It never adds or drops "
            "a node, so it cannot find that a different *set* of talents is better -- "
            "this is the best build in that neighbourhood, not the best build for the "
            "spec.",
            "Unlock edges are not in simc's data and were not checked. They do not need "
            "to be: every candidate takes exactly the nodes its seed took, so any edge "
            "the seed satisfied the candidate satisfies too. The claim is only as good "
            "as the seed's own legality.",
        ]
        if not self.settled and self.runner_up is not None:
            best, second = self.ranked[0][1], self.ranked[1][1]
            margin = (best.dps / second.dps - 1) * 100 if second.dps else 0.0
            band = tie_band(best.dps_error, second.dps_error) * 100
            notes.append(
                f"The winner did not separate from the runner-up: {margin:+.2f}% against "
                f"a {band:.2f}% tie band. Both are published for that reason."
            )
        notes.extend(self.notes)
        return notes


def search(
    *,
    spec_id: str,
    build_id: str,
    seeds: Sequence[Candidate],
    nodes: dict[int, list[Trait]],
    runner: Runner,
    breadth: int = 24,
    seed_value: int = 0,
    blind: bool = False,
    rounds: Sequence[Round] | None = None,
    climb_steps: int = CLIMB_STEPS,
) -> SearchOutcome:
    """Run one build's search and return everything it can honestly say.

    ``seeds`` are the starting builds -- simc's own where one exists and the run is not
    blind, harvested ones where a document was supplied, and the repaired hash for a
    build simc refuses. ``breadth`` is how many neighbours are generated per seed for
    the opening round.

    ``climb_steps`` bounds how far the search can travel from its seed. **This is the
    thing the first version of this module got wrong, and the calibration caught it.**

    The first version generated the whole field once, around the seeds, and never
    again. For a run seeded with simc's build that is defensible -- the answer is one
    edit away or it is not. For a *blind* run it is fatal, and structurally rather than
    statistically: a scrambled seed has a median of nine choice nodes set at random, and
    a one-shot single-edit neighbourhood can correct exactly **one** of them. The search
    could not reach simc's build from a blind start however many variants it measured,
    and the blind calibration duly reported it 3-4% behind -- a measurement of the
    algorithm's reach, not of the method.

    So the opening rounds climb: measure the leader's whole one-edit neighbourhood, take
    the winner, repeat. The stopping rule is the tie rule again, not a fixed number of
    steps -- a step is only taken when the new leader **separates** from the old one, so
    the climb cannot walk uphill on noise. ``climb_steps`` is the ceiling on how many
    real improvements it will chase.
    """
    if not seeds:
        raise SearchError(f"{build_id}: a search needs at least one seed")

    rng = random.Random(seed_value)
    outcome = SearchOutcome(spec_id=spec_id, build_id=build_id, seed_value=seed_value, blind=blind)
    for seed in seeds:
        outcome.seeds[seed.origin] = outcome.seeds.get(seed.origin, 0) + 1

    field_by_key: dict[str, Candidate] = {}
    for seed in seeds:
        field_by_key[seed.key] = seed
        for index, (mutant, what) in enumerate(neighbours(seed.loadout, nodes, rng, breadth)):
            key = f"{seed.key}_v{index:03d}"
            try:
                talent_hash = talenttree.encode_loadout(mutant, nodes, preserve_framing=False)
            except talenttree.TalentEncodeError as error:
                # Nothing here should produce one -- the mutations are bounded -- so it
                # is worth a line rather than a silent skip.
                log.warning("%s: %s is unencodable (%s)", build_id, key, error)
                continue
            field_by_key[key] = Candidate(
                key=key,
                label=f"{seed.label} + {what}",
                origin=ORIGIN_SEARCH,
                loadout=mutant,
                talent_hash=talent_hash,
                lineage=(*seed.lineage, what),
                parent=seed.key,
            )

    schedule = tuple(rounds) if rounds else plan_rounds(len(field_by_key))
    alive: list[str] = list(field_by_key)

    if climb_steps > 0 and schedule:
        survivors = _climb(
            outcome,
            field_by_key,
            nodes,
            runner,
            rng,
            iterations=schedule[0].iterations,
            breadth=breadth,
            steps=climb_steps,
        )
        if survivors:
            # Two things had to move together here, and both were wrong in a way that
            # is invisible from any single round's output.
            #
            # **The climb's survivors, not everything it visited.** The climb measures
            # each step's whole neighbourhood at the opening precision; carrying all of
            # it forward means the first halving round re-measures the same builds at
            # the same precision, which is deterministic and therefore returns the same
            # numbers for nothing. What has to survive is what the *tie rule* kept --
            # which is everything that could still win, so no leader is being trusted
            # at 300 iterations either.
            #
            # **And the schedule has to be planned after the climb, not before.** It was
            # planned from the pre-climb field, so ``keep`` was half of (say) 10 while
            # the field entering the round was 200 -- a 95% truncation reported honestly
            # as "dropped for budget" and meaning the halving had stopped being a
            # halving of anything.
            alive = survivors
            if not rounds:
                # Clamped, because the climb may already have run at the final
                # precision: ``plan_rounds`` gives a field of two a single round at
                # ``FINAL_ITERATIONS``, and quadrupling that asks for a schedule that
                # starts above where it ends. Measured on Frost Death Knight, which has
                # exactly one flippable choice node and therefore a field of two --
                # ``iteration schedule must rise: start 12000, final 3000``, on the
                # first build of a real calibration.
                nxt = min(schedule[0].iterations * ITERATION_FACTOR, FINAL_ITERATIONS)
                schedule = plan_rounds(len(alive), start=nxt)
            elif len(schedule) > 1:
                schedule = schedule[1:]

    measured_last: dict[str, Measurement] = {}

    for step in schedule:
        entering = list(alive)
        candidates = [field_by_key[key] for key in entering]
        measured = _measure_in_batches(runner, candidates, step.iterations)
        rows = [measured[key] for key in entering if key in measured]
        if not rows:
            outcome.rounds.append(
                RoundOutcome(
                    iterations=step.iterations,
                    entered=len(entering),
                    measured=0,
                    pruned=0,
                    truncated=0,
                    survived=0,
                    leader=None,
                    undivided=False,
                )
            )
            outcome.notes.append(
                f"No variant returned a result at {step.iterations} iterations; the "
                f"search stopped there."
            )
            alive = []
            break

        outcome.variants_evaluated += len(rows)
        survivors, pruned, truncated = prune(rows, step.keep)
        outcome.rounds.append(
            RoundOutcome(
                iterations=step.iterations,
                entered=len(entering),
                measured=len(rows),
                pruned=len(pruned),
                truncated=len(truncated),
                survived=len(survivors),
                leader=survivors[0] if survivors else None,
                undivided=not pruned and len(rows) > 1,
            )
        )
        if truncated:
            outcome.notes.append(
                f"At {step.iterations} iterations {len(truncated)} variant(s) tied with "
                f"the leader and were dropped to fit the round's budget, not because "
                f"they measured worse."
            )
        # Everything measured, not only the survivors: the last round has no next
        # round to protect, so pruning there would discard the runner-up that makes a
        # near-tie visible. Intermediate rounds still carry survivors only.
        measured_last = {row.key: row for row in rows}
        alive = survivors

    outcome.ranked = sorted(
        ((field_by_key[key], row) for key, row in measured_last.items()),
        key=lambda pair: (-pair[1].dps, pair[0].key),
    )[:FINAL_KEEP]
    return outcome


def _climb(
    outcome: SearchOutcome,
    field: dict[str, Candidate],
    nodes: dict[int, list[Trait]],
    runner: Runner,
    rng: random.Random,
    *,
    iterations: int,
    breadth: int,
    steps: int,
) -> list[str]:
    """Steepest ascent over one-edit neighbourhoods, at the opening precision.

    Cheap where it needs to be and honest about when to stop. ``breadth`` at its default
    covers a whole neighbourhood -- MID2's widest build offers 16 flippable choice nodes
    and 4 rank-move pairs -- so this is steepest ascent rather than a random walk.

    The opening precision is the right one for it: a scrambled choice is worth 3-15% on
    the builds measured here and 300 iterations resolves ~1%, so the climb is deciding
    questions far larger than its own error. The steps it *cannot* decide are exactly
    the ones the tie rule refuses to take, and those are what the halving rounds below
    are for.

    Mutates ``field`` and ``outcome`` in place and returns the keys the halving rounds
    should carry: the **tie-rule survivors** of the last measurement it took. That is
    everything the opening precision cannot separate from the leader, so nothing is
    being trusted at 300 iterations -- and it is not the whole path, because
    re-measuring a deterministic run at the precision it already ran at returns the same
    numbers for nothing.
    """
    outcome.climb_iterations = iterations
    measured = _measure_in_batches(runner, list(field.values()), iterations)
    rows = list(measured.values())
    if not rows:
        return []
    outcome.climb_rounds += 1
    outcome.variants_evaluated += len(rows)
    leader_key = min(rows, key=lambda m: (-m.dps, m.key)).key
    leader = measured[leader_key]
    last_round = rows

    for step in range(steps):
        challengers: list[Candidate] = []
        base = field[leader_key]
        for index, (mutant, what) in enumerate(neighbours(base.loadout, nodes, rng, breadth)):
            key = f"c{step:02d}_{index:03d}"
            try:
                talent_hash = talenttree.encode_loadout(mutant, nodes, preserve_framing=False)
            except talenttree.TalentEncodeError as error:
                log.warning(
                    "%s: climb variant %s is unencodable (%s)", outcome.build_id, key, error
                )
                continue
            challengers.append(
                Candidate(
                    key=key,
                    label=f"{base.label} + {what}",
                    origin=ORIGIN_SEARCH,
                    loadout=mutant,
                    talent_hash=talent_hash,
                    lineage=(*base.lineage, what),
                    parent=base.key,
                )
            )
        if not challengers:
            break

        got = _measure_in_batches(runner, challengers, iterations)
        if not got:
            break
        outcome.climb_rounds += 1
        outcome.variants_evaluated += len(got)
        for candidate in challengers:
            field[candidate.key] = candidate
        # The leader is in the running too: a step that improves on nothing must leave
        # the leader as a survivor rather than dropping it for its own challengers.
        last_round = [*got.values(), leader]
        best = min(got.values(), key=lambda m: (-m.dps, m.key))
        # The tie rule decides whether a step was real. Without it the climb walks
        # uphill on Monte Carlo noise and reports the walk as a finding.
        if not separated(best, leader):
            outcome.notes.append(
                f"The climb stopped after {step} step(s): no neighbour separated from "
                f"the leader at {iterations} iterations."
            )
            break
        leader_key, leader = best.key, best
    else:
        outcome.notes.append(
            f"The climb used all {steps} of its steps, so a longer climb might have "
            f"found more. The step limit bounds how far this search travelled."
        )

    survivors, _, _ = prune(last_round, MAX_VARIANTS_PER_ROUND)
    return survivors


def _measure_in_batches(
    runner: Runner, candidates: Sequence[Candidate], iterations: int
) -> dict[str, Measurement]:
    """Hand the runner at most ``MAX_VARIANTS_PER_ROUND`` variants at a time."""
    measured: dict[str, Measurement] = {}
    for start in range(0, len(candidates), MAX_VARIANTS_PER_ROUND):
        batch = list(candidates[start : start + MAX_VARIANTS_PER_ROUND])
        if batch:
            measured.update(runner(batch, iterations))
    return measured


def seed_from(
    *,
    key: str,
    label: str,
    origin: str,
    loadout: Loadout,
    nodes: dict[int, list[Trait]],
    rng: random.Random | None = None,
) -> Candidate:
    """A seed candidate, scrambled first when ``rng`` is given.

    Scrambling belongs here rather than in ``search`` so that a caller cannot ask for a
    blind run and be handed simc's own choices anyway: a blind seed is a *different
    build*, and building it is the caller's single decision.
    """
    build = scramble(loadout, nodes, rng) if rng is not None else loadout
    return Candidate(
        key=key,
        label=label,
        origin=origin,
        loadout=build,
        talent_hash=talenttree.encode_loadout(build, nodes, preserve_framing=False),
        lineage=("scrambled",) if rng is not None else (),
    )


def with_hash(candidate: Candidate, nodes: dict[int, list[Trait]]) -> Candidate:
    """Re-derive a candidate's hash from its loadout. Used after a repair edits one."""
    return replace(
        candidate,
        talent_hash=talenttree.encode_loadout(candidate.loadout, nodes, preserve_framing=False),
    )


# --------------------------------------------------------------------------------
# Running it against simc
# --------------------------------------------------------------------------------


def simc_runner(
    simc: Path,
    request: SimRequest,
    settings: SimSettings,
    anchor_options: Sequence[str],
    timeout: int = 3600,
    base_talents: str | None = None,
) -> Runner:
    """A ``Runner`` that measures a field as profilesets in one simc invocation.

    Four things in it are the project's rules rather than choices, and getting any of
    the first three wrong produces plausible numbers that are wrong by about a tenth of
    a percent -- which is the same size as the differences being measured:

    * **profileset against profileset, never against the base actor.** The base actor
      runs a different iteration count and lands ~0.09% away from an identical
      profileset. Nothing here reads it.
    * **``profileset_work_threads=1``**, set inside ``run_profilesets``. Without it each
      variant silently runs at ``iterations / threads``.
    * **the anchor's options ride on every variant**, ahead of the talents. Two
      candidates then differ in ``talents=`` and in nothing else, which is the whole
      claim a talent search makes.
    * **``base_talents`` must be a hash simc will load**, and this one is not about
      precision -- it is about the run happening at all.

    The fourth is the one a real run found and no test could. Nothing *reads* the base
    actor, but simc still **builds** it, from the profile file, before it generates a
    single profileset -- and for the four specs this feature exists for, the profile's
    own hash is exactly the one simc refuses. Measured on the tier-wide calibration:

        Error: Initialization error: Player 'MID2_Demon_Hunter_Havoc_Fel-Scarred':
        Hash '...': Node 91024 is not a choice node but has index selection.

    simc exits 81 and the whole invocation dies, taking every profileset in it with it.
    A repaired hash verified by hand on the command line (``simc PROFILE talents=HASH``,
    which overrides the profile) therefore proves nothing about whether the *pipeline*
    can run it, because the pipeline reaches simc by a different route. Passing the
    repaired hash as a base-actor option is what makes the profilesets reachable; each
    one still sets its own ``talents=`` and so overrides it.
    """
    from . import simc_runner as runner_module
    from .scenarios import SimSettings as Settings

    # Ahead of the profileset lines, so the base actor is built from a hash simc
    # accepts. Options after the profile path override the profile -- see
    # ``simc_runner.build_command``.
    base = (f"talents={base_talents}",) if base_talents else ()

    def run(candidates: Sequence[Candidate], iterations: int) -> dict[str, Measurement]:
        sets = [
            runner_module.Profileset(
                key=candidate.key,
                options=(*anchor_options, f"talents={candidate.talent_hash}"),
            )
            for candidate in candidates
        ]
        measured = runner_module.run_profilesets(
            simc,
            request,
            Settings(
                target_error=0.0,
                max_iterations=iterations,
                threads=settings.threads,
                extra_options=(*settings.extra_options, *base),
            ),
            sets,
            timeout=timeout,
        )
        return {
            key: Measurement(
                key=key,
                dps=result.dps,
                dps_error=result.dps_error,
                iterations=result.iterations,
                priority_dps=result.priority_dps,
            )
            for key, result in measured.items()
        }

    return run
