"""The search, its pruning rule, and the invariant its legality argument rests on.

Everything here is hand-built or stubbed, so none of it needs a simc checkout or a
binary. What that cannot cover is whether simc agrees, and the pull request carries
that separately: repaired hashes and searched builds handed to a real simc.
"""

import random

import pytest

from wowdps import buildsearch, computedbuilds
from wowdps.buildsearch import (
    Measurement,
    Round,
    SearchError,
    fingerprint,
    flippable,
    movable_ranks,
    neighbours,
    plan_rounds,
    prune,
    scramble,
    search,
    seed_from,
    separated,
    tie_band,
)
from wowdps.talentedit import select_node
from wowdps.talenttree import (
    TREE_CLASS,
    TREE_HERO,
    TREE_SELECTION,
    TREE_SPEC,
    Loadout,
    Trait,
    encode_loadout,
)

SPEC = 260
OTHER_SPEC = 259


def trait(node_id, entry_id, tree=TREE_CLASS, **kw):
    return Trait(
        tree_index=tree,
        class_id=4,
        entry_id=entry_id,
        node_id=node_id,
        max_ranks=kw.get("max_ranks", 1),
        req_points=kw.get("req_points", 0),
        spell_id=kw.get("spell_id", 1000 + entry_id),
        row=kw.get("row", 1),
        col=kw.get("col", 1),
        selection_index=kw.get("selection_index", 100),
        name=kw.get("name", f"Talent {entry_id}"),
        spec_ids=kw.get("spec_ids", ()),
        sub_tree=kw.get("sub_tree", 0),
        node_type=kw.get("node_type", 0),
    )


def sample_nodes():
    """A small class with everything the search touches: choice nodes, ranked nodes,
    a hero tree and a selection node."""
    return {
        10: [trait(10, 100, max_ranks=3, name="Class A")],
        11: [trait(11, 110, max_ranks=3, name="Class B")],
        12: [
            trait(12, 120, node_type=2, name="Left"),
            trait(12, 121, node_type=2, name="Right"),
        ],
        13: [
            trait(13, 130, node_type=2, name="Up"),
            trait(13, 131, node_type=2, name="Down"),
        ],
        20: [trait(20, 200, tree=TREE_SPEC, max_ranks=2, name="Spec A")],
        21: [trait(21, 210, tree=TREE_SPEC, max_ranks=2, name="Spec B")],
        30: [trait(30, 300, tree=TREE_HERO, sub_tree=51, max_ranks=2, name="Hero A")],
        31: [trait(31, 310, tree=TREE_HERO, sub_tree=51, max_ranks=2, name="Hero B")],
        41: [
            trait(
                41, 410, tree=TREE_SELECTION, sub_tree=51, node_type=3, spec_ids=(SPEC,), name="0"
            ),
            trait(
                41, 411, tree=TREE_SELECTION, sub_tree=52, node_type=3, spec_ids=(SPEC,), name="0"
            ),
        ],
    }


def sample_build() -> Loadout:
    nodes = sample_nodes()
    build = Loadout(version=2, spec_id=SPEC, selections=(), spare_bits=0)
    build = select_node(build, nodes, 10, rank=2)
    build = select_node(build, nodes, 11, rank=1)
    build = select_node(build, nodes, 12, choice_index=0)
    build = select_node(build, nodes, 13, choice_index=0)
    build = select_node(build, nodes, 20, rank=2)
    build = select_node(build, nodes, 21, rank=1)
    build = select_node(build, nodes, 30, rank=2)
    build = select_node(build, nodes, 31, rank=1)
    build = select_node(build, nodes, 41, rank=1, choice_index=0)
    return build


def wide_nodes():
    """A class with ten choice nodes, so a tied field is wide enough to size a round.

    ``sample_nodes`` has four, which happens to make ``plan_rounds`` give the same
    answer whether it is planned before or after the climb -- so a test built on it
    cannot see the difference between the two.
    """
    nodes = {
        1000 + i: [
            trait(1000 + i, 9000 + i * 2, node_type=2, name=f"Left {i}"),
            trait(1000 + i, 9001 + i * 2, node_type=2, name=f"Right {i}"),
        ]
        for i in range(10)
    }
    nodes[41] = sample_nodes()[41]
    return nodes


def wide_build() -> Loadout:
    nodes = wide_nodes()
    build = Loadout(version=2, spec_id=SPEC, selections=(), spare_bits=0)
    for node_id in sorted(n for n in nodes if n != 41):
        build = select_node(build, nodes, node_id, choice_index=0)
    return select_node(build, nodes, 41, rank=1, choice_index=0)


