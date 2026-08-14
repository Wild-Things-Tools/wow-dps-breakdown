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
 *
 * Form: the headline is a *diverging* bar around 1.0, one bar per build, every
 * build in the tier. 1.0 is where the reading flips, so it is the baseline the
 * bars grow from rather than a line drawn across a chart anchored at zero. The
 * per-target curves are small multiples underneath, for the scenario that sweeps.
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
import {
  AXIS_LINE,
  AXIS_TICK,
  CURSOR_FILL,
  GRID,
  TooltipCard,
} from '../components/chart'
import { BuildIdentity, makeBuildTick } from '../components/BuildIdentity'
import { SmallMultiples, type SparkPanel } from '../components/SmallMultiples'
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
import { classColor } from '../lib/palette'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

const MAIN_COLOR = 'var(--series-1)'
const REST_COLOR = 'var(--baseline)'

/** Two lines of tick text plus the icon. */
const ROW_HEIGHT = 32
const TICK_WIDTH = 205

type Metric = 'gain' | 'concentration'

export function FunnelView({
  details,
  scenario,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
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
  const [readAt, setReadAt] = useState(5)
  const effectiveTargets = funnelTargets.includes(readAt) ? readAt : (funnelTargets[0] ?? 1)

  const ranked = useMemo(
    () => rankRows(details, scenario.id, effectiveTargets, metric, sweeps),
    [details, scenario.id, effectiveTargets, metric, sweeps],
  )
  const panels = useMemo(
    () => (sweeps ? gainPanels(details, scenario.id, metric) : []),
    [details, scenario.id, metric, sweeps],
  )

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Funnel" subtitle={EXPLAINER} />
        <EmptyState>No per-build data has been generated for this tier yet.</EmptyState>
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

  const targetSelect =
    funnelTargets.length > 1 ? (
      <Select
        label="Targets"
        value={effectiveTargets}
        onChange={setReadAt}
        options={funnelTargets.map((count) => ({ value: count, label: String(count) }))}
      />
    ) : null

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
                ? `Main-target damage at ${effectiveTargets} targets`
                : 'Main-target damage with the adds present'
              : `How the damage is distributed at ${effectiveTargets} targets`
          }
          subtitle={
            metric === 'gain'
              ? 'Relative to the same build with no adds at all. Bars grow from 1.0 in both directions: right of it, having the adds up makes the main target die faster.'
              : 'Relative to an even spread across every target. This says nothing about whether the adds helped — only where the damage went.'
          }
          actions={
            <>
              {targetSelect}
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
            </>
          }
        />
        {ranked.length === 0 ? (
          <EmptyState>
            {metric === 'gain'
              ? 'No funnel gain for these builds in this scenario.'
              : 'Concentration needs a fixed target count, and this scenario’s adds come and go. Switch to funnel gain.'}
          </EmptyState>
        ) : (
          <>
            <DivergingChart rows={ranked} metric={metric} targets={effectiveTargets} />
            <RankTable rows={ranked} metric={metric} targets={effectiveTargets} />
          </>
        )}
        {metric === 'concentration' ? (
          <Note>
            A build with little area damage scores high here without the extra targets
            having done anything for it. Switch to funnel gain for that question.
          </Note>
        ) : null}
      </Panel>

      {panels.length ? (
        <Panel>
          <PanelHeader
            title={
              metric === 'gain'
                ? 'How the gain moves as targets are added'
                : 'How concentration moves as targets are added'
            }
            subtitle="One panel per build, all on one scale, with the median build drawn faint behind each curve."
          />
          <SmallMultiples
            panels={panels}
            formatX={(value) => `${value} targets`}
            formatY={(value) => `${value.toFixed(2)}x`}
            referenceY={metric === 'gain' ? 1 : undefined}
            referenceLabel={
              metric === 'gain'
                ? 'Dashed line is 1.0x, where the adds neither help nor hurt.'
                : 'Concentration rises with the target count by construction — 1.0x is always an even spread, and the ceiling is the number of targets.'
            }
          />
        </Panel>
      ) : null}

      <Panel>
        <PanelHeader
          title={
            sweeps ? `Where the damage lands at ${effectiveTargets} targets` : 'Where the damage lands'
          }
          subtitle="Damage per second split between the main target and everything else."
          actions={targetSelect}
        />
        <SplitChart
          details={details}
          scenarioId={scenario.id}
          targets={effectiveTargets}
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
          targets={effectiveTargets}
          baselineId={baselineId}
          fixedTargets={sweeps}
        />
      </Panel>
    </div>
  )
}

const EXPLAINER =
  'Funnelling is when the main target takes more damage because the other targets are there — damage-over-time effects on adds generating resources and procs that get spent on the priority target. That is a different question from how the damage happens to be distributed, and the two often disagree.'

// --------------------------------------------------------------------------------
// Ranked diverging bars
// --------------------------------------------------------------------------------

