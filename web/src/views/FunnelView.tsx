/**
 * Funnel: does the main target take more damage because the other targets exist?
 *
 * Two distinct numbers live here, and keeping them apart is the whole point of the
 * view:
 *
 * **Funnel gain** -- main-target damage at N targets divided by damage at one
 * target. Above 1.0 the adds are feeding the priority target (resources from
 * damage-over-time effects, procs); below 1.0 the global cooldowns spent on area
 * damage are costing it. This is what players mean by funnelling.
 *
 * **Concentration** -- how the damage is distributed across the targets present.
 * 1.0 is an even spread, N is everything on the main target. Useful, but a spec with
 * no area damage scores high here while gaining nothing from the adds.
 *
 * Both come from simc's `prioritydps`, which is measured rather than estimated.
 */

import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell as RBCell,
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
  SegmentedControl,
  Select,
  StatTile,
} from '../components/ui'
import {
  compactNumber,
  describeConcentration,
  describeFunnelGain,
  fullNumber,
  percent,
} from '../lib/format'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

const MAIN_COLOR = 'var(--series-1)'
const REST_COLOR = 'var(--baseline)'

/** Plot area of the line chart: its 340px height less margins and the x-axis. */
const CHART_PLOT_HEIGHT = 280

type Metric = 'gain' | 'concentration'

export function FunnelView({
  details,
  scenario,
  colorOf,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
  colorOf: (id: string) => string
}) {
  const [metric, setMetric] = useState<Metric>('gain')

  // Which scenario's single-target run the gain divides by. Scenarios whose extra
  // targets arrive from raid events have no add-free cell of their own and borrow
  // Patchwerk's; "self" means the sweep provides its own.
  const baselineId =
    scenario.funnelBaseline === 'self' ? scenario.id : (scenario.funnelBaseline ?? null)

  // A swept scenario shows funnel data from two targets up. A scenario that runs at
  // one configured target but spawns adds shows it at that single count -- simc
  // reports priority damage whenever more than one enemy exists, however it arrived.
  const sweeps = scenario.targetCounts.length > 1
  const funnelTargets = useMemo(
    () => (sweeps ? scenario.targetCounts.filter((count) => count > 1) : scenario.targetCounts),
    [scenario, sweeps],
  )
  const [splitAt, setSplitAt] = useState(5)
  const effectiveSplit = funnelTargets.includes(splitAt) ? splitAt : (funnelTargets[0] ?? 1)

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Funnel" subtitle={EXPLAINER} />
        <EmptyState>
          Pick a build above to see whether extra targets help or hurt its damage on the
          main target.
        </EmptyState>
      </Panel>
    )
  }

  if (!scenario.supportsFunnel || funnelTargets.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Funnel" subtitle={EXPLAINER} />
        <EmptyState>
          {scenario.label} does not produce a comparable main-target number —
          SimulationCraft counts priority damage against bosses rather than the primary
          target in this fight style. Switch to Patchwerk for the clean measurement.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader title="Do extra targets help the main target?" subtitle={EXPLAINER} />
        <div className="grid gap-3 px-5 pb-5 sm:grid-cols-3">
          <StatTile
            label="Gain above 1.0"
            value="Funnel"
            caption="The main target takes more damage than it would if it stood alone. Damage-over-time effects on the adds feed resources and procs back into it."
          />
          <StatTile
            label="Gain of 1.0"
            value="Neutral"
            caption="The adds neither help nor hurt. Whatever the spec does to them is on top."
          />
          <StatTile
            label="Gain below 1.0"
            value="Dilution"
            caption="Global cooldowns go into area damage, so the main target dies slower than it would alone."
          />
        </div>
      </Panel>

      <Panel>
        <PanelHeader
          title={
            metric === 'gain'
              ? sweeps
                ? 'Main-target damage as targets are added'
                : 'Main-target damage with the adds present'
              : 'How the damage is distributed'
          }
          subtitle={
            metric === 'gain'
              ? 'Relative to the same build with no adds at all. Above 1.0, having the adds up makes the main target die faster.'
              : 'Relative to an even spread across every target. This says nothing about whether the adds helped — only where the damage went.'
          }
          actions={
            <SegmentedControl
              label="Metric"
              value={metric}
              onChange={setMetric}
              options={[
                { value: 'gain', label: 'Funnel gain', title: 'Main-target DPS vs single target' },
                {
                  value: 'concentration',
                  label: 'Concentration',
                  title: 'Share on main target vs an even spread',
                },
              ]}
            />
          }
        />
        {sweeps ? (
          <MetricChart
            details={details}
            scenarioId={scenario.id}
            colorOf={colorOf}
            metric={metric}
          />
        ) : (
          <SinglePointChart
            details={details}
            scenarioId={scenario.id}
            colorOf={colorOf}
            metric={metric}
          />
        )}
        <Legend
          items={details.map((detail) => ({
            id: detail.id,
            label: detail.displayName,
            color: colorOf(detail.id),
          }))}
        />
        {metric === 'concentration' ? (
          <Note>
            A build with little area damage scores high here without the extra targets
            having done anything for it. Switch to funnel gain for that question.
          </Note>
        ) : null}
      </Panel>

      <Panel>
        <PanelHeader
          title={
            sweeps
              ? `Where the damage lands at ${effectiveSplit} targets`
              : 'Where the damage lands'
          }
          subtitle="Damage per second split between the main target and everything else."
          actions={
            funnelTargets.length > 1 ? (
              <Select
                label="Targets"
                value={effectiveSplit}
                onChange={setSplitAt}
                options={funnelTargets.map((count) => ({ value: count, label: String(count) }))}
              />
            ) : null
          }
        />
        <SplitChart
          details={details}
          scenarioId={scenario.id}
          targets={effectiveSplit}
          baselineId={baselineId}
        />
        <Legend
          items={[
            { id: 'main', label: 'Main target', color: MAIN_COLOR },
            { id: 'rest', label: 'All other targets', color: REST_COLOR },
          ]}
        />
        <SplitTable
          details={details}
          scenarioId={scenario.id}
          targets={effectiveSplit}
          baselineId={baselineId}
          fixedTargets={sweeps}
        />
      </Panel>
    </div>
  )
}

