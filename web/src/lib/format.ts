/** Number and label formatting shared by tables, axes and tooltips. */

import type { Manifest, RunSettings } from './types'

const COMPACT = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})

const FULL = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })

/** Axis ticks and dense table cells: 1.2M, 587.4k. */
export function compactNumber(value: number): string {
  return COMPACT.format(value)
}

/** Tooltips and detail rows, where the exact figure matters: 587,407. */
export function fullNumber(value: number): string {
  return FULL.format(value)
}

export function percent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`
}

/**
 * Concentration, phrased the way it should be read.
 *
 * Describes *distribution* only — how the damage is spread across the targets that
 * are present. Deliberately avoids the word "funnel", which means something else
 * (see describeFunnelGain).
 */
export function describeConcentration(index: number, targets: number): string {
  if (index <= 1.08) return 'Spread evenly'
  if (index >= targets * 0.92) return 'Almost all on main target'
  if (index >= targets * 0.6) return 'Very concentrated'
  if (index >= 1.6) return 'Concentrated'
  return 'Slightly favours main target'
}

/**
 * Funnel gain, phrased the way it should be read.
 *
 * 1.0 means the main target takes exactly what it would take if it were alone.
 * Above that, the extra targets are feeding it. Below, area damage is costing it.
 */
export function describeFunnelGain(gain: number): string {
  if (gain >= 1.1) return 'Strong funnel'
  if (gain >= 1.02) return 'Funnels onto the main target'
  if (gain >= 0.98) return 'Main target unaffected'
  if (gain >= 0.85) return 'Slight cost to the main target'
  return 'Main target loses out'
}

export function describeBurst(ratio: number): string {
  if (ratio >= 2.2) return 'Heavy burst window'
  if (ratio >= 1.6) return 'Noticeable burst'
  if (ratio >= 1.25) return 'Mild burst'
  return 'Steady output'
}

export function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

export function relativeAge(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const hours = Math.round((Date.now() - date.getTime()) / 36e5)
  if (hours < 1) return 'just now'
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return days === 1 ? 'yesterday' : `${days} days ago`
}

/**
 * How precise a run is, in a form that can sit in a sentence.
 *
 * Reports the error that was *measured*, not the one that was requested. A
 * deterministic run asks for no target error at all, so quoting the request would
 * read as "0% error" — precision the numbers do not have.
 */
export function samplingError(settings: RunSettings): string {
  const measured = settings.medianDpsError
  if (typeof measured === 'number' && measured > 0) {
    return `${measured < 0.1 ? measured.toFixed(2) : measured.toFixed(1)}%`
  }
  // Older datasets carry only the requested error.
  return `${settings.targetError}%`
}

/**
 * The spread of per-scenario precision, for a sentence that covers every view.
 *
 * `settings.medianDpsError` is ONE median over ALL scenarios, and the population is
 * lopsided: 510 of MID2's 864 cells are Patchwerk, so the pooled figure is
 * essentially Patchwerk's. Measured against the committed manifest on 2026-08-30,
 * pooled 0.0555%:
 *
 * ```
 * patchwerk        n=510  0.0527   0.95x        boss_53445   n=51  0.0457  0.82x
 * addwaves         n= 51  0.0709   1.28x        boss_53455   n=51  0.0477  0.86x
 * hecticaddcleave  n= 51  0.1177   2.12x        boss_53470   n=51  0.0581  1.05x
 * dungeonslice     n= 48  0.1149   2.07x        boss_53497   n=51  0.0501  0.90x
 * ```
 *
 * So the footer's point value understates the two noisiest scenarios by a factor of
 * two, on every page (#103). The chart caption already solves this its own way, by
 * taking the median of the rows actually on screen; the footer has no such context,
 * so it names the range instead of picking one number to be wrong with.
 *
 * Derived from the per-count `dpsError` maps the manifest already publishes rather
 * than from a new field. Two reasons, and the second is this project's rule: those
 * maps are the cells the tie rule itself uses, so a published per-scenario median
 * would be a second source that could disagree with the numbers beside it.
 *
 * Returns `null` when the manifest carries no usable per-cell errors -- an older
 * dataset, or one where every error is zero -- and the caller falls back to
 * `samplingError`. A zero is not a measurement.
 */
export function samplingErrorRange(manifest: Manifest): { low: number; high: number } | null {
  const medians: number[] = []
  const byScenario = new Map<string, number[]>()
  for (const spec of manifest.specs ?? []) {
    for (const [name, scenario] of Object.entries(spec.scenarios ?? {})) {
      const errors = (scenario as { dpsError?: Record<string, number> }).dpsError
      if (!errors) continue
      for (const value of Object.values(errors)) {
        if (typeof value === 'number' && value > 0) {
          const bucket = byScenario.get(name)
          if (bucket) bucket.push(value)
          else byScenario.set(name, [value])
        }
      }
    }
  }
  for (const values of byScenario.values()) {
    values.sort((a, b) => a - b)
    const mid = Math.floor(values.length / 2)
    const upper = values[mid] ?? 0
    const lower = values[mid - 1] ?? upper
    medians.push(values.length % 2 === 1 ? upper : (lower + upper) / 2)
  }
  if (medians.length === 0) return null
  return { low: Math.min(...medians), high: Math.max(...medians) }
}

/** The range as a sentence fragment, or the pooled point value where there is none. */
export function describeSamplingError(manifest: Manifest): string {
  const range = samplingErrorRange(manifest)
  if (!range) return samplingError(manifest.settings)
  // BOTH ends at the same precision, chosen by the smaller one. `samplingError`'s
  // rule (2dp below 0.1%, 1dp above) applied per end gives "0.05% to 0.1%" for
  // 0.0527/0.1177 -- two different precisions in one range, and the wider end reads
  // as less certain than the narrow one when it is the opposite.
  const decimals = range.low < 0.1 ? 2 : 1
  const show = (value: number) => value.toFixed(decimals)
  // One scenario, or two that round to the same figure: a range reading "0.05% to
  // 0.05%" is worse than the point value it came from.
  if (show(range.low) === show(range.high)) return `${show(range.low)}%`
  return `${show(range.low)}% to ${show(range.high)}% depending on the scenario`
}

/** "3,000 deterministic iterations per sim" / "adaptive sampling down to 0.3%". */
export function describeConvergence(settings: RunSettings): string {
  if (settings.deterministic ?? settings.targetError === 0) {
    return `${fullNumber(settings.maxIterations)} deterministic iterations per sim`
  }
  return `adaptive sampling down to ${settings.targetError}% standard error`
}

/**
 * A plain sentence about the game state simc modelled: which patch, which hotfix,
 * and the standing caveat that balance changes after that date are not yet in.
 *
 * The date is stated rather than judged. "Is last night's class tuning in here?"
 * is answered by comparing the hotfix date to the tuning date -- which the reader
 * can do, and which does not go stale the way a hardcoded "the tuning is/ is not
 * included" would.
 */
export function describeGameBuild(simc: {
  wowVersion?: string
  hotfixDate?: string
}): string {
  if (!simc.wowVersion) return 'The game build these numbers model was not recorded.'
  const hotfix = simc.hotfixDate ? `, game-data hotfix ${simc.hotfixDate}` : ''
  return (
    `These numbers model World of Warcraft ${simc.wowVersion}${hotfix}. ` +
    'Balance changes Blizzard applied after that date are not reflected until ' +
    "SimulationCraft's data is regenerated and the tier is re-simulated."
  )
}