interface RankRow {
  build: SpecDetail
  label: string
  value: number
  /** value - 1: what the bar draws, so the mark grows from the meaningful baseline. */
  delta: number
}

function rankRows(
  details: SpecDetail[],
  scenarioId: string,
  targets: number,
  metric: Metric,
  sweeps: boolean,
): RankRow[] {
  const rows: RankRow[] = []
  for (const detail of details) {
    const cells = detail.scenarios[scenarioId]?.targets ?? []
    const cell = sweeps ? cells.find((entry) => entry.targets === targets) : cells[0]
    const value = metric === 'gain' ? cell?.funnelGain : cell?.concentration
    if (value === undefined) continue
    rows.push({ build: detail, label: detail.displayName, value, delta: value - 1 })
  }
  return rows.sort((a, b) => b.value - a.value)
}

function DivergingChart({
  rows,
  metric,
  targets,
}: {
  rows: RankRow[]
  metric: Metric
  targets: number
}) {
  const byLabel = useMemo(() => new Map(rows.map((row) => [row.label, row.build])), [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: TICK_WIDTH }), [byLabel])
  // Asymmetric on purpose: the data is lopsided (a lot of dilution, very little
  // gain), and a symmetric domain would spend half the plot on empty space.
  const low = Math.min(0, ...rows.map((row) => row.delta))
  const high = Math.max(0, ...rows.map((row) => row.delta))
  const pad = Math.max((high - low) * 0.08, 0.01)

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
            domain={[low - pad, high + pad]}
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
          {/* 1.0 is the meaningful zero: no gain for one metric, an even spread for
              the other. The bars grow from it, so a bar's direction is the reading. */}
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as RankRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  rows={[
                    {
                      id: 'value',
                      label: metric === 'gain' ? 'Funnel gain' : 'Concentration',
                      color: classColor(row.build.class),
                      value: `${row.value.toFixed(3)}x`,
                      hint:
                        metric === 'gain'
                          ? describeFunnelGain(row.value)
                          : describeConcentration(row.value, targets),
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="delta" barSize={14} radius={2} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell key={row.build.id} fill={classColor(row.build.class)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function RankTable({
  rows,
  metric,
  targets,
}: {
  rows: RankRow[]
  metric: Metric
  targets: number
}) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[620px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">
              Build
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              {metric === 'gain' ? 'Funnel gain' : 'Concentration'}
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
                {row.value.toFixed(2)}x
              </td>
              <td className="py-2 pr-5 text-ink-secondary">
                {metric === 'gain'
                  ? describeFunnelGain(row.value)
                  : describeConcentration(row.value, targets)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function gainPanels(details: SpecDetail[], scenarioId: string, metric: Metric): SparkPanel[] {
  const panels: SparkPanel[] = []
  for (const detail of details) {
    const points: Array<{ x: number; y: number }> = []
    for (const cell of detail.scenarios[scenarioId]?.targets ?? []) {
      const value = metric === 'gain' ? cell.funnelGain : cell.concentration
      if (value === undefined) continue
      points.push({ x: cell.targets, y: value })
    }
    if (points.length < 2) continue
    points.sort((a, b) => a.x - b.x)
    const last = points[points.length - 1]
    if (!last) continue
    panels.push({
      build: detail,
      points,
      headline: `${last.y.toFixed(2)}x`,
      caption:
        metric === 'gain'
          ? describeFunnelGain(last.y)
          : describeConcentration(last.y, last.x),
    })
  }
  return panels.sort((a, b) => {
    const av = a.points[a.points.length - 1]?.y ?? 0
    const bv = b.points[b.points.length - 1]?.y ?? 0
    return bv - av
  })
}

// --------------------------------------------------------------------------------
// Main target vs everything else
// --------------------------------------------------------------------------------

interface SplitRow {
  id: string
  build: SpecDetail
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
    const cell = cells.find((entry) => entry.targets === targets) ?? cells[0]
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
      build: detail,
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
  const byLabel = useMemo(() => new Map(rows.map((row) => [row.label, row.build])), [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: TICK_WIDTH }), [byLabel])

  if (rows.length === 0) {
    return <EmptyState>No split data at {targets} targets for these builds.</EmptyState>
  }

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={Math.max(200, rows.length * ROW_HEIGHT + 50)}>
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
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
            width={TICK_WIDTH}
            tick={tick}
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
          {/* Two segments of one whole, so this is the one chart here whose colour is
              not class identity: it encodes which part of the damage, not whose. */}
          <Bar
            dataKey="main"
            stackId="dps"
            fill={MAIN_COLOR}
            barSize={16}
            isAnimationActive={false}
            stroke="var(--surface-1)"
            strokeWidth={1}
          />
          <Bar
            dataKey="rest"
            stackId="dps"
            fill={REST_COLOR}
            barSize={16}
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
        <table className="w-full min-w-[820px] border-collapse text-[13px]">
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
                <td className="px-4 py-2">
                  <BuildIdentity build={row.build} />
                </td>
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