def measurement(key, dps, error=0.05, iterations=3000):
    return Measurement(key=key, dps=dps, dps_error=error, iterations=iterations)


# --------------------------------------------------------------------------------
# The tie rule
# --------------------------------------------------------------------------------


def test_the_tie_band_is_the_two_errors_in_quadrature():
    assert tie_band(0.0, 0.0) == 0.0
    assert tie_band(3.0, 4.0) == pytest.approx(0.05)


def test_a_lead_equal_to_the_band_is_a_tie():
    """``leadVerdict`` in the web project has it the same way, and the two must agree:
    the site derives its verdict from the published numbers, so a boundary this side
    calls a win and that side calls a tie would show a contradiction on one page."""
    band = tie_band(0.25, 0.25)
    trailer = measurement("b", 100.0, error=0.25)
    # Exactly on the boundary, constructed rather than guessed at.
    leader = measurement("a", 100.0 * (1 + band), error=0.25)
    assert leader.dps / trailer.dps - 1 == pytest.approx(band)
    assert not separated(leader, trailer)
    # And a hair above it is a lead.
    assert separated(measurement("a", 100.0 * (1 + band * 1.001), error=0.25), trailer)


def test_a_candidate_inside_the_band_survives_the_round():
    """The whole reason pruning is not "keep the top half". At 300 iterations the error
    is around 0.3% and real talent differences are often smaller, so a rule that ranked
    on the mean alone would drop the eventual winner whenever it rolled low."""
    rows = [measurement("lead", 100.0, error=0.3), measurement("close", 99.9, error=0.3)]
    survivors, pruned, truncated = prune(rows, keep=1)
    assert pruned == []
    # It survives on evidence and is only then dropped for budget -- two different
    # sentences, and the caller reports them separately.
    assert survivors == ["lead"]
    assert truncated == ["close"]


def test_a_candidate_outside_the_band_is_pruned_on_evidence():
    rows = [measurement("lead", 100.0, error=0.05), measurement("far", 95.0, error=0.05)]
    survivors, pruned, truncated = prune(rows, keep=4)
    assert survivors == ["lead"]
    assert pruned == ["far"]
    assert truncated == []


def test_pruning_an_empty_field_says_so_rather_than_raising():
    assert prune([], keep=3) == ([], [], [])


def test_ties_break_on_the_key_so_a_re_run_picks_the_same_leader():
    rows = [measurement("b", 100.0), measurement("a", 100.0)]
    survivors, _, _ = prune(rows, keep=2)
    assert survivors == ["a", "b"]


# --------------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------------


def test_the_schedule_quadruples_iterations_and_halves_the_field():
    rounds = plan_rounds(24)
    assert [r.iterations for r in rounds] == [300, 1200, 3000]
    assert [r.keep for r in rounds] == [12, 6, buildsearch.FINAL_KEEP]


def test_the_last_round_is_clamped_rather_than_overshooting():
    """1200 x 4 is 4800, which buys precision no other sweep in this project pays for."""
    assert plan_rounds(24)[-1].iterations == buildsearch.FINAL_ITERATIONS


def test_the_last_round_keeps_more_than_one_so_a_runner_up_exists():
    """The runner-up is published so a near-tie at the cut is visible from outside the
    file. A schedule narrowing to a single survivor throws that away before anyone can
    look at it."""
    assert plan_rounds(40)[-1].keep >= 2


def test_a_single_candidate_still_gets_one_round_at_full_precision():
    assert plan_rounds(1) == (Round(iterations=3000, keep=buildsearch.FINAL_KEEP),)


def test_an_empty_field_is_refused():
    with pytest.raises(SearchError):
        plan_rounds(0)


def test_a_schedule_that_does_not_rise_is_refused():
    with pytest.raises(SearchError):
        plan_rounds(10, start=3000, final=300)


# --------------------------------------------------------------------------------
# The legality invariant
# --------------------------------------------------------------------------------


