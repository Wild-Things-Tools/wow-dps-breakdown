/**
 * Shared chart chrome.
 *
 * Grid and axes stay recessive, marks stay thin, and every chart gets a hover
 * layer -- a crosshair plus tooltip on lines, a per-mark tooltip on bars.
 */

import type { ReactNode } from 'react'
import { Dot } from './ui'

export const AXIS_TICK = {
  fill: 'var(--text-muted)',
  fontSize: 11.5,
} as const

export const AXIS_LINE = { stroke: 'var(--baseline)' } as const

export const GRID = {
  stroke: 'var(--gridline)',
  strokeDasharray: '0',
  vertical: false,
} as const

export const CURSOR_LINE = {
  stroke: 'var(--baseline)',
  strokeWidth: 1,
} as const

export const CURSOR_FILL = { fill: 'var(--elevated)' } as const

/** 2px surface gap between adjacent fills, per the mark spec. */
export const BAR_GAP = 2

export interface TooltipRow {
  id: string
  label: string
  color?: string
  value: ReactNode
  hint?: ReactNode
}

export function TooltipCard({
  title,
  subtitle,
  rows,
}: {
  title: ReactNode
  subtitle?: ReactNode
  rows: TooltipRow[]
}) {
  return (
    <div className="pointer-events-none min-w-44 rounded-lg border border-hairline bg-surface px-3 py-2.5 shadow-lg">
      <div className="text-[12.5px] font-semibold text-ink">{title}</div>
      {subtitle ? <div className="mt-0.5 text-[11.5px] text-ink-muted">{subtitle}</div> : null}
      <ul className="mt-2 space-y-1">
        {rows.map((row) => (
          <li key={row.id} className="flex items-baseline justify-between gap-4 text-[12.5px]">
            <span className="flex items-center gap-1.5 text-ink-secondary">
              {row.color ? <Dot color={row.color} /> : null}
              {row.label}
            </span>
            <span className="tnum font-medium text-ink">{row.value}</span>
          </li>
        ))}
      </ul>
      {rows.some((row) => row.hint) ? (
        <ul className="mt-1.5 space-y-0.5 border-t border-hairline pt-1.5">
          {rows
            .filter((row) => row.hint)
            .map((row) => (
              <li key={`${row.id}-hint`} className="text-[11.5px] text-ink-muted">
                {row.hint}
              </li>
            ))}
        </ul>
      ) : null}
    </div>
  )
}

/**
 * Direct label drawn at the end of a line.
 *
 * Not decoration: three of the light-mode series colours sit below 3:1 against
 * the light surface, so the palette's relief rule requires visible labels (or a
 * table view -- this app ships both). Labels are drawn in secondary ink rather
 * than the series colour, since text wears text tokens; the line arriving at the
 * label carries the identity.
 */
/** Vertical space one label needs before it starts touching its neighbour. */
const LABEL_MIN_GAP = 15

/**
 * Nudge end labels apart so converging lines do not stack their labels on top of
 * each other.
 *
 * Resolved from the data rather than from measured geometry: the caller knows
 * every series' final value, the y-domain and the plot height, which is enough to
 * estimate each label's y. Only the *difference* between the estimate and the
 * collision-free position is used, so a constant error in the estimate cancels
 * out and the label still lands on its own line.
 */
export function resolveLabelOffsets(
  series: Array<{ id: string; value: number }>,
  [min, max]: [number, number],
  plotHeight: number,
  minGap = LABEL_MIN_GAP,
): Map<string, number> {
  const span = max - min
  const offsets = new Map<string, number>()
  if (span <= 0 || plotHeight <= 0) return offsets

  const estimate = (value: number) => plotHeight * (1 - (value - min) / span)

  // Highest line first, then push each subsequent label down far enough to clear
  // the one above it.
  const sorted = [...series].sort((a, b) => b.value - a.value)
  let previous = Number.NEGATIVE_INFINITY

  for (const entry of sorted) {
    const wanted = estimate(entry.value)
    const placed = Math.max(wanted, previous + minGap)
    offsets.set(entry.id, placed - wanted)
    previous = placed
  }

  return offsets
}

export function makeEndLabel(text: string, lastIndex: number, dy = 0) {
  // Recharts types the label renderer's coordinates as `string | number`, so they
  // are narrowed here rather than asserted.
  return function EndLabel(props: {
    x?: string | number
    y?: string | number
    index?: number
  }) {
    const { x, y, index } = props
    if (typeof x !== 'number' || typeof y !== 'number') return null
    if (index !== lastIndex) return null
    return (
      <text
        x={x + 8}
        y={y + dy}
        dy={4}
        fill="var(--text-secondary)"
        fontSize={11.5}
        fontWeight={500}
      >
        {text}
      </text>
    )
  }
}

/** Widest label the reserved right margin fits without clipping. */
const LABEL_MAX = 18

/**
 * Shorten a build name for an in-chart label. The legend below carries the full
 * name, so the label only has to disambiguate -- and it has to fit the margin
 * reserved for it, or it gets clipped at the plot edge.
 */
export function shortLabel(displayName: string): string {
  const match = /^(.*?)\s*\(([^)]+)\)\s*$/.exec(displayName)
  const label = match
    ? (() => {
        const [, base = '', hero = ''] = match
        const spec = base.split(' ')[0] ?? base
        return hero === 'Default' ? spec : `${spec} · ${hero}`
      })()
    : displayName

  return label.length > LABEL_MAX ? `${label.slice(0, LABEL_MAX - 1).trimEnd()}…` : label
}