const EXPLAINER =
  'Funnelling is when the main target takes more damage because the other targets are there — damage-over-time effects on adds generating resources and procs that get spent on the priority target. That is a different question from how the damage happens to be distributed, and the two often disagree.'

interface Point {
  targets: number
  [specId: string]: number
}

function MetricChart({
  details,
  scenarioId,
  colorOf,
  metric,
}: {
  details: SpecDetail[]
  scenarioId: string
  colorOf: (id: string) => string
  metric: Metric
}) {
  const data = useMemo(() => {
    const byTargets = new Map<number, Point>()
    for (const detail of details) {
      for (const cell of detail.scenarios[scenarioId]?.targets ?? []) {
        const value = metric === 'gain' ? cell.funnelGain : cell.concentration
        if (value === undefined) continue
        let row = byTargets.get(cell.targets)
        if (!row) {
          row = { targets: cell.targets }
          byTargets.set(cell.targets, row)
        }
        row[detail.id] = value
      }
    }
    return [...byTargets.values()].sort((a, b) => a.targets - b.targets)
  }, [details, scenarioId, metric])

  const labelOffsets = useMemo(() => {
    const last = data[data.length - 1]
    if (!last) return new Map<string, number>()
    const finals = details
      .map((detail) => ({ id: detail.id, value: last[detail.id] ?? 0 }))
      .filter((entry) => entry.value > 0)
    const values = finals.map((entry) => entry.value)
    const min = metric === 'gain' ? Math.min(...values, 1) : 1
    return resolveLabelOffsets(finals, [min, Math.max(...values, min + 0.1)], CHART_PLOT_HEIGHT)
  }, [data, details, metric])

  if (data.length === 0) {
    return <EmptyState>No data for this scenario yet.</EmptyState>
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
            domain={metric === 'gain' ? ['auto', 'auto'] : [1, 'auto']}
            tickFormatter={(value: number) => `${value.toFixed(1)}x`}
          />
          {/* The meaningful zero line: 1.0 is "no effect" for gain and "even spread"
              for concentration. Either way it is where the reading flips. */}
          <ReferenceLine
            y={1}
            stroke="var(--baseline)"
            strokeDasharray="4 4"
            label={{
              value: metric === 'gain' ? 'No gain' : 'Even spread',
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
                  subtitle={
                    metric === 'gain'
                      ? 'Main-target damage vs the same build at one target'
                      : `Even spread = 1.0x · all on main = ${targets}.0x`
                  }
                  rows={sorted.map((entry) => {
                    const value = Number(entry.value ?? 0)
                    return {
                      id: String(entry.dataKey),
                      label: String(entry.name),
                      color: entry.color,
                      value: `${value.toFixed(2)}x`,
                      hint: `${entry.name}: ${
                        metric === 'gain'
                          ? describeFunnelGain(value)
                          : describeConcentration(value, targets)
                      }`,
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

/**
 * One value per build, for scenarios that run at a single target configuration.
 *
 * A line chart needs an x-axis to sweep; an add-wave scenario has one measurement,
 * so the honest form is a ranked comparison. The 1.0 reference line is what makes
 * it readable -- crossing it is the whole question.
 */
function SinglePointChart({
  details,
  scenarioId,
  colorOf,
  metric,
}: {
  details: SpecDetail[]
  scenarioId: string
  colorOf: (id: string) => string
  metric: Metric
}) {
  const rows = useMemo(() => {
    const out: Array<{ id: string; label: string; value: number }> = []
    for (const detail of details) {
      const cell = detail.scenarios[scenarioId]?.targets[0]
      const value = metric === 'gain' ? cell?.funnelGain : cell?.concentration
      if (value === undefined) continue
      out.push({ id: detail.id, label: detail.displayName, value })
    }
    return out.sort((a, b) => b.value - a.value)
  }, [details, scenarioId, metric])

  if (rows.length === 0) {
    return (
      <EmptyState>
        {metric === 'gain'
          ? 'No funnel gain for these builds in this scenario.'
          : 'Concentration needs a fixed target count, and this scenario’s adds come and go. Switch to funnel gain.'}
      </EmptyState>
    )
  }

  const min = Math.min(...rows.map((row) => row.value), 1)
  const max = Math.max(...rows.map((row) => row.value), 1)

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={Math.max(180, rows.length * 40 + 50)}>
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 40, bottom: 4, left: 8 }}>
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            domain={[Math.min(min, 0.9) - 0.05, Math.max(max, 1.1) + 0.05]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => `${value.toFixed(2)}x`}
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
              const row = payload[0]?.payload as { label: string; value: number } | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  rows={[
                    {
                      id: 'value',
                      label: metric === 'gain' ? 'Funnel gain' : 'Concentration',
                      value: `${row.value.toFixed(3)}x`,
                      hint: metric === 'gain' ? describeFunnelGain(row.value) : undefined,
                    },
                  ]}
                />
              )
            }}
          />
          <ReferenceLine
            x={1}
            stroke="var(--baseline)"
            strokeDasharray="4 4"
            label={{
              value: metric === 'gain' ? 'No gain' : 'Even',
              position: 'insideTopRight',
              fill: 'var(--text-muted)',
              fontSize: 11,
            }}
          />
          <Bar dataKey="value" barSize={16} radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell key={row.id} fill={colorOf(row.id)} />
            ))}
          </Bar>
        </BarChart>
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
  singleTarget: number
  share: number
  concentration?: number
  gain?: number
}

