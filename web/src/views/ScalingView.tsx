/**
 * Target scaling: how every build's throughput changes from 1 to 10 targets.
 *
 * Two readings, one axis each (never both on one chart):
 *  - absolute DPS, for "who actually does more damage at five targets"
 *  - indexed to each build's own single-target DPS, for "who gains the most from
 *    extra targets" -- which is the shape question, and is unreadable in absolute
 *    terms when builds start hundreds of thousands of DPS apart.
 *
 * The form is small multiples rather than one plot. This view used to draw up to
 * six lines chosen through a picker; with no selection there are twenty-six, and
 * twenty-six lines on one axis is a smear. One panel per build on a shared scale,
 * each with the median build drawn faint behind it, answers the same question
 * without asking anybody to choose first. The table underneath carries every
 * number the panels compress.
 */

import { useMemo, useState } from 'react'
import { SmallMultiples, type SparkPanel } from '../components/SmallMultiples'
import { BuildIdentity } from '../components/BuildIdentity'
import { EmptyState, Note, Panel, PanelHeader, SegmentedControl } from '../components/ui'
import { compactNumber, fullNumber } from '../lib/format'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

type Mode = 'absolute' | 'indexed'

const TABLE_TARGETS = [1, 3, 5, 10]

export function ScalingView({
  details,
  scenario,
}: {
  details: SpecDetail[]
  scenario: ScenarioMeta
}) {
  const [mode, setMode] = useState<Mode>('indexed')

  const panels = useMemo(() => buildPanels(details, scenario.id, mode), [details, scenario.id, mode])
  const rows = useMemo(() => buildTable(details, scenario.id), [details, scenario.id])

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

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Target scaling" />
        <EmptyState>No per-build data has been generated for this tier yet.</EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Target scaling"
          subtitle={
            mode === 'absolute'
              ? 'Total damage per second at each target count, every build in the tier on one scale.'
              : 'Each build indexed to its own single-target damage, so the curves show how much extra targets are worth rather than who is ahead.'
          }
          actions={
            <SegmentedControl
              label="Scale"
              value={mode}
              onChange={setMode}
              options={[
                { value: 'indexed', label: 'Relative to 1 target' },
                { value: 'absolute', label: 'Absolute DPS' },
              ]}
            />
          }
        />
        {panels.length === 0 ? (
          <EmptyState>No sweep recorded for these builds in {scenario.label}.</EmptyState>
        ) : (
          <SmallMultiples
            panels={panels}
            formatX={(value) => `${value} ${value === 1 ? 'target' : 'targets'}`}
            formatY={(value) =>
              mode === 'absolute' ? fullNumber(value) : `${value.toFixed(2)}x`
            }
            referenceY={mode === 'indexed' ? 1 : undefined}
            referenceLabel={
              mode === 'indexed'
                ? 'Dashed line is 1.0x — the build’s own single-target damage. The faint curve in every panel is the median build at that target count, so a panel that sits above it is scaling better than most.'
                : 'The faint curve in every panel is the median build at that target count. All panels share one axis, so panel heights are comparable.'
            }
          />
        )}
        <Note>
          {mode === 'indexed'
            ? 'A build at 4x on five targets does four times its single-target damage there. Higher is not automatically better — it means the build leans on extra targets rather than being strong on one.'
            : 'Absolute throughput. Compare the gaps, not the ranks: a few percent is within simulation noise.'}
        </Note>
      </Panel>

      {rows.length ? (
        <Panel>
          <PanelHeader
            title="Every build, in numbers"
            subtitle="The table twin of the panels above, so nothing here depends on reading a curve or telling two class colours apart."
          />
          <ScalingTable rows={rows} />
        </Panel>
      ) : null}
    </div>
  )
}

interface TableRow {
  build: SpecDetail
  dps: Record<number, number | undefined>
  ratio?: number
  top: number
}

function cellsOf(detail: SpecDetail, scenarioId: string) {
  return detail.scenarios[scenarioId]?.targets ?? []
}

function buildPanels(details: SpecDetail[], scenarioId: string, mode: Mode): SparkPanel[] {
  const panels: SparkPanel[] = []
  for (const detail of details) {
    const cells = cellsOf(detail, scenarioId)
    if (cells.length < 2) continue
    const single = cells.find((cell) => cell.targets === 1)?.dps
    if (mode === 'indexed' && !single) continue
    const points = [...cells]
      .sort((a, b) => a.targets - b.targets)
      .map((cell) => ({
        x: cell.targets,
        y: mode === 'absolute' ? cell.dps : cell.dps / (single ?? 1),
      }))
    const last = points[points.length - 1]
    if (!last) continue
    panels.push({
      build: detail,
      points,
      headline:
        mode === 'absolute' ? compactNumber(last.y) : `${last.y.toFixed(1)}x`,
      caption:
        mode === 'absolute'
          ? `${compactNumber(points[0]?.y ?? 0)} at 1 target · ${compactNumber(last.y)} at ${last.x}`
          : `1 → ${last.x} targets`,
    })
  }
  return panels.sort((a, b) => {
    const av = a.points[a.points.length - 1]?.y ?? 0
    const bv = b.points[b.points.length - 1]?.y ?? 0
    return bv - av
  })
}

function buildTable(details: SpecDetail[], scenarioId: string): TableRow[] {
  const rows: TableRow[] = []
  for (const detail of details) {
    const cells = cellsOf(detail, scenarioId)
    if (cells.length === 0) continue
    const dps: Record<number, number | undefined> = {}
    for (const count of TABLE_TARGETS) {
      dps[count] = cells.find((cell) => cell.targets === count)?.dps
    }
    const single = dps[1]
    const top = Math.max(...cells.map((cell) => cell.targets))
    const highest = cells.find((cell) => cell.targets === top)?.dps
    rows.push({
      build: detail,
      dps,
      ratio: single && highest ? highest / single : undefined,
      top,
    })
  }
  return rows.sort((a, b) => (b.ratio ?? 0) - (a.ratio ?? 0))
}

function ScalingTable({ rows }: { rows: TableRow[] }) {
  const top = rows[0]?.top ?? 10
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[720px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">
              Build
            </th>
            {TABLE_TARGETS.map((count) => (
              <th key={count} scope="col" className="py-2.5 pr-4 text-right font-medium">
                {count}T
              </th>
            ))}
            <th scope="col" className="py-2.5 pr-5 text-right font-medium">
              {top}T ÷ 1T
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.build.id} className="border-b border-hairline/60 last:border-0">
              <td className="py-2 pr-4 pl-5">
                <BuildIdentity build={row.build} />
              </td>
              {TABLE_TARGETS.map((count) => (
                <td key={count} className="tnum py-2 pr-4 text-right text-ink-secondary">
                  {row.dps[count] !== undefined ? compactNumber(row.dps[count] ?? 0) : '—'}
                </td>
              ))}
              <td className="tnum py-2 pr-5 text-right font-medium text-ink">
                {row.ratio !== undefined ? `${row.ratio.toFixed(2)}x` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
