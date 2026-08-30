/**
 * Which build the ranking presents, and what that costs in honesty.
 *
 * The owner's rule: SimulationCraft's build is the default wherever simc ships
 * one; a build this project computed takes its place only when it is genuinely
 * better, and then it has to be *visible* as a computed build. Never silently.
 *
 * ## The trap
 *
 * `index.json` measures every build on **simc's own shipped gear**.
 * `computed-builds.json` measures both contenders on a **gear anchor** -- one
 * normalised kit, so the only difference between them is the talents. The two
 * absolutes are therefore not comparable: on MID2 the anchored figures run
 * roughly 3-7% below the published ones for a build already wearing its tier
 * set, purely because the anchor sits at the floor of the tier's item level
 * band. Substituting an anchored DPS into the ranking would move every marked
 * build *down* while claiming it had been improved.
 *
 * What travels between the two is the **ratio**, which is exactly what the
 * computed run measures: profileset against profileset, one kit, talents the
 * only variable. So a winning row is ranked by `publishedDps × (1 + margin)`.
 *
 * ## Which margin, and why it stopped being the anchored one
 *
 * That assumption -- that a gain measured on the anchor holds on a kit a few
 * item levels above it -- **was measured on 2026-08-26 and does not hold on
 * every build**. Over all twelve marked MID2 builds the two margins agree to
 * about a tenth of a point on seven of nine, and disagree by **2.52 points** on
 * Devastation Evoker (Scalecommander): +2.53% on the anchor, **+0.02%** on
 * simc's own gear, the whole gain gone. Its sibling build moves the other way by
 * 0.69, so there is no correction factor -- a scale that fixed one would break
 * the other, and they are the same spec.
 *
 * So when the entry carries a `shipped` block the ranking uses **that** margin:
 * measured on the same kit the published DPS was measured on, with only the
 * talents varying. `marginBasis` says which of the two a row used.
 *
 * The anchored margin remains the fallback, for documents written before the
 * pipeline measured the other one. It is not a good fallback -- it is wrong by
 * two and a half points on one build in twelve and nothing visible says which --
 * and the answer is to re-run the search, not to blind the view. A row on the
 * fallback still reads `projected: true`.
 *
 * `rankDps` is a product either way, never a raw measurement, and `simcDps`
 * stays on the row so it can always be undone.
 *
 * ## Two things not done here
 *
 * - **No verdict read out of a file.** The margin and the band are recomputed
 *   from the two DPS values and their two errors. A published `beatsSimc`
 *   boolean could disagree with the figures printed beside it.
 * - **No fixed percentage.** "Better" is the project's tie rule --
 *   `hypot(errorA, errorB)`, the two means' errors in quadrature -- so the bar
 *   tracks the precision the run achieved. Measured on MID2 (2026-08-25): 17
 *   builds are numerically ahead and only 12 clear the band.
 */

import type {
  ComputedBuildsDataset,
  ComputedContender,
  ComputedShipped,
  ComputedSpec,
} from './types'

/** The newest document shape this code knows how to read. */
const SUPPORTED_SCHEMA = 1

