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
 *
 * Form: a ranked bar for the headline number, small multiples for the curves.
 * Twenty-six damage curves overlap almost completely in the middle of a fight, so
 * one plot with everything on it is a smear whatever colours it uses -- and with
 * no picker there is no smaller set to fall back on.
 */

import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell as RBCell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AXIS_LINE, AXIS_TICK, CURSOR_FILL, GRID, TooltipCard } from '../components/chart'
import { BuildIdentity, makeBuildTick } from '../components/BuildIdentity'
import { SmallMultiples, type SparkPanel } from '../components/SmallMultiples'
import { EmptyState, Note, Panel, PanelHeader, Select } from '../components/ui'
import { compactNumber, describeBurst, fullNumber } from '../lib/format'
import { classColor } from '../lib/palette'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

const TIMING_EXPLAINER =
  'Damage per second at each point of the fight, averaged over thousands of simulated pulls.'

const ROW_HEIGHT = 32
const TICK_WIDTH = 205

export function TimingView({
  details,
  scenario,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
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

  const burst = useMemo(
    () => burstRows(details, scenario.id, effective),
    [details, scenario.id, effective],
  )
  const panels = useMemo(
    () => timelinePanels(details, scenario.id, effective),
    [details, scenario.id, effective],
  )

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Timing" subtitle={TIMING_EXPLAINER} />
        <EmptyState>No per-build data has been generated for this tier yet.</EmptyState>
      </Panel>
    )
  }

  if (panels.length === 0 && burst.length === 0) {
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

  const targetSelect =
    available.length > 1 ? (
      <Select
        label="Targets"
        value={effective}
        onChange={setTargets}
        options={available.map((count) => ({ value: count, label: String(count) }))}
      />
    ) : null

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Burst versus sustain"
          subtitle="How much the best twenty seconds beat the build's own average. Bars grow from 1.0 — a build sitting on the line is perfectly steady, one far to the right is cooldown-driven."
          actions={targetSelect}
        />
        {burst.length === 0 ? (
          <EmptyState>No burst measurement at this target count.</EmptyState>
        ) : (
          <>
            <BurstChart rows={burst} />
            <BurstTable rows={burst} />
          </>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Damage over the course of the fight"
          subtitle={TIMING_EXPLAINER}
          actions={targetSelect}
        />
        {panels.length === 0 ? (
          <EmptyState>No timeline recorded at this target count.</EmptyState>
        ) : (
          <SmallMultiples
            panels={panels}
            height={78}
            formatX={(value) => `${Math.round(value)}s into the fight`}
            formatY={(value) => fullNumber(value)}
            referenceLabel="Every panel shares one axis, so panel heights compare directly. The faint curve behind each is the median build at that second."
          />
        )}
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

// --------------------------------------------------------------------------------
// Burst
// --------------------------------------------------------------------------------

interface BurstRow {
  build: SpecDetail
  label: string
  ratio: number
  /** ratio - 1, so the bar grows from "perfectly flat" rather than from zero. */
  delta: number
}

function burstRows(details: SpecDetail[], scenarioId: string, targets: number): BurstRow[] {
  const rows: BurstRow[] = []
  for (const detail of details) {
    const cell = detail.scenarios[scenarioId]?.targets.find((entry) => entry.targets === targets)
    if (cell?.burstRatio === undefined) continue
    rows.push({
      build: detail,
      label: detail.displayName,
      ratio: cell.burstRatio,
      delta: cell.burstRatio - 1,
    })
  }
  return rows.sort((a, b) => b.ratio - a.ratio)
}

function BurstChart({ rows }: { rows: BurstRow[] }) {
  const byLabel = useMemo(() => new Map(rows.map((row) => [row.label, row.build])), [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: TICK_WIDTH }), [byLabel])
  const span = Math.max(...rows.map((row) => row.delta), 0.1) * 1.08

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={Math.max(220, rows.length * ROW_HEIGHT + 50)}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 40, bottom: 4, left: 8 }}
          barCategoryGap={4}
        >
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            domain={[0, span]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => `${(1 + value).toFixed(2)}x`}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as BurstRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  rows={[
                    {
                      id: 'burst',
                      label: 'Peak 20s vs average',
                      color: classColor(row.build.class),
                      value: `${row.ratio.toFixed(2)}x`,
                      hint: describeBurst(row.ratio),
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="delta" barSize={14} radius={[0, 2, 2, 0]} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell key={row.build.id} fill={classColor(row.build.class)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function BurstTable({ rows }: { rows: BurstRow[] }) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[560px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">
              Build
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              Peak 20s ÷ average
            </th>
            <th scope="col" className="py-2.5 pr-5 font-medium">
              Reading
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.build.id} className="border-b border-hairline/60 last:border-0">
              <td className="py-2 pr-4 pl-5">
                <BuildIdentity build={row.build} />
              </td>
              <td className="tnum py-2 pr-4 text-right font-medium text-ink">
                {row.ratio.toFixed(2)}x
              </td>
              <td className="py-2 pr-5 text-ink-secondary">{describeBurst(row.ratio)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Timelines
// --------------------------------------------------------------------------------

function timelinePanels(
  details: SpecDetail[],
  scenarioId: string,
  targets: number,
): SparkPanel[] {
  const panels: SparkPanel[] = []
  for (const detail of details) {
    const cell = detail.scenarios[scenarioId]?.targets.find((entry) => entry.targets === targets)
    if (!cell?.timeline?.length) continue
    const bin = cell.timelineBin ?? 1
    const points = cell.timeline.map((value, index) => ({ x: index * bin, y: value }))
    const peak = Math.max(...points.map((point) => point.y))
    panels.push({
      build: detail,
      points,
      headline: compactNumber(cell.dps),
      caption: `Peaks at ${compactNumber(peak)}${
        cell.burstRatio !== undefined ? ` · ${describeBurst(cell.burstRatio)}` : ''
      }`,
    })
  }
  return panels.sort((a, b) => {
    const av = Math.max(...a.points.map((point) => point.y))
    const bv = Math.max(...b.points.map((point) => point.y))
    return bv - av
  })
}
