/**
 * A build's talent tree, drawn from the decoded loadout.
 *
 * Display follows wtt-frontend's widget: three CSS grids side by side (class,
 * specialisation, hero), each node placed at its own `row`/`col`, taken nodes lit and
 * passed-over ones dimmed but present. Drawing only the taken nodes would be a list,
 * and the shape of what a build *passed over* is most of what a reader is looking for.
 *
 * Two deliberate departures from that widget, both forced by what simc ships:
 *
 * - **No connector lines.** Blizzard's API carries an `unlocks` edge list and simc
 *   carries no edge table at all. A guessed edge is a claim about the tree, so the
 *   grid is drawn without them.
 * - **Icons come from Wowhead, not from us.** simc has no icon name for a spell, so
 *   each node is a Wowhead spell link and their script paints the icon into it. That
 *   is also why this is HTML and not SVG: `power.js` skips any element whose
 *   `nodeName` is not `A` or `AREA`, so a node drawn as an SVG rect could never carry
 *   a tooltip. Until the script answers, the node shows its own initials over a tile,
 *   the same fallback `EntityIcon` uses, so a blocked CDN costs the picture and
 *   nothing else.
 */
import { useEffect, useMemo } from 'react'
import type { TalentTreeBuild, TalentTreeLayout, TalentTreeNode } from '../lib/types'
import { loadWowheadTooltips, refreshWowheadLinks, wowheadParams, wowheadUrl } from '../lib/wowhead'
import { cx } from './ui'

const CELL = 44
const TREE_CLASS = 1
const TREE_SPEC = 2
const TREE_HERO = 3
const TYPE_CHOICE = 2

type Picked = { rank: number; entry: number }

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((word) => word[0] ?? '')
    .join('')
    .toUpperCase()
}

function TalentNode({
  node,
  picked,
  color,
}: {
  node: TalentTreeNode
  picked: Picked | undefined
  color: string
}) {
  // A choice node shows whichever half the build took; an untaken one shows the
  // first, which is what the game's own tooltip does when nothing is selected.
  const entry = picked ? (node.entries.find((e) => e.id === picked.entry) ?? node.entries[0]) : node.entries[0]
  if (!entry) return null

  const taken = !!picked && picked.rank > 0
  const choice = node.type === TYPE_CHOICE && node.entries.length > 1
  const label = `${entry.name}${node.maxRanks > 1 ? ` (${picked?.rank ?? 0}/${node.maxRanks})` : ''}`

  return (
    <a
      className={cx(
        'talent-node group relative block',
        taken ? 'talent-node-taken' : 'talent-node-idle',
        choice && 'talent-node-choice',
      )}
      style={{
        gridRow: node.row,
        gridColumn: node.col,
        // The tile behind the icon carries the class colour so a build reads as its
        // class even before Wowhead's script has answered.
        ['--node-color' as string]: color,
      }}
      href={wowheadUrl('spell', entry.spellId)}
      data-wowhead={wowheadParams('spell', entry.spellId)}
      data-wh-rename-link="false"
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      title={label}
    >
      <span className="talent-node-fallback" aria-hidden>
        {initials(entry.name)}
      </span>
      {node.maxRanks > 1 ? (
        <span className="talent-rank" aria-hidden>
          {picked?.rank ?? 0}/{node.maxRanks}
        </span>
      ) : null}
    </a>
  )
}

function TreeGrid({
  nodes,
  selected,
  color,
}: {
  nodes: TalentTreeNode[]
  selected: Map<number, Picked>
  color: string
}) {
  const cols = Math.max(...nodes.map((n) => n.col), 1)
  const rows = Math.max(...nodes.map((n) => n.row), 1)
  return (
    <div
      className="grid justify-center gap-1"
      style={{
        gridTemplateColumns: `repeat(${cols}, ${CELL}px)`,
        gridTemplateRows: `repeat(${rows}, ${CELL}px)`,
      }}
    >
      {nodes.map((node) => (
        <TalentNode key={node.id} node={node} picked={selected.get(node.id)} color={color} />
      ))}
    </div>
  )
}

export function TalentTree({
  layout,
  build,
  color,
}: {
  layout: TalentTreeLayout
  build: TalentTreeBuild
  color: string
}) {
  // React renders after the script's own startup walk, so every mount has to ask it
  // to look again -- without this the tooltips work on a reload and never after a
  // view change. Same arrangement as GameLink.
  useEffect(() => {
    loadWowheadTooltips()
    refreshWowheadLinks()
  }, [build.specId])

  const selected = useMemo(() => {
    const map = new Map<number, Picked>()
    for (const pick of build.selected) map.set(pick.id, { rank: pick.rank, entry: pick.entry })
    return map
  }, [build.selected])

  const sections = [
    { id: TREE_CLASS, label: 'Class', points: build.points.class },
    { id: TREE_SPEC, label: 'Specialisation', points: build.points.spec },
    { id: TREE_HERO, label: build.heroTalent ?? 'Hero', points: build.points.hero },
  ]

  return (
    <div className="flex flex-wrap items-start justify-center gap-8 px-5 pb-5">
      {sections.map((section) => {
        const nodes = layout.nodes.filter((n) => n.tree === section.id)
        if (!nodes.length) return null
        return (
          <div key={section.id} className="min-w-0">
            <div className="mb-3 text-center">
              <div className="text-[12.5px] font-medium text-ink-primary">{section.label}</div>
              <div className="text-[11.5px] tabular-nums text-ink-tertiary">
                {section.points} points
              </div>
            </div>
            <TreeGrid nodes={nodes} selected={selected} color={color} />
          </div>
        )
      })}
    </div>
  )
}

/** The table twin every chart on this site carries, here as the list of what is taken. */
export function TalentList({
  layout,
  build,
}: {
  layout: TalentTreeLayout
  build: TalentTreeBuild
}) {
  const byId = new Map(layout.nodes.map((n) => [n.id, n]))
  const rows = build.selected
    .map((pick) => {
      const node = byId.get(pick.id)
      if (!node) return null
      const entry = node.entries.find((e) => e.id === pick.entry) ?? node.entries[0]
      if (!entry) return null
      return { key: `${pick.id}-${pick.entry}`, tree: node.tree, name: entry.name, rank: pick.rank, max: node.maxRanks }
    })
    .filter((row): row is NonNullable<typeof row> => row !== null)

  const label: Record<number, string> = {
    [TREE_CLASS]: 'Class',
    [TREE_SPEC]: 'Spec',
    [TREE_HERO]: build.heroTalent ?? 'Hero',
  }

  return (
    <table className="w-full text-[12.5px]">
      <caption className="sr-only">Talents this build takes</caption>
      <thead>
        <tr className="border-b border-hairline text-left text-ink-tertiary">
          <th scope="col" className="px-5 py-2 font-medium">Tree</th>
          <th scope="col" className="px-5 py-2 font-medium">Talent</th>
          <th scope="col" className="px-5 py-2 text-right font-medium">Ranks</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key} className="border-b border-hairline/50">
            <td className="px-5 py-1.5 text-ink-tertiary">{label[row.tree] ?? row.tree}</td>
            <td className="px-5 py-1.5 text-ink-secondary">{row.name}</td>
            <td className="px-5 py-1.5 text-right tabular-nums text-ink-secondary">
              {row.rank}/{row.max}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