export interface BestBuild {
  /** The value the row is ranked and drawn by. */
  rankDps: number
  /** The manifest's own measurement, always. Never projected. */
  simcDps: number
  /** True when `rankDps` is a product rather than a raw measurement. */
  projected: boolean
  /**
   * Which kit the ranking margin was measured on. `shipped-gear` is the one the
   * published DPS was also measured on; `anchor` is the fallback, and is known
   * to be wrong by 2.52 points on one of MID2's twelve marked builds.
   */
  marginBasis: 'shipped-gear' | 'anchor' | null
  /** Relative lead of the computed build, on whichever kit `marginBasis` names. */
  gain: number | null
  /** The tie band that lead had to clear. */
  noise: number | null
  /**
   * What the marked build does *on the boss*, relative to simc's own — the
   * other axis of the trade issue #99 names. Measured in the anchored run (both
   * contenders on one kit, so the ratio travels the way the margin does).
   *
   * Null wherever either side carries no priorityDps — and null on the
   * degenerate rows where both sides' priorityDps equal their dps to the
   * published rounding. That is the single-enemy signature: the live
   * computed-builds.json carries priorityDps == dps on 49 of its 52 one-target
   * rows, and rendering that ratio as "on the boss" would print the *anchored
   * total margin* beside the shipped-gear mark — a manufactured trade whose
   * whole size is the gear basis. A configured-1-target scenario with raid
   * events (Add Waves) carries a *real* split and is deliberately not filtered
   * by target count. This is disclosure, not a verdict — no tie band is
   * applied, because the run publishes no error for the priority figures.
   */
  priorityGain: number | null
  /** The winning candidate, for the tooltip. */
  computed: ComputedContender | null
  /**
   * Set when the margin was measured against a build the manifest no longer
   * publishes, in which case the mark is withheld and the row falls back to
   * simc's own number.
   *
   * simc repairs its own profiles: measured 2026-08-30 over the committed MID2
   * pair, six build ids carry a different `talentHash` in the two documents --
   * both Havoc, both Affliction, Arms and Fury, all repaired between the search
   * run and the nightly that republished the manifest -- and **13 rows across
   * the three target counts were being marked** with a lead measured against a
   * build the ranking beside them does not show.
   *
   * Null covers three states, and only one of them is this one: the hashes
   * agree, or one of them is absent. Absent is never divergence -- a manifest
   * from before the summary carried the hash would otherwise blank every mark
   * in the tier.
   */
  staleAgainst: 'talent-hash' | null
}

/** True when both a DPS and an error can be read. Half a side is not a side. */
function usable(side: ComputedContender | null | undefined): side is ComputedContender {
  return (
    !!side &&
    Number.isFinite(side.dps) &&
    side.dps > 0 &&
    Number.isFinite(side.dpsError) &&
    side.dpsError >= 0
  )
}

/**
 * True when a `shipped` block carries a whole comparison.
 *
 * Both numbers are checked, not just presence: a block with a margin and no band
 * would rank a lead against no bar at all, which is the one thing the tie rule
 * exists to prevent. A negative band is not a band.
 */
function usableShipped(
  block: ComputedShipped | null | undefined,
): block is ComputedShipped {
  return (
    !!block &&
    Number.isFinite(block.margin) &&
    Number.isFinite(block.tieBand) &&
    block.tieBand >= 0
  )
}

/**
 * The tie band of two measurements, from their percent standard errors.
 * A margin equal to the band is still a tie.
 */
export function combinedNoise(errorPctA: number, errorPctB: number): number {
  return Math.hypot(errorPctA / 100, errorPctB / 100)
}

/**
 * True when the entry's `simc` side is a build the manifest no longer publishes.
 *
 * Two hashes, three answers. They agree — nothing to say. They differ — the
 * margin on this row was measured against a different talent build from the one
 * the ranking shows, and the product `publishedDps x (1 + margin)` is a claim
 * about neither. Either is missing — **nothing is concluded**: a manifest from
 * before `talentHash` reached the summary row, a profile that states no hash, or
 * a computed row for a build the tier has since dropped all land here, and
 * reading absence as divergence would blank every mark in the tier the first
 * time an older dataset is read.
 *
 * The check cannot live at write time: a search measures simc's hash as it is
 * that minute, so the two always agree there. The divergence opens later, when a
 * nightly republishes the manifest — which is why it has to be the reader's.
 */
function measuredAgainstAnotherBuild(
  entry: ComputedSpec,
  shippedTalentHash: string | null | undefined,
): boolean {
  const measured = entry.simc?.talentHash
  if (!measured || !shippedTalentHash) return false
  return measured !== shippedTalentHash
}