function splitRows(
  details: SpecDetail[],
  scenarioId: string,
  targets: number,
  baselineId: string | null,
): SplitRow[] {
  const rows: SplitRow[] = []
  for (const detail of details) {
    const cells = detail.scenarios[scenarioId]?.targets ?? []
    const cell = cells.find((entry) => entry.targets === targets)
    // The add-free reference. For a swept scenario that is its own 1-target cell;
    // for an add-wave scenario it lives in Patchwerk, because this scenario has no
    // run without adds.
    const single = baselineId
      ? (detail.scenarios[baselineId]?.targets ?? []).find((entry) => entry.targets === 1)
      : undefined
    // Concentration is absent when the target count is not fixed, which is exactly
    // the add-wave case -- so it must not gate the row.
    if (!cell || cell.priorityDps === undefined) continue
    rows.push({
      id: detail.id,
      label: detail.displayName,
      main: cell.priorityDps,
      rest: Math.max(0, cell.dps - cell.priorityDps),
      total: cell.dps,
      singleTarget: single?.dps ?? 0,
      share: cell.priorityShare ?? 0,
      concentration: cell.concentration,
      gain: cell.funnelGain,
    })
  }
  // Sorted by funnel gain, so the builds that actually benefit from adds come first.
  return rows.sort((a, b) => (b.gain ?? 0) - (a.gain ?? 0))
}

