/**
 * A game entity's name, annotated by Wowhead when Wowhead is reachable.
 *
 * Items today -- the Loot view is the payoff, where a loot council wants to hover
 * a trinket and read what it does without leaving the comparison. Abilities are
 * the same component: the ability breakdown already carries simc's spell id per
 * row, so `<GameLink kind="spell" id={ability.id} name={ability.name} />` is the
 * whole of that change when it comes.
 *
 * What this renders without any script at all is the point. A plain anchor with
 * the dataset's name, a dotted underline to say it is hoverable, and an empty
 * 18px slot where the icon goes. Wowhead's stylesheet adds exactly 18px of left
 * padding to a link it has iconised, so when the icon does arrive the slot
 * collapses and the text does not move -- see `.gamelink-icon` in `index.css`.
 */

import { useEffect } from 'react'
import { cx } from './ui'
import {
  type GameEntity,
  loadWowheadTooltips,
  refreshWowheadLinks,
  wowheadParams,
  wowheadUrl,
} from '../lib/wowhead'

export function GameLink({
  kind,
  id,
  name,
  ilevel,
  className,
}: {
  kind: GameEntity
  id: number
  name: string
  /** Item level the figures beside this name were simulated at. Items only. */
  ilevel?: number
  className?: string
}) {
  // Mounting a link is what pulls the script in, and what tells an already-loaded
  // script to look again. Re-running when `ilevel` changes is what makes the item
  // level toggle change the card as well as the numbers.
  useEffect(() => {
    loadWowheadTooltips()
    refreshWowheadLinks()
  }, [kind, id, ilevel])

  return (
    <a
      className={cx('gamelink', className)}
      href={wowheadUrl(kind, id)}
      data-wowhead={wowheadParams(kind, id, ilevel)}
      // Belt and braces against renameLinks ever being turned on: the rename
      // replaces this anchor's children, and one of them is the icon slot.
      data-wh-rename-link="false"
      target="_blank"
      rel="noreferrer"
    >
      <span className="gamelink-icon" aria-hidden />
      {name}
    </a>
  )
}
