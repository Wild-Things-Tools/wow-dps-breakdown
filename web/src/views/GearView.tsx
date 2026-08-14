/**
 * Loot: is this drop an upgrade over what the character already wears?
 *
 * The chart is a *diverging* bar around zero, and zero is not "no damage" — it is
 * the spec's own baseline, the two best Mythic+ trinkets it already has, worn at the
 * lower of the two item levels. So a bar to the left is a real answer ("this drop is
 * a downgrade for this spec"), not a missing one, and the baseline has to be named
 * on screen rather than implied.
 *
 * Two things this view has to keep visible or it starts lying:
 *
 * 1. **The noise band.** Gains here are fractions of a percent, and the runs measure
 *    to about a tenth of that. The shaded band across zero is the median combined
 *    standard error of the comparisons on screen; a bar that does not clear it is a
 *    tie, and the table says so in words. Same rule as the Builds view — margins
 *    inside `hypot(errA, errB)` are not leads.
 * 2. **Coverage.** A gear sweep is expensive enough that it usually covers part of
 *    the tier. The panel header says how many specs of how many were swept, because
 *    "the best trinket" over six specs and over twenty-six are different claims.
 *
 * Form and colour, per the dataviz method: the job is "tell distinct series apart"
 * across a magnitude comparison, so bars grouped by item with one bar per spec, and
 * categorical colour keyed to the spec — never to the value, and never reassigned
 * when the selection changes. Item level is a *mode* of the chart, chosen with a
 * segmented control, rather than a second axis or a second set of bars: doubling the
 * bars per group would put twelve marks in a row nobody can read.
 */

import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AXIS_LINE, AXIS_TICK, CURSOR_FILL, GRID, TooltipCard } from '../components/chart'
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
import { fullNumber, percent } from '../lib/format'
import type {
  GearDataset,
  GearItemMeta,
  GearSlot,
  GearSpecResult,
} from '../lib/types'

/** Height one item's group of bars needs, plus the axis band beneath the plot. */
const ROW_HEIGHT = 34
const AXIS_BAND = 44