function SplitChart({
  details,
  scenarioId,
  targets,
  baselineId,
}: {
  details: SpecDetail[]
  scenarioId: string
  targets: number
  baselineId: string | null
}) {
  const rows = useMemo(
    () => splitRows(details, scenarioId, targets, baselineId),
    [details, scenarioId, targets, baselineId],
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
                  subtitle={row.gain !== undefined ? describeFunnelGain(row.gain) : undefined}
                  rows={[
                    {
                      id: 'main',
                      label: 'Main target',
                      color: MAIN_COLOR,
                      value: fullNumber(row.main),
                    },
                    {
                      id: 'rest',
                      label: 'Other targets',
                      color: REST_COLOR,
                      value: fullNumber(row.rest),
                    },
                    { id: 'total', label: 'Total', value: fullNumber(row.total) },
                    ...(row.singleTarget > 0
                      ? [
                          {
                            id: 'single',
                            label: 'Alone it would take',
                            value: fullNumber(row.singleTarget),
                            hint: `So the adds ${
                              row.main >= row.singleTarget ? 'add' : 'cost'
                            } ${fullNumber(Math.abs(row.main - row.singleTarget))} DPS on the main target.`,
                          },
                        ]
                      : []),
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
          {/* Where the main target would sit with no adds at all. Crossing to the
              right of this line is exactly what funnelling means. */}
          <ReferenceLine
            x={rows[0]?.singleTarget}
            stroke="var(--text-muted)"
            strokeDasharray="3 3"
            ifOverflow="extendDomain"
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
  baselineId,
  fixedTargets,
}: {
  details: SpecDetail[]
  scenarioId: string
  targets: number
  baselineId: string | null
  fixedTargets: boolean
}) {
  const rows = useMemo(
    () => splitRows(details, scenarioId, targets, baselineId),
    [details, scenarioId, targets, baselineId],
  )
  if (rows.length === 0) return null

  return (
    <>
      <div className="overflow-x-auto px-1 pb-2">
        <table className="w-full min-w-[760px] border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              <th scope="col" className="px-4 py-2.5 font-medium">
                Build
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Main target
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Alone it would take
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Funnel gain
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Concentration
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
                  {row.singleTarget > 0 ? fullNumber(row.singleTarget) : '—'}
                </td>
                <td className="tnum px-4 py-2 text-right font-medium text-ink">
                  {row.gain !== undefined ? `${row.gain.toFixed(2)}x` : '—'}
                </td>
                <td className="tnum px-4 py-2 text-right text-ink-secondary">
                  {row.concentration !== undefined ? `${row.concentration.toFixed(2)}x ` : ''}
                  <span className="text-ink-muted">({percent(row.share, 0)} on main)</span>
                </td>
                <td className="px-4 py-2 text-ink-secondary">
                  {row.gain !== undefined ? describeFunnelGain(row.gain) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Note>
        {fixedTargets ? (
          <>
            Funnel gain compares main-target damage at {targets} targets against the same
            build alone. Concentration compares it against an even split across all{' '}
            {targets}. A build can score high on one and low on the other — high
            concentration with no gain just means the build has little area damage to
            spend global cooldowns on.
          </>
        ) : (
          <>
            Funnel gain compares main-target damage with the adds present against the same
            build with no adds at all. Concentration is not shown here: the adds arrive and
            leave, so there is no fixed target count to compare an even split against — only
            the share landing on the main target.
          </>
        )}
      </Note>
      <Note>
        Two caveats worth knowing. SimulationCraft's rotations are written to maximise
        total damage, and in these fights no target ever dies — a player deliberately
        funnelling would hold single-target spenders for the boss and score higher than
        this. And the talent build is held fixed across the whole sweep, because
        SimulationCraft ships one build per spec: the rotation adapts to the target
        count, the talents do not. At high target counts these are raid single-target
        builds, so a spec whose real area-damage build differs will read low here.
      </Note>
    </>
  )
}
