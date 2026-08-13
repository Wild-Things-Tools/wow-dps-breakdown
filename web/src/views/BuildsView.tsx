/**
 * Build comparison: within one spec, which hero-talent build is ahead, and where
 * that changes.
 *
 * Two rankings over the same runs, kept as separate modes rather than two axes on
 * one chart:
 *  - total damage, which is the question "what tops the meter"
 *  - damage on the main target, which is the question "what kills the boss fastest
 *    while the rest of the pack is up" -- the funnel reading of a build choice
 *
 * They disagree often, and that disagreement is the point of the view. So is the
 * crossover: a build that wins on one target and loses on eight is two different
 * recommendations, and a single headline number hides exactly that.
 *
 * Honesty constraint specific to this view: simc's shipped builds for one spec
 * differ in gear as well as talents (verified -- MID2 Arcane's two builds carry
 * different rings and noticeably different secondaries). So a gap here means "this
 * build the way simc plays it", not "these talents are worth this much". The note
 * says so, and a margin inside the two runs' combined sampling error is reported as
 * a tie rather than as a winner.
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
  Dot,
  EmptyState,
  Legend,
  Note,
  Panel,
  PanelHeader,
  SegmentedControl,
  Select,
  StatTile,
} from '../components/ui'
import { compactNumber, fullNumber, percent } from '../lib/format'
import { slotColor } from '../lib/palette'
import type { Cell, ScenarioMeta, SpecDetail, SpecSummary } from '../lib/types'

type Metric = 'dps' | 'priority'

/** Plot area of the chart: its 380px height less margins and the x-axis. */
const PLOT_HEIGHT = 320

