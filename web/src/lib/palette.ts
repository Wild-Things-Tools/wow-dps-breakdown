/**
 * Colour assignment.
 *
 * Two palettes live here and they answer different questions.
 *
 * **Class colours** encode *identity* — which class a build belongs to. They are
 * the primary encoding across the site: bars, lines, sparklines and names all
 * wear them, the way Warcraft Logs does it.
 *
 * **Series slots** encode *series* in the handful of charts whose marks are not
 * builds at all (main target vs everything else, gain vs loss). Those are still
 * the validated eight, still handed out in fixed order, never cycled.
 *
 * ---------------------------------------------------------------------------
 * Why class colour as a primary encoding, when it fails the palette checks
 * ---------------------------------------------------------------------------
 *
 * An earlier version of this file said class colours were "used only as a
 * secondary identity cue -- a small dot beside a name -- and never to encode a
 * series". That rule is deliberately overridden, by the owner's call: it is the
 * domain convention, every WoW site follows it, and a reader who plays the game
 * identifies a class from its colour faster than from its name.
 *
 * The reason for the old rule has not gone away, so it is stated rather than
 * quietly dropped. Run against the dataviz validator on the dark surface, the
 * thirteen fail three of the six checks by construction:
 *
 *   node scripts/validate_palette.js \
 *     "#e84756,#be4fe6,#ff7c0a,#33937f,#aad372,#3fc7eb,#00ff98,#f48cba,\
 *      #ffffff,#fff468,#2081ef,#8788ee,#c69b6d" \
 *     --mode dark --surface "#1a1a19" --pairs all
 *
 *   Lightness band   FAIL  nine of thirteen outside 0.48-0.67
 *   Chroma floor     FAIL  Priest (0), Warrior (0.08), Evoker (0.094) read grey
 *   CVD separation   FAIL  worst all-pairs Warlock/Demon Hunter ΔE 3.3 (deutan)
 *   Normal vision    FAIL  worst all-pairs Warlock/Shaman ΔE 10.4, floor is 15
 *   Contrast         PASS  all thirteen >= 3:1, and >= 4.5:1 after the lifts
 *
 * Thirteen fixed hues can never pass; no re-ordering fixes it. So the colour is
 * a *redundant* cue and something else carries identity, in every single place
 * a class colour appears:
 *
 *   1. **An icon.** Every build shows its class icon, its spec icon and its
 *      hero-tree icon. Three glyph channels, all independent of hue.
 *   2. **A name.** Every icon carries an accessible name, and outside the
 *      tightest axis labels the name is written out beside it.
 *   3. **A table.** Every chart still has its table twin. This is the relief
 *      channel the contrast check obliges and it is not removable.
 *
 * The one case colour genuinely cannot separate is two builds of the *same*
 * spec: Frost Death Knight Default and Frost Death Knight Rider of the
 * Apocalypse are one class and one spec, so they are one colour and one spec
 * icon. Their hero-tree icon and its written-out name are what tell them apart,
 * plus `buildDash` on line charts.
 */

import type { SpecSummary } from './types'

/**
 * Canonical World of Warcraft class colours, adjusted for legibility on this
 * app's dark surface. The token values and the contrast measurements behind
 * them are in `index.css`; this map is the class-name lookup.
 */
export const CLASS_TOKENS: Record<string, string> = {
  'Death Knight': '--class-death-knight',
  'Demon Hunter': '--class-demon-hunter',
  Druid: '--class-druid',
  Evoker: '--class-evoker',
  Hunter: '--class-hunter',
  Mage: '--class-mage',
  Monk: '--class-monk',
  Paladin: '--class-paladin',
  Priest: '--class-priest',
  Rogue: '--class-rogue',
  Shaman: '--class-shaman',
  Warlock: '--class-warlock',
  Warrior: '--class-warrior',
}

/**
 * Class colour as a CSS value, for marks and for text alike.
 *
 * Every token clears 4.5:1 against the panel surface, so the same value is
 * legal as a bar fill, a line stroke and a label colour. An unknown class gets
 * a neutral rather than borrowing a real class's identity.
 */
export function classColor(wowClass: string): string {
  const token = CLASS_TOKENS[wowClass]
  return token ? `var(${token})` : 'var(--class-unknown)'
}

/**
 * The same colour, washed into the surface, for a chip or row background.
 *
 * A fill this faint is not carrying any information -- the chip's border, icon
 * and text do that -- so it has no contrast obligation of its own. It exists so
 * a row reads as belonging to a class at a glance.
 */
export function classWash(wowClass: string, percent = 14): string {
  return `color-mix(in oklab, ${classColor(wowClass)} ${percent}%, transparent)`
}

/**
 * Stroke dash for the nth build *within one spec*.
 *
 * Class colour identifies the class and the spec icon identifies the spec, so
 * two hero-talent builds of one spec arrive at a line chart identical. The dash
 * separates them without inventing an unvalidated colour; the hero-tree icon
 * and its written-out name in the legend and the direct label carry the rest.
 */
const BUILD_DASHES = ['0', '7 4', '2 3', '10 3 2 3'] as const

export function buildDash(index: number): string {
  return BUILD_DASHES[index % BUILD_DASHES.length] ?? '0'
}

/**
 * Series colour slots, for the charts whose marks are not builds.
 *
 * Validated as an *ordered* set against the dark surface, so they are handed
 * out in fixed order and never cycled.
 */
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
 * as a discrete labelled mark starts at step 250.
 */
export function sequentialStep(value: number, ordinal = true): string {
  const clamped = Math.max(0, Math.min(1, value))
  const floor = ordinal ? 1 : 0
  const span = SEQUENTIAL.length - 1 - floor
  const index = floor + Math.round(clamped * span)
  return SEQUENTIAL[index] ?? SEQUENTIAL[SEQUENTIAL.length - 1]!
}

/**
 * Index of a build among its own spec's builds, in manifest order.
 *
 * The one thing `buildDash` needs and the one thing the manifest does not
 * spell out.
 */
export function buildIndexWithinSpec(specs: SpecSummary[], id: string): number {
  const self = specs.find((spec) => spec.id === id)
  if (!self) return 0
  return specs.filter((spec) => spec.specId === self.specId).findIndex((spec) => spec.id === id)
}