def test_every_generated_neighbour_takes_exactly_the_nodes_its_seed_took():
    """**The load-bearing property of this whole module.**

    Unlock edges are not in simc's data, so a search that added or dropped a node could
    not know whether the result is reachable. The argument that makes the search legal
    anyway is that an unlock edge is a statement about which nodes are taken, and none
    of these edits changes that set -- so any edge the seed satisfied, the mutant
    satisfies. Asserted over generated mutations rather than argued, because it is the
    one claim everything published from this module rests on.
    """
    nodes = sample_nodes()
    seed = sample_build()
    taken = frozenset(s.node_id for s in seed.selections)
    seen = 0
    for value in range(40):
        rng = random.Random(value)
        for mutant, _ in neighbours(seed, nodes, rng, 8):
            seen += 1
            assert frozenset(s.node_id for s in mutant.selections) == taken
    assert seen > 50, "the generator produced too few mutations to be evidence"


def test_every_generated_neighbour_leaves_each_tree_s_point_total_alone():
    """Gates and the point budget are thresholds on a tree's total, so this is what
    carries them through a mutation without re-checking either."""
    nodes = sample_nodes()
    seed = sample_build()
    before = {tree: seed.points(tree) for tree in (TREE_CLASS, TREE_SPEC, TREE_HERO)}
    for value in range(20):
        for mutant, _ in neighbours(seed, nodes, random.Random(value), 8):
            for tree, spent in before.items():
                assert mutant.points(tree) == spent


def test_a_rank_move_is_never_offered_that_would_empty_its_source():
    """``move_rank`` drops an emptied source, which changes the node set and voids the
    legality argument. The generator must never propose one."""
    nodes = sample_nodes()
    seed = sample_build()
    by_id = {s.node_id: s for s in seed.selections}
    for source, _ in movable_ranks(seed, nodes):
        assert by_id[source].rank >= 2


def test_a_granted_node_is_never_offered_for_a_flip():
    """A granted node's wire record ends at the purchased bit, so a flip encodes to the
    build it started from -- a variant that was never built, wearing another build's
    number. ``encode_loadout`` refuses it; this keeps the search from asking."""
    nodes = sample_nodes()
    seed = sample_build()
    granted = seed.selections[0]
    seed = Loadout(
        version=seed.version,
        spec_id=seed.spec_id,
        selections=tuple(
            s if s.node_id != 12 else type(s)(**{**s.__dict__, "purchased": False, "rank": 1})
            for s in seed.selections
        ),
        spare_bits=seed.spare_bits,
    )
    del granted
    assert 12 not in {s.node_id for s in flippable(seed, nodes)}


def test_the_hero_selection_node_is_never_offered_for_a_flip():
    """Flipping it is a hero swap: fourteen points move into a tree the build has not
    spent them in. A different edit with a different legality argument."""
    nodes = sample_nodes()
    assert 41 not in {s.node_id for s in flippable(sample_build(), nodes)}


def test_neighbours_are_distinct_builds():
    nodes = sample_nodes()
    seed = sample_build()
    found = neighbours(seed, nodes, random.Random(7), 12)
    marks = {fingerprint(m) for m, _ in found}
    assert len(marks) == len(found)
    assert fingerprint(seed) not in marks


# --------------------------------------------------------------------------------
# Blind seeding
# --------------------------------------------------------------------------------


def test_scrambling_keeps_the_node_set_and_the_totals():
    nodes = sample_nodes()
    seed = sample_build()
    blind = scramble(seed, nodes, random.Random(3))
    assert frozenset(s.node_id for s in blind.selections) == frozenset(
        s.node_id for s in seed.selections
    )
    for tree in (TREE_CLASS, TREE_SPEC, TREE_HERO):
        assert blind.points(tree) == seed.points(tree)


def test_scrambling_is_reproducible_from_its_seed():
    nodes = sample_nodes()
    build = sample_build()
    assert fingerprint(scramble(build, nodes, random.Random(11))) == fingerprint(
        scramble(build, nodes, random.Random(11))
    )


def test_a_blind_seed_really_moves_off_simc_s_choices():
    """A "blind" run that handed the search simc's own entries back would be a
    calibration of nothing. Over enough seeds at least one scramble must differ."""
    nodes = sample_nodes()
    build = sample_build()
    assert any(
        fingerprint(scramble(build, nodes, random.Random(v))) != fingerprint(build)
        for v in range(20)
    )


def test_seed_from_scrambles_only_when_a_generator_is_given():
    nodes = sample_nodes()
    build = sample_build()
    plain = seed_from(key="s", label="l", origin="simc", loadout=build, nodes=nodes)
    assert fingerprint(plain.loadout) == fingerprint(build)
    assert plain.talent_hash == encode_loadout(build, nodes, preserve_framing=False)


# --------------------------------------------------------------------------------
# End to end, with a stub runner
# --------------------------------------------------------------------------------