export function GearView({
  gear,
  visible,
  colorOf,
}: {
  gear: GearDataset | null
  /** Spec ids to chart, already narrowed to ones the sweep covered. */
  visible: string[]
  colorOf: (id: string) => string
}) {
  const slot = gear?.slots[0] ?? null
  const [levelId, setLevelId] = useState<string | null>(null)
  const [targets, setTargets] = useState<number | null>(null)

  const specs = useMemo(
    () => (slot?.specs ?? []).filter((spec) => visible.includes(spec.id)),
    [slot, visible],
  )

  const targetCounts = useMemo(() => {
    const counts = new Set<number>()
    for (const spec of slot?.specs ?? []) for (const entry of spec.targets) counts.add(entry.targets)
    return [...counts].sort((a, b) => a - b)
  }, [slot])

  const level = slot?.itemLevels.find((entry) => entry.id === levelId) ?? slot?.itemLevels[0]
  const targetCount = targets ?? targetCounts[0] ?? 1

  const items = useMemo(
    () => new Map((slot?.items ?? []).map((item) => [item.id, item])),
    [slot],
  )

  const rows = useMemo(
    () => (level ? buildRows(specs, targetCount, level.id, items) : []),
    [specs, targetCount, level, items],
  )
  const noise = useMemo(() => medianNoise(rows), [rows])
  const tiles = useMemo(() => headline(rows, specs, noise), [rows, specs, noise])

  if (!gear || !slot || !level) {
    return (
      <Panel>
        <PanelHeader title="Loot" />
        <EmptyState>
          No gear comparison has been generated for this tier yet. It is a separate
          pass — <code>wowdps gear</code> — because it costs roughly a full simulation
          matrix of its own.
        </EmptyState>
      </Panel>
    )
  }

  const controls = (
    <>
      {targetCounts.length > 1 ? (
        <Select
          label="Targets"
          value={targetCount}
          onChange={setTargets}
          options={targetCounts.map((count) => ({
            value: count,
            label: count === 1 ? '1 (single target)' : String(count),
          }))}
        />
      ) : null}
      <SegmentedControl
        label="Item level"
        value={level.id}
        onChange={setLevelId}
        options={slot.itemLevels.map((entry) => ({
          value: entry.id,
          label: `${entry.label.split(',')[0]} · ${entry.ilevel}`,
          title: entry.evidence,
        }))}
      />
    </>
  )

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title={`${slot.candidateSourceLabel} ${slot.label.toLowerCase()}s, against what you already wear`}
          subtitle={
            <>
              Zero is each spec’s own baseline: its two best {slot.baselineSourceLabel}{' '}
              {slot.label.toLowerCase()}s at item level {rows[0]?.baselineIlevel ?? '—'}. A
              candidate takes the place of the <em>weaker</em> of the two, because that is
              the decision a loot council actually makes. Covers{' '}
              {gear.coverage.specs} of {gear.coverage.specsAvailable} builds in the tier.
            </>
          }
          actions={controls}
        />
        {tiles.length ? (
          <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-3">
            {tiles.map((tile) => (
              <StatTile
                key={tile.label}
                label={tile.label}
                value={tile.value}
                caption={tile.caption}
                accent={tile.specId ? colorOf(tile.specId) : undefined}
              />
            ))}
          </div>
        ) : null}
      </Panel>

      <Panel>
        <PanelHeader
          title={`Gain over baseline at ${level.ilevel}`}
          subtitle={level.evidence}
        />
        {rows.length === 0 ? (
          <EmptyState>
            None of the selected builds has a gear sweep at {targetCount}{' '}
            {targetCount === 1 ? 'target' : 'targets'}.
          </EmptyState>
        ) : (
          <>
            <GainChart rows={rows} specs={specs} noise={noise} colorOf={colorOf} />
            <Legend
              items={specs.map((spec) => ({
                id: spec.id,
                label: spec.displayName,
                color: colorOf(spec.id),
              }))}
            />
            <Note>
              The shaded band is ±{percent(noise, 2)}, the median of the two runs’
              standard errors added in quadrature. A bar inside it is a tie, not a
              lead. Item levels come from the upgrade ladder measured in simc’s own
              data, not from a track name — simc’s files do not carry Blizzard’s track
              labels.
            </Note>
          </>
        )}
      </Panel>

      {rows.length ? (
        <Panel>
          <PanelHeader
            title="Every comparison, in numbers"
            subtitle="The table view, so nothing here depends on telling two colours apart. A margin inside the two runs’ combined sampling error is reported as a tie."
          />
          <GainTable rows={rows} specs={specs} colorOf={colorOf} />
        </Panel>
      ) : null}

      <Panel>
        <PanelHeader
          title={`The baseline: each build’s two best ${slot.baselineSourceLabel} ${slot.label.toLowerCase()}s`}
          subtitle={`Chosen by running every eligible ${slot.baselineSourceLabel} ${slot.label.toLowerCase()} on its own, with the other socket empty, and taking the two that added the most. Runners-up are listed so a close call at the cut is visible.`}
        />
        <BaselineTable
          specs={specs}
          targetCount={targetCount}
          items={items}
          colorOf={colorOf}
        />
        <Note>
          Standalone value is not perfectly additive — measured on Arcane Mage, a pair
          is worth about 3% more than the sum of its two parts — so two items within a
          few percent of each other at the cut could swap once paired.
        </Note>
      </Panel>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Data shaping
// --------------------------------------------------------------------------------

interface Row {
  itemId: number
  label: string
  baselineIlevel: number
  /** spec id -> gain, as a fraction of that spec's baseline DPS. */
  gains: Record<string, number>
  errors: Record<string, number>
  dps: Record<string, number>
  replaces: Record<string, number>
  /** Mean gain across the specs shown, used only to order the rows. */
  mean: number
  best: number
}

function buildRows(
  specs: GearSpecResult[],
  targetCount: number,
  levelId: string,
  items: Map<number, GearItemMeta>,
): Row[] {
  const byItem = new Map<number, Row>()

  for (const spec of specs) {
    const target = spec.targets.find((entry) => entry.targets === targetCount)
    if (!target) continue
    for (const candidate of target.candidates) {
      if (candidate.level !== levelId) continue
      let row = byItem.get(candidate.id)
      if (!row) {
        row = {
          itemId: candidate.id,
          label: items.get(candidate.id)?.name ?? `Item ${candidate.id}`,
          baselineIlevel: target.baseline.ilevel,
          gains: {},
          errors: {},
          dps: {},
          replaces: {},
          mean: 0,
          best: Number.NEGATIVE_INFINITY,
        }
        byItem.set(candidate.id, row)
      }
      row.gains[spec.id] = candidate.gain
      row.errors[spec.id] = candidate.gainError
      row.dps[spec.id] = candidate.dps
      row.replaces[spec.id] = candidate.replaces
    }
  }

  const rows = [...byItem.values()]
  for (const row of rows) {
    const values = Object.values(row.gains)
    row.mean = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0
    row.best = values.length ? Math.max(...values) : 0
  }
  // Ordered by the best result any shown spec gets, so the top of the chart is
  // "what to hand out first" rather than an alphabetical list.
  return rows.sort((a, b) => b.best - a.best)
}

/** Median combined standard error across everything on screen, as a fraction. */
function medianNoise(rows: Row[]): number {
  const errors = rows.flatMap((row) => Object.values(row.errors)).sort((a, b) => a - b)
  if (errors.length === 0) return 0
  const middle = Math.floor(errors.length / 2)
  return errors.length % 2
    ? (errors[middle] ?? 0)
    : ((errors[middle - 1] ?? 0) + (errors[middle] ?? 0)) / 2
}

interface Tile {
  label: string
  value: string
  caption: string
  specId?: string
}

function headline(rows: Row[], specs: GearSpecResult[], noise: number): Tile[] {
  if (rows.length === 0 || specs.length === 0) return []
  const tiles: Tile[] = []

  let bestGain = Number.NEGATIVE_INFINITY
  let bestRow: Row | null = null
  let bestSpec: GearSpecResult | null = null
  for (const row of rows) {
    for (const spec of specs) {
      const gain = row.gains[spec.id]
      if (gain !== undefined && gain > bestGain) {
        bestGain = gain
        bestRow = row
        bestSpec = spec
      }
    }
  }
  if (bestRow && bestSpec) {
    tiles.push({
      label: 'Biggest single upgrade',
      value: `${percent(bestGain)}`,
      caption: `${bestRow.label} for ${bestSpec.displayName}.`,
      specId: bestSpec.id,
    })
  }

  // Which item tops the most specs' lists — the one a loot council will argue over.
  const wins = new Map<number, number>()
  for (const spec of specs) {
    let top: Row | null = null
    for (const row of rows) {
      const gain = row.gains[spec.id]
      if (gain === undefined) continue
      if (!top || gain > (top.gains[spec.id] ?? Number.NEGATIVE_INFINITY)) top = row
    }
    if (top) wins.set(top.itemId, (wins.get(top.itemId) ?? 0) + 1)
  }
  const contested = [...wins.entries()].sort((a, b) => b[1] - a[1])[0]
  if (contested) {
    const row = rows.find((entry) => entry.itemId === contested[0])
    tiles.push({
      label: 'Most contested',
      value: row?.label ?? '—',
      caption: `Top pick for ${contested[1]} of ${specs.length} builds shown.`,
    })
  }

  const comparisons = rows.flatMap((row) =>
    specs.map((spec) => row.gains[spec.id]).filter((gain): gain is number => gain !== undefined),
  )
  const ties = comparisons.filter((gain) => Math.abs(gain) <= noise).length
  tiles.push({
    label: 'Too close to call',
    value: `${ties} of ${comparisons.length}`,
    caption: `Comparisons whose margin is inside ±${percent(noise, 2)}, this run’s measured precision.`,
  })

  return tiles
}

// --------------------------------------------------------------------------------
// Chart
// --------------------------------------------------------------------------------

function GainChart({
  rows,
  specs,
  noise,
  colorOf,
}: {
  rows: Row[]
  specs: GearSpecResult[]
  noise: number
  colorOf: (id: string) => string
}) {
  const data = rows.map((row) => ({
    label: row.label,
    ...Object.fromEntries(specs.map((spec) => [spec.id, row.gains[spec.id] ?? null])),
    row,
  }))
  const height = rows.length * Math.max(ROW_HEIGHT, specs.length * 11 + 14) + AXIS_BAND

  return (
    <div className="px-2 pb-1">
      <ResponsiveContainer width="100%" height={Math.max(240, height)}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 28, bottom: 8, left: 8 }}>
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => percent(value, 1)}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={220}
            tick={AXIS_TICK}
            axisLine={false}
            tickLine={false}
          />
          {/* The measured precision of the run, drawn rather than described: a bar
              that stops inside this band has not shown anything. */}
          <ReferenceArea
            x1={-noise}
            x2={noise}
            fill="var(--text-muted)"
            fillOpacity={0.1}
            ifOverflow="extendDomain"
          />
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = (payload[0]?.payload as { row?: Row } | undefined)?.row
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  subtitle={`Against a baseline at item level ${row.baselineIlevel}`}
                  rows={specs
                    .filter((spec) => row.gains[spec.id] !== undefined)
                    .map((spec) => {
                      const gain = row.gains[spec.id] ?? 0
                      const error = row.errors[spec.id] ?? 0
                      return {
                        id: spec.id,
                        label: spec.displayName,
                        color: colorOf(spec.id),
                        value:
                          Math.abs(gain) <= error
                            ? 'tie'
                            : `${gain > 0 ? '+' : ''}${percent(gain, 2)}`,
                        hint:
                          Math.abs(gain) <= error
                            ? `${spec.spec}: inside ±${percent(error, 2)}, so no difference was shown.`
                            : `${fullNumber(row.dps[spec.id] ?? 0)} DPS, ±${percent(error, 2)}.`,
                      }
                    })}
                />
              )
            }}
          />
          {specs.map((spec) => (
            <Bar
              key={spec.id}
              dataKey={spec.id}
              fill={colorOf(spec.id)}
              barSize={9}
              radius={2}
              isAnimationActive={false}
              // 2px surface gap between adjacent fills, per the mark spec.
              stroke="var(--surface-1)"
              strokeWidth={1}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Tables
// --------------------------------------------------------------------------------

function GainTable({
  rows,
  specs,
  colorOf,
}: {
  rows: Row[]
  specs: GearSpecResult[]
  colorOf: (id: string) => string
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Item</th>
            {specs.map((spec) => (
              <th key={spec.id} className="py-2 pr-4 text-right font-medium">
                <span className="inline-flex items-center gap-1.5">
                  <Dot color={colorOf(spec.id)} />
                  {spec.spec} {spec.class}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.itemId} className="border-b border-hairline/60 last:border-0">
              <td className="py-2 pr-4 pl-5 text-ink">{row.label}</td>
              {specs.map((spec) => {
                const gain = row.gains[spec.id]
                const error = row.errors[spec.id] ?? 0
                if (gain === undefined) {
                  return (
                    <td key={spec.id} className="py-2 pr-4 text-right text-ink-muted">
                      –
                    </td>
                  )
                }
                const tie = Math.abs(gain) <= error
                return (
                  <td
                    key={spec.id}
                    className={`py-2 pr-4 text-right tabular-nums ${
                      tie ? 'text-ink-muted' : 'text-ink'
                    }`}
                    title={`${fullNumber(row.dps[spec.id] ?? 0)} DPS, ±${percent(error, 2)}`}
                  >
                    {tie ? 'tie' : `${gain > 0 ? '+' : ''}${percent(gain, 2)}`}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function BaselineTable({
  specs,
  targetCount,
  items,
  colorOf,
}: {
  specs: GearSpecResult[]
  targetCount: number
  items: Map<number, GearItemMeta>
  colorOf: (id: string) => string
}) {
  const name = (id: number) => items.get(id)?.name ?? `Item ${id}`
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Build</th>
            <th className="py-2 pr-4 font-medium">Kept</th>
            <th className="py-2 pr-4 font-medium">Replaced by candidates</th>
            <th className="py-2 pr-4 text-right font-medium">Baseline DPS</th>
            <th className="py-2 pr-4 pr-5 font-medium">Next best</th>
          </tr>
        </thead>
        <tbody>
          {specs.map((spec) => {
            const target = spec.targets.find((entry) => entry.targets === targetCount)
            if (!target) return null
            const [kept, replaced] = target.baseline.items
            const runnersUp = target.pool.filter((entry) => !entry.chosen).slice(0, 2)
            return (
              <tr key={spec.id} className="border-b border-hairline/60 last:border-0">
                <td className="py-2 pr-4 pl-5">
                  <span className="inline-flex items-center gap-2">
                    <Dot color={colorOf(spec.id)} />
                    <span className="text-ink">{spec.displayName}</span>
                  </span>
                </td>
                <td className="py-2 pr-4 text-ink-secondary">{kept ? name(kept) : '—'}</td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {replaced ? name(replaced) : '—'}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-ink">
                  {fullNumber(target.baseline.dps)}
                </td>
                <td className="py-2 pr-5 text-ink-muted">
                  {runnersUp.length
                    ? runnersUp
                        .map(
                          (entry) =>
                            `${name(entry.id)} (${fullNumber(entry.standaloneGain)} alone)`,
                        )
                        .join(', ')
                    : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Spec ids the gear dataset actually covers, in dataset order. */
export function gearSpecIds(gear: GearDataset | null): string[] {
  const slot: GearSlot | undefined = gear?.slots[0]
  return (slot?.specs ?? []).map((spec) => spec.id)
}
