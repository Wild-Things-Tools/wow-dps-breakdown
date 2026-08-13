# Prior art

Does this already exist? The question matters for two reasons: if some other project
already publishes main-target damage as a function of target count, this one is redundant,
and if it does not, that absence should be stated carefully rather than turned into a
claim that cannot be supported.

This file records what a search found, what was actually verified, and what was not. The
distinction is deliberate and load-bearing: parts of the first pass were checked by hand
afterwards and turned out to be **wrong**. Read the confidence labels before quoting
anything here.

## Verified by reading source

These were confirmed by reading code on GitHub, not by trusting a search snippet.

**[bloodmallet/bloodytools](https://github.com/Bloodmallet/bloodytools)** is the closest
comparable project — the same simulator, the same idea of orchestrating it in bulk and
publishing the results. It structurally cannot produce the funnel metric. Its `DataType`
enum in `bloodytools/utils/data_type.py` has exactly one member, `DPS = "dps"`, and
`_collect_data` branches on it to call `profile.get_dps()`. There is no `prioritydps`
code path anywhere in the project.

**Sweeping target counts is not itself novel.** bloodytools'
`talent_target_scaling_simulator.py` sweeps 1, 2, 3, 4, 5, 6, 8, 9 and 15 targets. Curves
by target count are established practice; what is being claimed here is the main-target
measurement layered on top of one.

**A GitHub-wide code search for `desired_targets prioritydps` returns only SimulationCraft
itself and its forks.** Searching for `prioritydps` alone returns roughly 54 hits: almost
all simc and forks, two report viewers, and Mystler's
[Ravenholdt-TC/SimcScripts](https://github.com/Ravenholdt-TC/SimcScripts), a set of local
Ruby scripts that does collect `prioritydps` but writes local CSV and publishes nothing.
No open-source project found sweeps target counts *and* collects `prioritydps`.

**simc issue [#1091](https://github.com/simulationcraft/simc/issues/1091)** — no
per-target damage breakdown — was opened on 2012-01-10 and is still open. That is why
`prioritydps` is the only route to this measurement.

## Not verified — treat as open

The research pass had network access to github.com only. Everything below came from
search-engine snippets. Two were checked by hand afterwards and did not hold up, which is
reason enough to distrust the rest.

- **bloodmallet.com publishing a "Talent Target Scaling" chart.** The generator code is in
  the repository, but a manual look at the live site did not find such a chart published.
  Best reading: code present, chart apparently not published. Unconfirmed either way.
- **Azortharion publishing "Prio", "Route Prio" and "AoE Tax" metrics.** A manual check of
  azortharion.com found only Power Infusion and Skyfury sims. The snippet-based claim is
  unreliable; whether mplus-sims.azortharion.com carries anything of the sort is unknown.
- **Whether Raidbots exposes `prioritydps`.** Unresolved.
- **Whether anyone publishes per-spec burst / damage-over-time profiles.** Unresolved, with
  weak evidence in either direction.
- **Discord-native prior art cannot be ruled out at all.** Most WoW theorycrafting lives in
  class Discords that no search engine indexes. A negative result from a web search says
  nothing about them.

## The verdict, phrased honestly

The *composition* — main-target damage normalised against a single-target baseline, as a
function of target count, across specs, published openly — appears to be unpublished. The
*concept* of funnelling is ordinary community vocabulary, and tier lists rank specs on it
editorially all the time.

So the defensible phrasing is "the first published systematic measurement". Never "the
first funnel analysis": that would claim the concept, which is plainly not new, rather than
the measurement.

Before making any public novelty claim, re-check azortharion.com and
mplus-sims.azortharion.com from an unblocked network. Those are the two sites most likely
to falsify the claim, and they are exactly the two this pass could not reach.