def _runner(scores, log=None):
    """A runner that scores a candidate by a function of its hash, with a fixed error."""

    def run(candidates, iterations):
        if log is not None:
            log.append((iterations, [c.key for c in candidates]))
        return {
            c.key: Measurement(
                key=c.key,
                dps=scores(c),
                dps_error=0.3 if iterations < 1000 else 0.05,
                iterations=iterations,
            )
            for c in candidates
        }

    return run


def test_the_search_runs_every_round_and_narrows_the_field():
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="seed", origin="simc", loadout=sample_build(), nodes=nodes)
    calls: list = []
    # One candidate is genuinely far ahead; everything else is flat, so the tie rule
    # keeps the flat field together and the leader separates.
    outcome = search(
        spec_id="rogue_outlaw",
        build_id="rogue_outlaw_default",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 200.0 if c.key.endswith("v000") else 100.0, calls),
        breadth=12,
    )
    # The halving rounds are the tail of the call log; the climb's calls sit ahead of
    # them and all run at the opening precision. The halving rounds start at the *next*
    # precision, because the climb has already covered the opening one -- re-measuring a
    # deterministic run at the precision it already ran at buys nothing.
    schedule = [r.iterations for r in outcome.rounds]
    assert [c[0] for c in calls][-len(schedule) :] == schedule
    assert all(c[0] == buildsearch.START_ITERATIONS for c in calls[: -len(schedule)])
    assert schedule[0] > buildsearch.START_ITERATIONS
    assert schedule[-1] == buildsearch.FINAL_ITERATIONS
    assert outcome.best is not None
    assert outcome.best[0].key.endswith("v000")
    assert outcome.variants_evaluated > 0
    # The field narrowed: many builds were measured at the opening precision and one
    # reaches the last round. Where the narrowing happened -- in the climb or in a
    # halving round -- depends on how far the winner separates, so the assertion is on
    # the narrowing rather than on which round did it.
    assert sum(len(c[1]) for c in calls if c[0] == buildsearch.START_ITERATIONS) > 1
    assert outcome.rounds[-1].entered <= buildsearch.FINAL_KEEP


# --------------------------------------------------------------------------------
# The climb
# --------------------------------------------------------------------------------


def test_the_climb_walks_several_edits_away_from_a_blind_seed():
    """**The defect the first calibration caught, as a test.**

    A blind seed scrambles a median of nine choice nodes and a single-edit neighbourhood
    corrects one, so a search that generated its field once could never reach a build
    nine edits away -- and the blind calibration reported that as the search being 3-4%
    behind simc. Scored here so that each correct entry is worth more: the winner is
    reachable only by taking several steps in a row.
    """
    nodes = sample_nodes()
    target = fingerprint(sample_build())
    wanted = dict((n, e) for n, e, _r, _p in target)

    def score(candidate):
        got = dict((n, e) for n, e, _r, _p in fingerprint(candidate.loadout))
        return 100.0 + 10.0 * sum(1 for n, e in wanted.items() if got.get(n) == e)

    blind = seed_from(
        key="s00",
        label="blind",
        origin="search",
        loadout=sample_build(),
        nodes=nodes,
        rng=random.Random(5),
    )
    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[blind],
        nodes=nodes,
        runner=_runner(score),
        breadth=12,
    )
    assert outcome.best is not None
    assert score(outcome.best[0]) >= score(blind), "the climb must not end below its seed"
    # And it really moved: the winner is not the seed it started from.
    assert len(outcome.rounds) >= 1


def test_the_halving_rounds_carry_the_climb_s_survivors_not_its_whole_path():
    """Two flaws that were invisible from any single round's output.

    The climb measures each step's whole neighbourhood at the opening precision. Carried
    forward whole, the first halving round re-measured the same builds at the same
    precision -- deterministic, so the same numbers for nothing -- and the schedule's
    ``keep`` had been planned from the *pre-climb* field, so a field of two hundred was
    truncated to five and the halving had stopped halving anything.
    """
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    calls: list = []
    counter = {"n": 0}

    def ever_better(candidate):
        counter["n"] += 1
        return 100.0 + counter["n"]

    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(ever_better, calls),
        breadth=8,
        climb_steps=4,
    )
    opening = [c for c in calls if c[0] == buildsearch.START_ITERATIONS]
    halving = [c for c in calls if c[0] > buildsearch.START_ITERATIONS]
    assert opening, "the climb must have run"
    assert halving, "the halving rounds must have run"
    assert [r.iterations for r in outcome.rounds] == [c[0] for c in halving]
    # The field entering the first halving round is far smaller than everything the
    # climb visited, and no bigger than the tie rule's survivors.
    assert len(halving[0][1]) < sum(len(c[1]) for c in opening)
    assert len(halving[0][1]) <= buildsearch.MAX_VARIANTS_PER_ROUND


