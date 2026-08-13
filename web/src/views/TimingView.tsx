/**
 * Timing: when during a fight the damage actually happens.
 *
 * The curve is SimulationCraft's damage timeline, averaged across iterations and
 * cut at the shortest simulated fight -- past that point only the long
 * iterations contribute and the mean stops being comparable.
 *
 * Fight length is randomised per iteration, so cooldown windows land at slightly
 * different times and the averaged curve smooths them out. Read the shape (a big
 * opener, a mid-fight bump, a flat line) rather than the exact position of a peak.
 */

import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  AXIS_LINE,
  AXIS_TICK,
  CURSOR_LINE,
  GRID,
  TooltipCard,
  makeEndLabel,
  resolveLabelOffsets,
  shortLabel,
} from '../components/chart'
import {
  EmptyState,
  Legend,
  Note,
  Panel,
  PanelHeader,
  Select,
  StatTile,
} from '../components/ui'
import { compactNumber, describeBurst, fullNumber } from '../lib/format'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

export function TimingView({
  details,
  scenario,
  colorOf,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
  colorOf: (id: string) => string
}) {
  const available = useMemo(() => {
    const counts = new Set<number>()
    for (const detail of details) {
      for (const cell of detail.scenarios[scenario.id]?.targets ?? []) {
        if (cell.timeline?.length) counts.add(cell.targets)
      }
    }
    return [...counts].sort((a, b) => a - b)
  }, [details, scenario.id])

  const [targets, setTargets] = useState(1)
  const effective = available.includes(targets) ? targets : (available[0] ?? 1)

  const { data, burst } = useMemo(
    () => buildTimeline(details, scenario.id, effective),
    [details, scenario.id, effective],
  )

  // Damage curves all settle into the same band by the end of the fight, so their
  // end labels land almost on top of each other without this.
  const labelOffsets = useMemo(() => {
    const last = data[data.length - 1]
    if (!last) return new Map<string, number>()
    const finals = details
      .map((detail) => ({ id: detail.id, value: last[detail.id] ?? 0 }))
      .filter((entry) => entry.value > 0)
    const peak = data.reduce(
      (best, row) =>
        Math.max(best, ...Object.entries(row).filter(([k]) => k !== 'second').map(([, v]) => v)),
      0,
    )
    return resolveLabelOffsets(finals, [0, peak || 1], PLOT_HEIGHT)
  }, [data, details])

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Timing" subtitle={TIMING_EXPLAINER} />
        <EmptyState>Pick a build above to see when its damage lands.</EmptyState>
      </Panel>
    )
  }

  if (data.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Timing" subtitle={TIMING_EXPLAINER} />
        <EmptyState>
          No timeline recorded for these builds in {scenario.label}. Timelines are kept for a
          few representative target counts to keep the dataset small.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Burst versus sustain"
          subtitle="How much the best twenty seconds beat the build's own average. A steady build sits near 1.0; a cooldown-driven one climbs well above it."
        />
        <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-3">
          {burst.map((entry) => (
            <StatTile
              key={entry.id}
              accent={colorOf(entry.id)}
              label={entry.label}
              value={`${entry.ratio.toFixed(2)}x`}
              caption={describeBurst(entry.ratio)}
            />
          ))}
        </div>
      </Panel>

      <Panel>
        <PanelHeader
          title="Damage over the course of the fight"
          subtitle={TIMING_EXPLAINER}
          actions={
            available.length > 1 ? (
              <Select
                label="Targets"
                value={effective}
                onChange={setTargets}
                options={available.map((count) => ({ value: count, label: String(count) }))}
              />
            ) : null
          }
        />

        <div className="px-2 py-4">
          <ResponsiveContainer width="100%" height={360}>
            <LineChart data={data} margin={{ top: 8, right: 130, bottom: 8, left: 8 }}>
              <CartesianGrid {...GRID} />
              <XAxis
                dataKey="second"
                type="number"
                domain={['dataMin', 'dataMax']}
                tick={AXIS_TICK}
                axisLine={AXIS_LINE}
                tickLine={false}
                tickFormatter={(value: number) => `${Math.round(value)}s`}
                label={{
                  value: 'Seconds into the fight',
                  position: 'insideBottom',
                  offset: -4,
                  fill: 'var(--text-muted)',
                  fontSize: 11.5,
                }}
              />
              <YAxis
                tick={AXIS_TICK}
                axisLine={false}
                tickLine={false}
                width={56}
                tickFormatter={compactNumber}
              />
              <Tooltip
                cursor={CURSOR_LINE}
                content={({ active, payload, label }) => {
                  if (!active || !payload?.length) return null
                  const sorted = [...payload].sort(
                    (a, b) => Number(b.value ?? 0) - Number(a.value ?? 0),
                  )
                  return (
                    <TooltipCard
                      title={`${Math.round(Number(label))}s into the fight`}
                      rows={sorted.map((entry) => ({
                        id: String(entry.dataKey),
                        label: String(entry.name),
                        color: entry.color,
                        value: fullNumber(Number(entry.value ?? 0)),
                      }))}
                    />
                  )
                }}
              />
              {details.map((detail) => (
                <Line
                  key={detail.id}
                  type="monotone"
                  dataKey={detail.id}
                  name={detail.displayName}
                  stroke={colorOf(detail.id)}
                  // Thinner than the other charts: these curves overlap heavily and
                  // 2px strokes turn the busy middle of the fight into a smear.
                  strokeWidth={1.5}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
                  isAnimationActive={false}
                  label={makeEndLabel(
                    shortLabel(detail.displayName),
                    data.length - 1,
                    labelOffsets.get(detail.id) ?? 0,
                  )}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>

        <Legend
          items={details
            .filter((detail) => data.some((row) => row[detail.id] !== undefined))
            .map((detail) => ({
              id: detail.id,
              label: detail.displayName,
              color: colorOf(detail.id),
            }))}
        />

        <Note>
          Averaged across every simulated pull, so a cooldown that fires at slightly
          different times shows up as a broad bump rather than a spike. The opening seconds
          are the most reliable part of the curve — that is where every pull is doing the
          same thing.
        </Note>
      </Panel>
    </div>
  )
}

/** Plot area of the chart: its 360px height less margins and the x-axis. */
const PLOT_HEIGHT = 300

const TIMING_EXPLAINER =
  'Damage per second at each point of the fight, averaged over thousands of simulated pulls.'

function buildTimeline(details: SpecDetail[], scenarioId: string, targets: number) {
  const rows = new Map<number, Record<string, number>>()
  const burst: Array<{ id: string; label: string; ratio: number }> = []

  for (const detail of details) {
    const cell = detail.scenarios[scenarioId]?.targets.find((entry) => entry.targets === targets)
    if (!cell?.timeline?.length) continue

    const bin = cell.timelineBin ?? 1
    cell.timeline.forEach((value, index) => {
      const second = index * bin
      let row = rows.get(second)
      if (!row) {
        row = { second }
        rows.set(second, row)
      }
      row[detail.id] = value
    })

    if (cell.burstRatio !== undefined) {
      burst.push({ id: detail.id, label: detail.displayName, ratio: cell.burstRatio })
    }
  }

  const data = [...rows.values()].sort((a, b) => (a.second ?? 0) - (b.second ?? 0))
  burst.sort((a, b) => b.ratio - a.ratio)
  return { data, burst }
}
