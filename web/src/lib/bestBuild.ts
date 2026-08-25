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
 * That product is a **projection**, not a measurement, and `projected` says so
 * on every row carrying one. Nobody has run the computed talents on simc's
 * shipped gear; the assumption is that a talent gain measured on the anchor
 * holds on a kit a few item levels above it. It is a smaller assumption than
 * mixing the two absolutes, and it leaves every unmarked row byte-for-byte what
 * it was. `simcDps` stays on the row so the projection can always be undone.
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

import type { ComputedBuildsDataset, ComputedContender, ComputedSpec } from './types'

/** The newest document shape this code knows how to read. */
const SUPPORTED_SCHEMA = 1

export interface BestBuild {
  /** The value the row is ranked and drawn by. */
  rankDps: number
  /** The manifest's own measurement, always. Never projected. */
  simcDps: number
  /** True when `rankDps` is a projection rather than a measurement. */
  projected: boolean
  /** Measured relative lead of the computed build, on the anchored kit. */
  gain: number | null
  /** The tie band that lead had to clear. */
  noise: number | null
  /** The winning candidate, for the tooltip. */
  computed: ComputedContender | null
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
 * The tie band of two measurements, from their percent standard errors.
 * A margin equal to the band is still a tie.
 */
export function combinedNoise(errorPctA: number, errorPctB: number): number {
  return Math.hypot(errorPctA / 100, errorPctB / 100)
}

export function bestBuildFor(simcDps: number, entry: ComputedSpec | null): BestBuild {
  const plain: BestBuild = {
    rankDps: simcDps,
    simcDps,
    projected: false,
    gain: null,
    noise: null,
    computed: null,
  }
  if (!entry) return plain

  const simc = usable(entry.simc) ? entry.simc : null
  const best = usable(entry.best) ? entry.best : null
  // With no simc contender there is no measured ratio to carry the published
  // number forward by, so there is nothing to project even when a computed
  // build exists. With no computed contender there is nothing to compare.
  if (!simc || !best) return plain

  const margin = best.dps / simc.dps - 1
  const noise = combinedNoise(best.dpsError, simc.dpsError)
  if (margin <= noise) return plain

  return {
    rankDps: simcDps * (1 + margin),
    simcDps,
    projected: true,
    gain: margin,
    noise,
    computed: best,
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