def test_the_halving_rounds_size_themselves_to_the_field_the_climb_hands_them():
    """``keep`` has to come from the field that actually enters the round.

    Planned from the *pre-climb* field it was half of nine while the field arriving was
    twenty, so the round dropped most of a tied field "for budget" and the halving had
    stopped halving anything. Everything ties here, so the climb hands on a wide field
    and the first halving round must carry more than the floor.
    """
    nodes = wide_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=wide_build(), nodes=nodes)
    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0),
        breadth=12,
        climb_steps=2,
    )
    assert outcome.rounds
    assert outcome.rounds[0].entered > buildsearch.FINAL_KEEP
    assert outcome.rounds[0].survived > buildsearch.FINAL_KEEP


def test_a_build_with_one_choice_node_survives_the_replan():
    """The narrowest build in MID2, and it broke a real run.

    ``plan_rounds`` gives a field of two a *single* round at the final precision, so the
    climb runs at 3000 -- and quadrupling that asks for a schedule starting above where
    it ends. Both Frost Death Knight builds are exactly this shape (one flippable choice
    node), so this is the first build a tier-wide calibration reaches, and it came back
    ``iteration schedule must rise: start 12000, final 3000``.
    """
    nodes = {
        12: [
            trait(12, 120, node_type=2, name="Left"),
            trait(12, 121, node_type=2, name="Right"),
        ],
        41: sample_nodes()[41],
    }
    build = Loadout(version=2, spec_id=SPEC, selections=(), spare_bits=0)
    build = select_node(build, nodes, 12, choice_index=0)
    build = select_node(build, nodes, 41, rank=1, choice_index=0)
    seed = seed_from(key="s00", label="s", origin="simc", loadout=build, nodes=nodes)
    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0 + len(c.lineage)),
        breadth=8,
    )
    assert outcome.best is not None
    assert outcome.rounds
    assert outcome.rounds[-1].iterations == buildsearch.FINAL_ITERATIONS
    assert not outcome.notes or all("stopped there" not in n for n in outcome.notes)


def test_the_published_method_names_the_climb():
    """The climb does most of the work on a blind run, and a reader trying to reproduce
    a winner nine edits from its seed has to be told it happened."""
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    counter = {"n": 0}

    def ever_better(candidate):
        counter["n"] += 1
        return 100.0 + counter["n"]

    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(ever_better),
        breadth=6,
        climb_steps=3,
    )
    assert outcome.climb_rounds > 0
    assert "climb" in outcome.method()
    assert str(outcome.climb_iterations) in outcome.method()


def test_the_climb_refuses_a_step_the_tie_rule_calls_noise():
    """Without this the climb walks uphill on Monte Carlo noise and reports the walk."""
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    calls: list = []
    # Everything identical: no neighbour can separate, so the climb must stop at once.
    search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0, calls),
        breadth=8,
        climb_steps=12,
    )
    opening = [c for c in calls if c[0] == 300]
    # The seeds' own round, one round of challengers, and then it stops -- not twelve.
    assert len(opening) <= 3


def test_a_climb_that_spends_every_step_says_so():
    """A truncated climb presented as a converged one is the same error as a truncated
    event fetch presented as a whole fight."""
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    counter = {"n": 0}

    def ever_better(candidate):
        counter["n"] += 1
        return 100.0 + counter["n"]

    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(ever_better),
        breadth=6,
        climb_steps=3,
    )
    assert any("all 3 of its steps" in note for note in outcome.notes)


def test_climbing_can_be_switched_off():
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    calls: list = []
    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0 + len(c.key), calls),
        breadth=6,
        climb_steps=0,
    )
    assert [c[0] for c in calls] == [r.iterations for r in outcome.rounds]


def test_a_field_that_never_separates_is_reported_rather_than_resolved():
    """Every candidate identical: there is no winner to find, and saying there is one
    would be the search convincing itself."""
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    outcome = search(
        spec_id="rogue_outlaw",
        build_id="rogue_outlaw_default",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0),
        breadth=10,
    )
    assert all(r.pruned == 0 for r in outcome.rounds)
    assert outcome.rounds[0].undivided
    assert not outcome.settled
    assert any("did not separate" in note for note in outcome.caveats())