export function BuildsView({
  specs,
  specId,
  onSpecChange,
  details,
  scenario,
}: {
  specs: SpecSummary[]
  specId: string | null
  onSpecChange: (specId: string) => void
  details: SpecDetail[]
  scenario: ScenarioMeta
}) {
  const [metric, setMetric] = useState<Metric>('dps')

  const groups = useMemo(() => groupBySpec(specs), [specs])
  const current = specId ?? groups[0]?.specId ?? null
  const sweeps = scenario.sweepsTargets ?? false

  // Slots follow the build's position within the spec. Unlike the shared picker
  // there is nothing to keep stable across a filter: the set changes wholesale
  // when another spec is chosen.
  const colorOf = useMemo(() => {
    const slots = new Map(details.map((detail, index) => [detail.id, slotColor(index)]))
    return (id: string) => slots.get(id) ?? slotColor(0)
  }, [details])

  const { data, series } = useMemo(
    () => buildSeries(details, scenario.id, metric),
    [details, scenario.id, metric],
  )

  const labelOffsets = useMemo(() => {
    const last = data[data.length - 1]
    if (!last) return new Map<string, number>()
    const finals = series
      .map((entry) => ({ id: entry.id, value: Number(last[entry.id] ?? 0) }))
      .filter((entry) => entry.value > 0)
    const values = finals.map((entry) => entry.value)
    return resolveLabelOffsets(finals, [0, Math.max(...values, 1)], PLOT_HEIGHT)
  }, [data, series])

  const leads = useMemo(
    () => buildLeads(details, scenario.id, metric),
    [details, scenario.id, metric],
  )
  const story = useMemo(() => describeCrossover(leads), [leads])

  const picker = (
    <Select
      label="Spec"
      value={current ?? ''}
      onChange={onSpecChange}
      options={groups.map((group) => ({
        value: group.specId,
        label: `${group.label}${group.builds.length > 1 ? '' : ' (one build)'}`,
      }))}
    />
  )

  if (details.length < 2) {
    return (
      <Panel>
        <PanelHeader title="Build comparison" actions={picker} />
        <EmptyState>
          SimulationCraft ships a single build for this spec, so there is nothing to
          compare. Pick a spec with two hero-talent builds — most have them.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Build comparison"
          subtitle={
            metric === 'dps'
              ? 'Total damage per second for each of the spec’s hero-talent builds.'
              : 'Damage per second landing on the main target — which build kills the boss fastest while the rest of the pack is up.'
          }
          actions={
            <div className="flex flex-wrap items-center gap-3">
              {picker}
              <SegmentedControl
                label="Rank by"
                value={metric}
                onChange={setMetric}
                options={[
                  { value: 'dps', label: 'Total DPS' },
                  { value: 'priority', label: 'Boss DPS' },
                ]}
              />
            </div>
          }
        />

        {data.length === 0 ? (
          <EmptyState>
            {metric === 'priority'
              ? `${scenario.label} reports no main-target damage to rank by — priority damage only exists once there is more than one enemy.`
              : `No ${scenario.label} data for these builds yet.`}
          </EmptyState>
        ) : (
          <>
            <div className="grid gap-3 px-2 py-4 sm:grid-cols-2 xl:grid-cols-4">
              {story.map((tile) => (
                <StatTile
                  key={tile.label}
                  label={tile.label}
                  value={tile.value}
                  caption={tile.caption}
                  accent={tile.buildId ? colorOf(tile.buildId) : undefined}
                />
              ))}
            </div>

            {sweeps ? (
              <div className="px-2 pb-4">
                <ResponsiveContainer width="100%" height={380}>
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
                      width={56}
                      tickFormatter={(value: number) => compactNumber(value)}
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
                            title={`${label} ${Number(label) === 1 ? 'target' : 'targets'}`}
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
                    {series.map((entry) => (
                      <Line
                        key={entry.id}
                        type="monotone"
                        dataKey={entry.id}
                        name={entry.label}
                        stroke={colorOf(entry.id)}
                        strokeWidth={2}
                        dot={false}
                        activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
                        isAnimationActive={false}
                        label={makeEndLabel(
                          shortLabel(entry.label),
                          data.length - 1,
                          labelOffsets.get(entry.id) ?? 0,
                        )}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : null}

            <Legend
              items={series.map((entry) => ({
                id: entry.id,
                label: entry.label,
                color: colorOf(entry.id),
              }))}
            />
          </>
        )}
      </Panel>

      {leads.length > 0 ? (
        <Panel>
          <PanelHeader
            title="Where the lead changes hands"
            subtitle={`Ranked by ${
              metric === 'dps' ? 'total damage' : 'damage on the main target'
            } at each target count, with the margin over the runner-up.`}
          />
          <LeadTable leads={leads} colorOf={colorOf} sweeps={sweeps} />
          <Note>
            A margin shown as a tie is smaller than the two runs’ combined sampling error,
            so the ranking at that target count is not evidence of anything. These are
            SimulationCraft’s own recommended builds, which differ in gear as well as
            talents — read a gap as “this build the way simc plays it”, not as the value of
            the talents alone.
          </Note>
        </Panel>
      ) : null}
    </div>
  )
}

interface Lead {
  targets: number
  winnerId: string
  winnerLabel: string
  winnerValue: number
  runnerUpLabel: string
  /** Fractional lead of the winner over the runner-up. */
  margin: number
  /** The two means' standard errors added in quadrature, as a fraction. */
  noise: number
}

function LeadTable({
  leads,
  colorOf,
  sweeps,
}: {
  leads: Lead[]
  colorOf: (id: string) => string
  sweeps: boolean
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            {sweeps ? <th className="py-2 pr-4 pl-4 font-medium">Targets</th> : null}
            <th className="py-2 pr-4 font-medium">Ahead</th>
            <th className="py-2 pr-4 text-right font-medium">DPS</th>
            <th className="py-2 pr-4 text-right font-medium">Margin</th>
            <th className="py-2 pr-4 font-medium">Over</th>
          </tr>
        </thead>
        <tbody>
          {leads.map((lead) => {
            const tie = lead.margin <= lead.noise
            return (
              <tr key={lead.targets} className="border-b border-hairline/60 last:border-0">
                {sweeps ? (
                  <td className="py-2 pr-4 pl-4 tabular-nums text-ink-secondary">
                    {lead.targets}
                  </td>
                ) : null}
                <td className="py-2 pr-4">
                  <span className="inline-flex items-center gap-2">
                    <Dot color={colorOf(lead.winnerId)} />
                    <span className="text-ink">{lead.winnerLabel}</span>
                  </span>
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-ink">
                  {fullNumber(lead.winnerValue)}
                </td>
                <td
                  className={`py-2 pr-4 text-right tabular-nums ${
                    tie ? 'text-ink-muted' : 'text-ink'
                  }`}
                >
                  {tie ? 'tie' : `+${percent(lead.margin)}`}
                </td>
                <td className="py-2 pr-4 text-ink-secondary">{lead.runnerUpLabel}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

interface SpecGroup {
  specId: string
  label: string
  builds: SpecSummary[]
}

/** Spec rows regrouped as "one spec, its builds", which is how this view reads them. */
export function groupBySpec(specs: SpecSummary[]): SpecGroup[] {
  const byId = new Map<string, SpecGroup>()
  for (const spec of specs) {
    let group = byId.get(spec.specId)
    if (!group) {
      group = { specId: spec.specId, label: `${spec.spec} ${spec.class}`, builds: [] }
      byId.set(spec.specId, group)
    }
    group.builds.push(spec)
  }
  // Specs with something to compare first, then alphabetically, so the picker opens
  // on a spec that actually has a comparison to show.
  return [...byId.values()].sort(
    (a, b) => b.builds.length - a.builds.length || a.label.localeCompare(b.label),
  )
}

function valueOf(cell: Cell, metric: Metric): number | undefined {
  return metric === 'dps' ? cell.dps : cell.priorityDps
}

function buildSeries(details: SpecDetail[], scenarioId: string, metric: Metric) {
  const series = details.map((detail) => ({ id: detail.id, label: detail.heroTalent }))

  const byTargets = new Map<number, Record<string, number | string>>()
  for (const detail of details) {
    for (const cell of detail.scenarios[scenarioId]?.targets ?? []) {
      const value = valueOf(cell, metric)
      if (value === undefined) continue
      let row = byTargets.get(cell.targets)
      if (!row) {
        row = { targets: cell.targets }
        byTargets.set(cell.targets, row)
      }
      row[detail.id] = value
    }
  }

  const data = [...byTargets.values()].sort((a, b) => Number(a.targets) - Number(b.targets))
  return { data, series }
}

function buildLeads(details: SpecDetail[], scenarioId: string, metric: Metric): Lead[] {
  const targets = new Set<number>()
  for (const detail of details) {
    for (const cell of detail.scenarios[scenarioId]?.targets ?? []) targets.add(cell.targets)
  }

  const leads: Lead[] = []
  for (const count of [...targets].sort((a, b) => a - b)) {
    const entries = details
      .map((detail) => {
        const cell = detail.scenarios[scenarioId]?.targets.find(
          (candidate) => candidate.targets === count,
        )
        const value = cell ? valueOf(cell, metric) : undefined
        return cell && value !== undefined
          ? { id: detail.id, label: detail.heroTalent, value, error: cell.dpsError / 100 }
          : null
      })
      .filter((entry): entry is NonNullable<typeof entry> => entry !== null)
      .sort((a, b) => b.value - a.value)

    const [winner, runnerUp] = entries
    if (!winner || !runnerUp) continue
    leads.push({
      targets: count,
      winnerId: winner.id,
      winnerLabel: winner.label,
      winnerValue: winner.value,
      runnerUpLabel: runnerUp.label,
      margin: winner.value / runnerUp.value - 1,
      noise: Math.hypot(winner.error, runnerUp.error),
    })
  }
  return leads
}

interface StoryTile {
  label: string
  value: string
  caption: string
  buildId?: string
}

/**
 * What belongs above the chart: who leads at the target counts people plan around,
 * and whether the answer changes across the sweep.
 */
function describeCrossover(leads: Lead[]): StoryTile[] {
  if (leads.length === 0) return []

  const tiles: StoryTile[] = []
  for (const count of [1, 5, 10]) {
    const lead = leads.find((entry) => entry.targets === count)
    if (!lead) continue
    const clear = lead.margin > lead.noise
    tiles.push({
      label: count === 1 ? 'Single target' : `${count} targets`,
      value: clear ? lead.winnerLabel : 'Too close to call',
      caption: clear
        ? `${percent(lead.margin)} ahead of ${lead.runnerUpLabel}.`
        : `${lead.winnerLabel} and ${lead.runnerUpLabel} land inside each other’s sampling error.`,
      buildId: clear ? lead.winnerId : undefined,
    })
  }

  // Leads run in target order, so the first entry whose winner differs from the
  // first decisive one is where the lead changed hands.
  const decisive = leads.filter((lead) => lead.margin > lead.noise)
  const first = decisive[0]
  const flip = first ? decisive.find((lead) => lead.winnerId !== first.winnerId) : undefined

  if (first && flip) {
    tiles.push({
      label: 'Crossover',
      value: `${flip.targets} targets`,
      caption: `${first.winnerLabel} leads below that, ${flip.winnerLabel} from there up. Which build is “better” depends on the content.`,
    })
  } else if (first) {
    tiles.push({
      label: 'Crossover',
      value: 'None',
      caption: `${first.winnerLabel} is ahead everywhere the gap clears the sampling error.`,
    })
  }

  return tiles
}
