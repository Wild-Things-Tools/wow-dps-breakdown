/**
 * Series colour assignment.
 *
 * The eight categorical slots are validated as an *ordered* set, so they are
 * handed out in fixed order and never cycled. Colour follows the entity: once a
 * spec is selected it keeps its slot until it is deselected, so filtering the
 * chart never repaints the survivors.
 *
 * Comparisons are capped at six series. Past that, adjacent-pair separation is
 * still fine but a legend of eight lines stops being readable, and the honest
 * answer is a table rather than more colours.
 */

export const MAX_SERIES = 6

const SLOTS = [
  'var(--series-1)',
  'var(--series-2)',
  'var(--series-3)',
  'var(--series-4)',
  'var(--series-5)',
  'var(--series-6)',
  'var(--series-7)',
  'var(--series-8)',
] as const

/**
 * Assign a colour slot per entity id, stable across re-renders and filter
 * changes. Slots freed by a deselection are reused by the next selection.
 */
export class SeriesPalette {
  private assigned = new Map<string, number>()

  sync(ids: string[]): void {
    for (const id of [...this.assigned.keys()]) {
      if (!ids.includes(id)) this.assigned.delete(id)
    }
    for (const id of ids) {
      if (this.assigned.has(id)) continue
      const taken = new Set(this.assigned.values())
      let slot = 0
      while (taken.has(slot) && slot < SLOTS.length - 1) slot += 1
      this.assigned.set(id, slot)
    }
  }

  colorOf(id: string): string {
    const slot = this.assigned.get(id) ?? 0
    return SLOTS[slot] ?? SLOTS[0]
  }
}

/** Colour for a fixed slot index, for charts with a static series order. */
export function slotColor(index: number): string {
  return SLOTS[index % SLOTS.length] ?? SLOTS[0]
}

/** Sequential blue ramp for magnitude encoding (ability shares, heat cells). */
export const SEQUENTIAL = [
  'var(--seq-100)',
  'var(--seq-250)',
  'var(--seq-400)',
  'var(--seq-550)',
  'var(--seq-700)',
] as const

/**
 * Step of the sequential ramp for a 0..1 magnitude.
 *
 * The lightest step is reserved for near-zero values; anything meant to be read
 * as a discrete labelled mark starts at step 250 so it clears 2:1 on the light
 * surface.
 */
export function sequentialStep(value: number, ordinal = true): string {
  const clamped = Math.max(0, Math.min(1, value))
  const floor = ordinal ? 1 : 0
  const span = SEQUENTIAL.length - 1 - floor
  const index = floor + Math.round(clamped * span)
  return SEQUENTIAL[index] ?? SEQUENTIAL[SEQUENTIAL.length - 1]!
}

/**
 * Canonical World of Warcraft class colours.
 *
 * These are a domain convention players read instantly, but as a set of thirteen
 * they are nowhere near colour-blind safe. So they are used only as a secondary
 * identity cue -- a small dot beside a name -- and never to encode a series.
 */
export const CLASS_COLORS: Record<string, string> = {
  'Death Knight': '#c41e3a',
  'Demon Hunter': '#a330c9',
  Druid: '#ff7c0a',
  Evoker: '#33937f',
  Hunter: '#aad372',
  Mage: '#3fc7eb',
  Monk: '#00ff98',
  Paladin: '#f48cba',
  Priest: '#ffffff',
  Rogue: '#fff468',
  Shaman: '#0070dd',
  Warlock: '#8788ee',
  Warrior: '#c69b6d',
}

export function classColor(wowClass: string): string {
  return CLASS_COLORS[wowClass] ?? 'var(--text-muted)'
}
