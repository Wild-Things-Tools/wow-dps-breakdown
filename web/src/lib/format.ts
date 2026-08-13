/** Number and label formatting shared by tables, axes and tooltips. */

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
 * Funnel index, phrased the way it should be read.
 *
 * The raw number is "how many times more damage the main target takes than an
 * even split would give it", which is not self-explanatory, so the UI always
 * pairs it with this wording.
 */
export function describeFunnel(index: number, targets: number): string {
  if (index <= 1.08) return 'Spread evenly'
  if (index >= targets * 0.92) return 'Almost all on main target'
  if (index >= targets * 0.6) return 'Heavily funnelled'
  if (index >= 1.6) return 'Funnels to main target'
  return 'Slightly favours main target'
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
