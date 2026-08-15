/** Number and label formatting shared by tables, axes and tooltips. */

import type { RunSettings } from './types'

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
