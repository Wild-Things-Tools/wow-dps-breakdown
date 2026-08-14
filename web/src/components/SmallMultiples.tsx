/**
 * Small multiples: one tiny chart per build, all on the same scale.
 *
 * This is the form that replaced "pick up to six builds, then draw six lines".
 * Twenty-six lines on one plot is unreadable and six lines behind a picker is
 * unreadable until you have already chosen -- faceting is the honest answer for
 * this many series, and it is what the dataviz method says to reach for past
 * about four converging lines.
 *
 * Two things make the grid comparable rather than decorative:
 *
 *  - **One shared y domain** across every panel, so the curves can be read
 *    against each other rather than each against its own private scale.
 *  - **A context curve** drawn faint in every panel: the median build at each x.
 *    It is what turns "this line goes up" into "this line goes up more than most
 *    of them do".
 *
 * Colour is the class colour, and the panel header carries the spec icon, the
 * spec name and the hero tree in words, so the identity of a panel never rests
 * on its hue.
 */

import { type ReactNode, useMemo } from 'react'
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import { TooltipCard } from './chart'
import { BuildIdentity, type BuildLike } from './BuildIdentity'
import { classColor } from '../lib/palette'

export interface SparkPanel {
  build: BuildLike
  /** Ascending in x. Panels may be sparse; missing x values simply have no point. */
  points: Array<{ x: number; y: number }>
  /** The number that orders the grid, printed on the panel. */
  headline: ReactNode
  caption?: ReactNode
}

export function SmallMultiples({
  panels,
  formatX,
  formatY,
  referenceY,
  referenceLabel,
  height = 74,
}: {
  panels: SparkPanel[]
  formatX: (value: number) => string
  formatY: (value: number) => string
  /** A meaningful horizontal line, e.g. 1.0 for a ratio. */
  referenceY?: number
  referenceLabel?: string
  height?: number
}) {
  const { domain, context } = useMemo(() => {
    const values = panels.flatMap((panel) => panel.points.map((point) => point.y))
    if (values.length === 0) {
      return { domain: [0, 1] as [number, number], context: [] as Array<{ x: number; y: number }> }
    }
    let low = Math.min(...values)
    let high = Math.max(...values)
    if (referenceY !== undefined) {
      low = Math.min(low, referenceY)
      high = Math.max(high, referenceY)
    }
    // Ratio charts read from their reference line; magnitude charts read from zero.
    const floor = referenceY === undefined ? 0 : low - (high - low) * 0.12
    const pad = (high - floor) * 0.08

    // The median build at each x, as background context in every panel.
    const byX = new Map<number, number[]>()
    for (const panel of panels)
      for (const point of panel.points) {
        const bucket = byX.get(point.x)
        if (bucket) bucket.push(point.y)
        else byX.set(point.x, [point.y])
      }
    const median = [...byX.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([x, ys]) => {
        const sorted = [...ys].sort((a, b) => a - b)
        const middle = Math.floor(sorted.length / 2)
        return {
          x,
          y:
            sorted.length % 2
              ? (sorted[middle] ?? 0)
              : ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2,
        }
      })

    return { domain: [floor, high + pad] as [number, number], context: median }
  }, [panels, referenceY])

  return (
    <div className="grid gap-x-4 gap-y-3 px-5 pb-5 sm:grid-cols-2 xl:grid-cols-3">
      {panels.map((panel) => (
        <figure key={panel.build.id} className="rounded-lg border border-hairline px-3 pt-2.5 pb-1">
          <figcaption className="flex items-start justify-between gap-2">
            <BuildIdentity build={panel.build} size={18} />
            <span className="tnum shrink-0 pt-0.5 text-[13px] font-semibold text-ink">
              {panel.headline}
            </span>
          </figcaption>
          <ResponsiveContainer width="100%" height={height}>
            {/* Deliberately not `syncId`-linked: twenty-six synchronised tooltips
                is twenty-six cards on screen at once, which is worse than none. */}
            <LineChart data={panel.points} margin={{ top: 6, right: 2, bottom: 2, left: 2 }}>
              <YAxis hide domain={domain} />
              {referenceY !== undefined ? (
                <ReferenceLine y={referenceY} stroke="var(--baseline)" strokeDasharray="3 3" />
              ) : null}
              {context.length > 1 ? (
                <Line
                  data={context}
                  dataKey="y"
                  type="monotone"
                  stroke="var(--text-muted)"
                  strokeWidth={1}
                  strokeOpacity={0.45}
                  dot={false}
                  isAnimationActive={false}
                  legendType="none"
                />
              ) : null}
              <Line
                dataKey="y"
                type="monotone"
                stroke={classColor(panel.build.class)}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 3.5, strokeWidth: 2, stroke: 'var(--surface-1)' }}
                isAnimationActive={false}
              />
              <Tooltip
                cursor={{ stroke: 'var(--baseline)', strokeWidth: 1 }}
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null
                  const point = payload[0]?.payload as { x?: number; y?: number } | undefined
                  if (!point || point.x === undefined || point.y === undefined) return null
                  return (
                    <TooltipCard
                      title={formatX(point.x)}
                      subtitle={panel.build.displayName}
                      rows={[
                        {
                          id: 'value',
                          label: 'Value',
                          color: classColor(panel.build.class),
                          value: formatY(point.y),
                        },
                      ]}
                    />
                  )
                }}
              />
            </LineChart>
          </ResponsiveContainer>
          {panel.caption ? (
            <p className="pb-1 text-[11.5px] leading-snug text-ink-muted">{panel.caption}</p>
          ) : null}
        </figure>
      ))}
      {referenceLabel ? (
        <p className="col-span-full text-[11.5px] text-ink-muted">{referenceLabel}</p>
      ) : null}
    </div>
  )
}
