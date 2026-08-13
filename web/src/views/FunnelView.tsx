/**
 * Funnel: how much of a build's damage lands on the main target.
 *
 * The underlying number is SimulationCraft's own priority-target damage
 * accounting -- every damage event aimed at the primary target is accumulated
 * separately, so `priorityDps / dps` is a measured share, not an estimate.
 *
 * Two framings, because the raw share is misleading on its own: at ten targets a
 * 30% share sounds low but is three times what an even spread would give. The
 * index normalises by target count so 1.0 always means "spread evenly" and N
 * always means "everything on the main target", whatever N is.
 */

import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  AXIS_LINE,
  AXIS_TICK,
  CURSOR_FILL,
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
import { compactNumber, describeFunnel, fullNumber, percent } from '../lib/format'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

const MAIN_COLOR = 'var(--series-1)'
const REST_COLOR = 'var(--baseline)'

/** Plot area of the index chart: its 340px height less margins and the x-axis. */
const CHART_PLOT_HEIGHT = 280

export function FunnelView({
  details,
  scenario,
  colorOf,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
  colorOf: (id: string) => string
}) {
  const funnelTargets = useMemo(
    () => scenario.targetCounts.filter((count) => count > 1),
    [scenario],
  )
  const [splitAt, setSplitAt] = useState(5)
  const effectiveSplit = funnelTargets.includes(splitAt) ? splitAt : (funnelTargets[0] ?? 2)

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Funnel" subtitle={FUNNEL_EXPLAINER} />
        <EmptyState>
          Pick a build above to see how much of its damage it can aim at the main target.
        </EmptyState>
      </Panel>
    )
  }

  if (!scenario.supportsFunnel || funnelTargets.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Funnel" subtitle={FUNNEL_EXPLAINER} />
        <EmptyState>
          {scenario.label} does not produce a comparable main-target share — SimulationCraft
          counts priority damage against bosses rather than the primary target in this fight
          style. Switch to Patchwerk for the clean measurement.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader title="How concentrated is the damage?" subtitle={FUNNEL_EXPLAINER} />
        <div className="grid gap-3 px-5 pb-5 sm:grid-cols-3">
          <StatTile
            label="Index 1.0"
            value="Even"
            caption="Every target takes the same damage. Pure area damage looks like this."
          />
          <StatTile
            label={`Index ${effectiveSplit}.0`}
            value="All on main"
            caption="Nothing lands on the other targets at all — a build with no area damage."
          />
          <StatTile
            label="In between"
            value="Funnel"
            caption="The main target takes more than its share. The higher the index, the more the build concentrates."
          />
        </div>
      </Panel>

      <Panel>
        <PanelHeader
          title="Concentration as targets are added"
          subtitle="Above the flat line, the main target is taking more than an even share."
        />
        <FunnelIndexChart details={details} scenarioId={scenario.id} colorOf={colorOf} />
        <Legend
          items={details.map((detail) => ({
            id: detail.id,
            label: detail.displayName,
            color: colorOf(detail.id),
          }))}
        />
      </Panel>

      <Panel>
        <PanelHeader
          title={`Where the damage lands at ${effectiveSplit} targets`}
          subtitle="Damage per second split between the main target and everything else."
          actions={
            <Select
              label="Targets"
              value={effectiveSplit}
              onChange={setSplitAt}
              options={funnelTargets.map((count) => ({
                value: count,
                label: String(count),
              }))}
            />
          }
        />
        <SplitChart details={details} scenarioId={scenario.id} targets={effectiveSplit} />
        <Legend
          items={[
            { id: 'main', label: 'Main target', color: MAIN_COLOR },
            { id: 'rest', label: 'All other targets', color: REST_COLOR },
          ]}
        />
        <SplitTable details={details} scenarioId={scenario.id} targets={effectiveSplit} />
      </Panel>
    </div>
  )
}

const FUNNEL_EXPLAINER =
  'Two builds can do identical total damage and still be worth very different amounts, depending on whether that damage lands on the target that matters. The funnel index answers that: 1.0 means the damage is spread evenly across every target, and the target count itself means all of it lands on the main target.'

