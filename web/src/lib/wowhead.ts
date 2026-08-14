/**
 * Wowhead tooltips: the one third-party script on the site, and its blast radius.
 *
 * Wowhead publishes a script that walks the document for anchors pointing at its
 * item and spell pages and gives each one a hover card, an inline icon and
 * (optionally) quality colouring. Everything specific to that script lives here,
 * so `GameLink` stays an anchor with a reserved icon slot and nothing else in the
 * app knows Wowhead exists.
 *
 * The decisions below were checked against the shipped script itself
 * (`wow.zamimg.com/widgets/power.js`, read 2026-08-14) rather than against
 * documentation, because the behaviour that matters here is not documented and
 * the widely quoted `wow.zamimg.com/js/power.js` URL 404s.
 *
 * **The script is loaded lazily, on the first `GameLink` that mounts.** Only the
 * Loot view has game links, so a visitor who never opens it makes no request to
 * Wowhead at all. That is worth keeping: the published site is guild-internal
 * behind a Discord login, and a script tag in `index.html` would hand a third
 * party a request from every viewer of every view.
 *
 * **Links that mount later need `refreshLinks`.** The script enriches
 * `document.links` once on load; a React view that renders after that is invisible
 * to it. `$WowheadPower.refreshLinks()` re-walks the document, so every `GameLink`
 * asks for one on mount and the calls are batched into a single pass per tick.
 * It also re-reads each link's `data-wowhead`, which is what makes the item level
 * toggle change what the hover card says.
 *
 * **Colouring is off, icons are on, renaming is off** -- see `TOOLTIP_CONFIG`.
 */

/** What kinds of thing Wowhead can be asked about. Spells are not linked yet. */
export type GameEntity = 'item' | 'spell'

const SCRIPT_URL = 'https://wow.zamimg.com/widgets/power.js'

/**
 * `colorLinks` would repaint every item name in Wowhead's quality colours with
 * `!important`, overriding the theme's ink tokens in both light and dark mode.
 * It would also say nothing: every trinket in the sweep is an epic, so the colour
 * is a constant. And in these tables text colour already means something -- a
 * muted cell is a tie, an inked one is a lead.
 *
 * `renameLinks` would replace the anchor's contents with Wowhead's own name for
 * the item, asynchronously, after the tooltip data arrives. Three reasons it stays
 * off, in increasing order of weight: it destroys the anchor's children, and the
 * reserved icon slot is one of them; the text would visibly change after load; and
 * the name in the dataset is the name of the thing that was actually simulated,
 * read out of the same `item_data.inc` the sweep enumerated its candidates from.
 * The chart's axis labels are SVG and can never be renamed, so a rename would put
 * one item on screen under two names. Checked on the current tier: all 40 trinket
 * names are identical either way, so the option buys nothing today and risks a
 * mismatch later.
 */
const TOOLTIP_CONFIG = {
  colorLinks: false,
  iconizeLinks: true,
  renameLinks: false,
} as const

declare global {
  interface Window {
    whTooltips?: typeof TOOLTIP_CONFIG
    $WowheadPower?: { refreshLinks: () => void }
  }
}

let requested = false

/**
 * Fetch the tooltip script, once per page load.
 *
 * Nothing here reports failure, because failure is not exceptional: the script is
 * third party, and ad blockers, an offline viewer and a Wowhead outage all end the
 * same way. What is left is a plain anchor with a readable name that still opens
 * the item's page when clicked, which is why the icon slot is reserved in CSS
 * rather than added by the script.
 */
export function loadWowheadTooltips(): void {
  if (requested || typeof document === 'undefined') return
  requested = true

  window.whTooltips = TOOLTIP_CONFIG

  const script = document.createElement('script')
  script.src = SCRIPT_URL
  script.async = true
  document.head.append(script)
}

let pending = 0

/**
 * Ask the script to re-walk the document, at most once per tick.
 *
 * A table of forty links calls this forty times and costs one pass. Harmless
 * before the script has loaded: its own startup walks the document anyway.
 */
export function refreshWowheadLinks(): void {
  if (pending || typeof window === 'undefined') return
  pending = window.setTimeout(() => {
    pending = 0
    window.$WowheadPower?.refreshLinks()
  }, 0)
}

/** The page a click should open. Plain, canonical, no parameters. */
export function wowheadUrl(kind: GameEntity, id: number): string {
  return `https://www.wowhead.com/${kind}=${id}`
}

/**
 * What the hover card should describe.
 *
 * The item level is not decoration: the same trinket at 334 and at 344 has
 * different numbers in it, and the view always knows which one the figures beside
 * the name were simulated at. Passing it keeps the card and the row consistent.
 */
export function wowheadParams(kind: GameEntity, id: number, ilevel?: number): string {
  const params = [`${kind}=${id}`]
  if (kind === 'item' && ilevel) params.push(`ilvl=${ilevel}`)
  return params.join('&')
}
