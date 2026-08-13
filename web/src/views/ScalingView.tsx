/**
 * Target scaling: how each selected build's throughput changes from 1 to 10 targets.
 *
 * Two readings, one axis each (never both on one chart):
 *  - absolute DPS, for "who actually does more damage at five targets"
 *  - indexed to each build's own single-target DPS, for "who gains the most from
 *    extra targets" -- which is the shape question, and is unreadable in absolute
 *    terms when builds start hundreds of thousands of DPS apart.
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
import { EmptyState, Legend, Note, Panel, PanelHeader, SegmentedControl } from '../components/ui'
import { compactNumber, fullNumber } from '../lib/format'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

type Mode = 'absolute' | 'indexed'

/** Plot area of the chart: its 380px height less margins and the x-axis. */
const PLOT_HEIGHT = 320

export function ScalingView({
  details,
  scenario,
  colorOf,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
  colorOf: (id: string) => string
}) {
  const [mode, setMode] = useState<Mode>('absolute')

  const { data, series } = useMemo(
    () => buildSeries(details, scenario.id, mode),
    [details, scenario.id, mode],
  )

  const labelOffsets = useMemo(() => {
    const last = data[data.length - 1]
    if (!last) return new Map<string, number>()
    const finals = series
      .map((entry) => ({ id: entry.id, value: Number(last[entry.id] ?? 0) }))
      .filter((entry) => entry.value > 0)
    const values = finals.map((entry) => entry.value)
    // The absolute chart starts at zero; the indexed one starts at 1x.
    const min = mode === 'absolute' ? 0 : 1
    return resolveLabelOffsets(finals, [min, Math.max(...values, min + 1)], PLOT_HEIGHT)
  }, [data, series, mode])

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Target scaling" />
        <EmptyState>
          Pick a build above to plot how its damage scales from one target to ten.
        </EmptyState>
      </Panel>
    )
  }

  if (scenario.targetCounts.length < 2) {
    return (
      <Panel>
        <PanelHeader title="Target scaling" subtitle={scenario.description} />
        <EmptyState>
          {scenario.label} sets its own target count over the course of the fight, so there
          is no sweep to plot. Switch to Patchwerk to compare across target counts.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <Panel>
      <PanelHeader
        title="Target scaling"
        subtitle={
          mode === 'absolute'
            ? 'Total damage per second at each target count.'
            : 'Each build indexed to its own single-target damage, so the lines show how much extra targets are worth rather than who is ahead.'
        }
        actions={
          <SegmentedControl
            label="Scale"
            value={mode}
            onChange={setMode}
            options={[
              { value: 'absolute', label: 'Absolute DPS' },
              { value: 'indexed', label: 'Relative to 1 target' },
            ]}
          />
        }
      />

      <div className="px-2 py-4">
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
              tickFormatter={(value: number) =>
                mode === 'absolute' ? compactNumber(value) : `${value.toFixed(1)}x`
              }
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
                      value:
                        mode === 'absolute'
                          ? fullNumber(Number(entry.value ?? 0))
                          : `${Number(entry.value ?? 0).toFixed(2)}x`,
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

      <Legend
        items={series.map((entry) => ({
          id: entry.id,
          label: entry.label,
          color: colorOf(entry.id),
        }))}
      />

      <Note>
        {mode === 'indexed'
          ? 'A build at 4x on five targets does four times its single-target damage there. Higher is not automatically better — it means the build leans on extra targets rather than being strong on one.'
          : 'Absolute throughput. Compare the gaps, not the ranks: a few percent is within simulation noise.'}
      </Note>
    </Panel>
  )
}

function buildSeries(details: SpecDetail[], scenarioId: string, mode: Mode) {
  const series = details.map((detail) => ({ id: detail.id, label: detail.displayName }))

  const byTargets = new Map<number, Record<string, number | string>>()
  for (const detail of details) {
    const cells = detail.scenarios[scenarioId]?.targets ?? []
    const single = cells.find((cell) => cell.targets === 1)?.dps
    for (const cell of cells) {
      let row = byTargets.get(cell.targets)
      if (!row) {
        row = { targets: cell.targets }
        byTargets.set(cell.targets, row)
      }
      row[detail.id] =
        mode === 'absolute' ? cell.dps : single && single > 0 ? cell.dps / single : 0
    }
  }

  const data = [...byTargets.values()].sort(
    (a, b) => Number(a.targets) - Number(b.targets),
  )
  return { data, series }
}