function FunnelIndexChart({
  details,
  scenarioId,
  colorOf,
}: {
  details: SpecDetail[]
  scenarioId: string
  colorOf: (id: string) => string
}) {
  const data = useMemo(() => {
    const byTargets = new Map<number, Record<string, number>>()
    for (const detail of details) {
      for (const cell of detail.scenarios[scenarioId]?.targets ?? []) {
        if (cell.funnelIndex === undefined) continue
        let row = byTargets.get(cell.targets)
        if (!row) {
          row = { targets: cell.targets }
          byTargets.set(cell.targets, row)
        }
        row[detail.id] = cell.funnelIndex
      }
    }
    return [...byTargets.values()].sort((a, b) => (a.targets ?? 0) - (b.targets ?? 0))
  }, [details, scenarioId])

  // Lines converge as target counts rise, so their end labels need pushing apart.
  const labelOffsets = useMemo(() => {
    const last = data[data.length - 1]
    if (!last) return new Map<string, number>()
    const finals = details
      .map((detail) => ({ id: detail.id, value: last[detail.id] ?? 0 }))
      .filter((entry) => entry.value > 0)
    const max = Math.max(...finals.map((entry) => entry.value), 1)
    return resolveLabelOffsets(finals, [1, max], CHART_PLOT_HEIGHT)
  }, [data, details])

  if (data.length === 0) {
    return <EmptyState>No funnel data for this scenario yet.</EmptyState>
  }

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={340}>
        <LineChart data={data} margin={{ top: 8, right: 130, bottom: 8, left: 8 }}>
          <CartesianGrid {...GRID} />
          <XAxis
            dataKey="targets"
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            label={{
              value: 'Targets',
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
            width={46}
            domain={[1, 'auto']}
            tickFormatter={(value: number) => `${value.toFixed(1)}x`}
          />
          <ReferenceLine
            y={1}
            stroke="var(--baseline)"
            strokeDasharray="4 4"
            label={{
              value: 'Even spread',
              position: 'insideBottomRight',
              fill: 'var(--text-muted)',
              fontSize: 11,
            }}
          />
          <Tooltip
            cursor={CURSOR_LINE}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null
              const targets = Number(label)
              const sorted = [...payload].sort(
                (a, b) => Number(b.value ?? 0) - Number(a.value ?? 0),
              )
              return (
                <TooltipCard
                  title={`${targets} targets`}
                  subtitle={`Even spread would be 1.0x · all on main would be ${targets}.0x`}
                  rows={sorted.map((entry) => {
                    const index = Number(entry.value ?? 0)
                    return {
                      id: String(entry.dataKey),
                      label: String(entry.name),
                      color: entry.color,
                      value: `${index.toFixed(2)}x`,
                      hint: `${entry.name}: ${percent(index / targets)} on main target — ${describeFunnel(index, targets)}`,
                    }
                  })}
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
              strokeWidth={2}
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
  )
}

interface SplitRow {
  id: string
  label: string
  main: number
  rest: number
  total: number
  share: number
  index: number
}

function splitRows(details: SpecDetail[], scenarioId: string, targets: number): SplitRow[] {
  const rows: SplitRow[] = []
  for (const detail of details) {
    const cell = detail.scenarios[scenarioId]?.targets.find((entry) => entry.targets === targets)
    if (!cell || cell.priorityDps === undefined || cell.funnelIndex === undefined) continue
    rows.push({
      id: detail.id,
      label: detail.displayName,
      main: cell.priorityDps,
      rest: Math.max(0, cell.dps - cell.priorityDps),
      total: cell.dps,
      share: cell.funnelShare ?? 0,
      index: cell.funnelIndex,
    })
  }
  return rows.sort((a, b) => b.main - a.main)
}

function SplitChart({
  details,
  scenarioId,
  targets,
}: {
  details: SpecDetail[]
  scenarioId: string
  targets: number
}) {
  const rows = useMemo(
    () => splitRows(details, scenarioId, targets),
    [details, scenarioId, targets],
  )

  if (rows.length === 0) {
    return <EmptyState>No split data at {targets} targets for these builds.</EmptyState>
  }

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={Math.max(200, rows.length * 44 + 50)}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 24, bottom: 4, left: 8 }}
        >
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={compactNumber}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={210}
            tick={AXIS_TICK}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as SplitRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  subtitle={describeFunnel(row.index, targets)}
                  rows={[
                    { id: 'main', label: 'Main target', color: MAIN_COLOR, value: fullNumber(row.main) },
                    { id: 'rest', label: 'Other targets', color: REST_COLOR, value: fullNumber(row.rest) },
                    { id: 'total', label: 'Total', value: fullNumber(row.total) },
                    {
                      id: 'share',
                      label: 'Main-target share',
                      value: percent(row.share),
                      hint: `An even spread across ${targets} targets would be ${percent(1 / targets, 0)}.`,
                    },
                  ]}
                />
              )
            }}
          />
          {/* 2px surface gap between the stacked segments, per the mark spec. */}
          <Bar
            dataKey="main"
            stackId="dps"
            fill={MAIN_COLOR}
            barSize={18}
            isAnimationActive={false}
            stroke="var(--surface-1)"
            strokeWidth={1}
          />
          <Bar
            dataKey="rest"
            stackId="dps"
            fill={REST_COLOR}
            barSize={18}
            radius={[0, 4, 4, 0]}
            isAnimationActive={false}
            stroke="var(--surface-1)"
            strokeWidth={1}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function SplitTable({
  details,
  scenarioId,
  targets,
}: {
  details: SpecDetail[]
  scenarioId: string
  targets: number
}) {
  const rows = useMemo(
    () => splitRows(details, scenarioId, targets),
    [details, scenarioId, targets],
  )
  if (rows.length === 0) return null

  const even = 1 / targets

  return (
    <>
      <div className="overflow-x-auto px-1 pb-2">
        <table className="w-full min-w-[640px] border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              <th scope="col" className="px-4 py-2.5 font-medium">
                Build
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Main target
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Other targets
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Share on main
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Index
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Reading
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-hairline/60 last:border-0">
                <td className="px-4 py-2 text-ink">{row.label}</td>
                <td className="tnum px-4 py-2 text-right font-medium text-ink">
                  {fullNumber(row.main)}
                </td>
                <td className="tnum px-4 py-2 text-right text-ink-secondary">
                  {fullNumber(row.rest)}
                </td>
                <td className="tnum px-4 py-2 text-right text-ink-secondary">
                  {percent(row.share)}
                </td>
                <td className="tnum px-4 py-2 text-right font-medium text-ink">
                  {row.index.toFixed(2)}x
                </td>
                <td className="px-4 py-2 text-ink-secondary">
                  {describeFunnel(row.index, targets)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Note>
        With {targets} targets, an even spread puts {percent(even, 0)} of the damage on the
        main target. Anything above that is the build concentrating — from extra single-target
        abilities, damage-over-time spread mechanics, or pets and procs that stay on the
        primary target.
      </Note>
    </>
  )
}