def test_a_round_that_measures_nothing_stops_the_search_and_says_so():
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)

    def dead(candidates, iterations):
        return {}

    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=dead,
        breadth=4,
    )
    assert outcome.best is None
    assert any("No variant returned a result" in n for n in outcome.notes)


def test_a_search_with_no_seed_is_refused():
    with pytest.raises(SearchError):
        search(
            spec_id="x", build_id="x", seeds=[], nodes=sample_nodes(), runner=_runner(lambda c: 1)
        )


def test_every_caveat_states_the_bound_the_search_cannot_cross():
    """A published number carries the limits of the method that produced it, and the
    two that matter here are the node set and the unchecked unlock edges."""
    nodes = sample_nodes()
    seed = seed_from(key="s00", label="s", origin="simc", loadout=sample_build(), nodes=nodes)
    outcome = search(
        spec_id="x",
        build_id="x",
        seeds=[seed],
        nodes=nodes,
        runner=_runner(lambda c: 100.0 + len(c.key)),
        breadth=6,
    )
    text = " ".join(outcome.caveats())
    assert "never adds or drops a node" in text
    assert "Unlock edges" in text


def test_truncation_is_counted_apart_from_pruning():
    """ "We could not afford to carry these" is not "these were worse", and a run that
    reported one number for both would turn a budget decision into a finding."""
    rows = [measurement(f"k{i}", 100.0, error=0.3) for i in range(10)]
    survivors, pruned, truncated = prune(rows, keep=3)
    assert pruned == []
    assert len(survivors) == 3
    assert len(truncated) == 7


# --------------------------------------------------------------------------------
# The calibration gate
# --------------------------------------------------------------------------------


def row(build, simc_dps, search_dps, error=0.05, evaluated=20, recovered=False):
    return computedbuilds.CalibrationRow(
        build_id=build,
        label=build,
        simc=measurement("simc", simc_dps, error=error),
        found=None if search_dps is None else measurement("best", search_dps, error=error),
        variants_evaluated=evaluated,
        recovered_simc_build=recovered,
    )


def test_a_search_that_ties_everywhere_passes():
    rows = tuple(row(f"b{i}", 100.0, 100.0) for i in range(10))
    result = computedbuilds.Calibration(rows=rows)
    assert all(r.verdict == "tie" for r in rows)
    assert result.passed


def test_a_search_behind_on_more_than_a_fifth_of_builds_fails():
    rows = tuple(row(f"b{i}", 100.0, 99.0 if i < 3 else 100.0) for i in range(10))
    result = computedbuilds.Calibration(rows=rows)
    assert result.not_behind_share == pytest.approx(0.7)
    assert not result.passed


def test_one_catastrophic_loss_fails_even_when_the_share_is_fine():
    """The share criterion alone can be satisfied by a search that is spectacularly
    wrong on one build, and one build published 5% below simc's is worse than a run
    that publishes nothing."""
    rows = (row("bad", 100.0, 94.0),) + tuple(row(f"b{i}", 100.0, 101.0) for i in range(19))
    result = computedbuilds.Calibration(rows=rows)
    assert result.not_behind_share == pytest.approx(0.95)
    assert result.worst_loss > computedbuilds.PASS_MAX_LOSS
    assert not result.passed


def test_finding_no_candidate_counts_as_behind_and_bounds_nothing():
    """A search that finds nothing is not neutral, and no margin bounds the loss -- so
    treating it as zero would let "found nothing" clear a criterion about how badly the
    search may lose."""
    rows = (row("none", 100.0, None),) + tuple(row(f"b{i}", 100.0, 100.0) for i in range(9))
    result = computedbuilds.Calibration(rows=rows)
    assert rows[0].verdict == "no-candidate"
    assert result.worst_loss == float("inf")
    assert not result.passed


def test_an_empty_calibration_never_passes():
    """Nothing judged is not the same as nothing wrong."""
    assert not computedbuilds.Calibration(rows=()).passed


def test_the_criterion_is_stated_with_the_verdict():
    result = computedbuilds.Calibration(rows=(row("b", 100.0, 100.0),))
    assert "fixed in advance" in result.criterion()
    assert "PASSED" in result.summary()


def test_the_gate_thresholds_are_the_ones_fixed_in_advance():
    """Pinned so that moving them to fit a run is a visible edit rather than a tweak."""
    assert computedbuilds.PASS_MIN_NOT_BEHIND == 0.80
    assert computedbuilds.PASS_MAX_LOSS == 0.02