export function bestBuildFor(
  simcDps: number,
  entry: ComputedSpec | null,
  /**
   * The talent hash the manifest publishes for this build, when it publishes one.
   * Optional by contract: every tier built before this field reached the summary
   * row passes nothing, and behaves exactly as it did.
   */
  shippedTalentHash?: string | null,
): BestBuild {
  const plain: BestBuild = {
    rankDps: simcDps,
    simcDps,
    projected: false,
    marginBasis: null,
    gain: null,
    noise: null,
    priorityGain: null,
    computed: null,
    staleAgainst: null,
  }
  if (!entry) return plain

  // Before anything is read off the entry: a margin against a build the ranking
  // does not show is not a smaller claim than no margin, it is a different one.
  // Falling back to simc's published number understates (the computed build may
  // really be ahead) rather than inventing, which is the direction this project
  // fails in.
  if (measuredAgainstAnotherBuild(entry, shippedTalentHash)) {
    return { ...plain, staleAgainst: 'talent-hash' }
  }

  const simc = usable(entry.simc) ? entry.simc : null
  const best = usable(entry.best) ? entry.best : null
  // With no simc contender there is no measured ratio to carry the published
  // number forward by, so there is nothing to project even when a computed
  // build exists. With no computed contender there is nothing to compare.
  if (!simc || !best) return plain

  // The measured margin when the run produced one, the anchored one otherwise.
  // Both `margin` and `tieBand` come from the same block: mixing a shipped-gear
  // margin with an anchored band would compare a number against the precision of
  // a different run.
  const measured = usableShipped(entry.shipped) ? entry.shipped : null
  const margin = measured ? measured.margin : best.dps / simc.dps - 1
  const noise = measured ? measured.tieBand : combinedNoise(best.dpsError, simc.dpsError)
  if (margin <= noise) return plain

  // Both sides measured in one anchored run, so the ratio is meaningful the
  // same way the anchored margin is. Zero on simc's side means the question is
  // undefined (nothing landed on a boss that is not there), not a lead. And a
  // row where both sides' priorityDps equal their dps (to the 0.1 rounding the
  // pipeline publishes) has no second axis at all — that is what a single-enemy
  // cell looks like in the document, and the "ratio" there is the anchored
  // total margin wearing a boss label.
  const singleEnemy =
    Number.isFinite(best.priorityDps) &&
    Number.isFinite(simc.priorityDps) &&
    Math.abs((best.priorityDps as number) - best.dps) < 0.1 &&
    Math.abs((simc.priorityDps as number) - simc.dps) < 0.1
  const priorityGain =
    !singleEnemy &&
    Number.isFinite(best.priorityDps) &&
    Number.isFinite(simc.priorityDps) &&
    (simc.priorityDps as number) > 0
      ? (best.priorityDps as number) / (simc.priorityDps as number) - 1
      : null

  return {
    rankDps: simcDps * (1 + margin),
    simcDps,
    projected: true,
    marginBasis: measured ? 'shipped-gear' : 'anchor',
    gain: margin,
    noise,
    priorityGain,
    computed: best,
    staleAgainst: null,
  }
}

/** True when the document is one this code knows how to read. */
export function isReadable(
  dataset: ComputedBuildsDataset | null | undefined,
): dataset is ComputedBuildsDataset {
  if (!dataset || !Array.isArray(dataset.specs)) return false
  const version = dataset.schemaVersion
  return Number.isFinite(version) && version >= 1 && version <= SUPPORTED_SCHEMA
}

/**
 * The entry for one build **as measured in the view being looked at**.
 *
 * The join is `(id, scenario, targets)`, not the id alone: a verdict is a
 * statement about one scenario at one target count, and the document's own
 * caveat says a build ahead at one target need not be ahead at ten.
 */
export function findComputedSpec(
  dataset: ComputedBuildsDataset | null | undefined,
  buildId: string,
  scenario: string,
  targets: number,
): ComputedSpec | null {
  if (!isReadable(dataset)) return null
  return (
    dataset.specs.find(
      (entry) =>
        entry?.id === buildId &&
        entry.scenario === scenario &&
        entry.targets === targets,
    ) ?? null
  )
}

/**
 * Why a scenario and target count carries no marks. Three sentences, not one:
 * `computed-builds.json` covers Patchwerk at one target today, so everywhere
 * else is `not-searched` -- and reporting that as "nothing found" would be a
 * finding the run never made.
 */
export type ComputedScope = 'absent' | 'not-searched' | 'searched'

export function computedScope(
  dataset: ComputedBuildsDataset | null | undefined,
  scenario: string,
  targets: number,
): ComputedScope {
  if (!isReadable(dataset)) return 'absent'
  const covered = dataset.specs.some(
    (entry) => entry?.scenario === scenario && entry.targets === targets,
  )
  return covered ? 'searched' : 'not-searched'
}

/** "computed +2.20%", or nothing. The size is in the mark, not only a tooltip. */
export function bestBuildMark(best: BestBuild): string | null {
  if (!best.projected || best.gain === null) return null
  return `computed +${(best.gain * 100).toFixed(2)}%`
}
