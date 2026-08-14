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

import { useMemo, useState, type ReactNode } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceArea,
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
import { GameLink } from '../components/GameLink'
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
import { classColor } from '../lib/palette'
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
  // "Which build should get this?" is the loot-council question, and it is the
  // transpose of "what should this build hope for": one item, every build, rather
  // than a handful of builds and every item.
  const [mode, setMode] = useState<'byBuild' | 'byItem'>('byItem')
  const [itemId, setItemId] = useState<number | null>(null)

  const specs = useMemo(
    () => (slot?.specs ?? []).filter((spec) => visible.includes(spec.id)),
    [slot, visible],
  )

  const targetCounts = useMemo(() => {
    const counts = new Set<number>()
    for (const spec of slot?.specs ?? [])
      for (const entry of spec.targets) counts.add(entry.targets)
    return [...counts].sort((a, b) => a - b)
  }, [slot])

  const level =
    slot?.itemLevels.find((entry) => entry.id === levelId) ??
    slot?.itemLevels[0]
  const targetCount = targets ?? targetCounts[0] ?? 1

  const items = useMemo(
    () => new Map((slot?.items ?? []).map((item) => [item.id, item])),
    [slot],
  )

  const rows = useMemo(
    () => (level ? buildRows(specs, targetCount, level.id, items) : []),
    [specs, targetCount, level, items],
  )
  // Item mode covers the whole sweep, not the picker's six: a loot council needs
  // every candidate on screen, and one item across many builds is a single series,
  // so it does not spend a categorical colour slot per build.
  const allRows = useMemo(
    () =>
      level ? buildRows(slot?.specs ?? [], targetCount, level.id, items) : [],
    [slot, targetCount, level, items],
  )
  const activeItem = allRows.find((row) => row.itemId === itemId) ?? allRows[0]
  const itemRows = useMemo(
    () => (activeItem ? buildItemRows(activeItem, slot?.specs ?? []) : []),
    [activeItem, slot],
  )
  const itemNoise = useMemo(
    () => medianNoise(activeItem ? [activeItem] : []),
    [activeItem],
  )

  const noise = useMemo(() => medianNoise(rows), [rows])
  // The headline has to describe what is on screen. Item mode charts every build
  // the sweep covered, so its tiles are computed over all of them -- quoting "3 of
  // 6 builds shown" above a chart of twenty-six would be a different claim than the
  // one the reader is looking at.
  const tileScope = mode === 'byItem' ? (slot?.specs ?? []) : specs
  // The series palette hands out six slots and falls back to the first for anything
  // else, so it cannot colour twenty-six rows. Class colour is the right cue for a
  // table anyway: an identity mark beside a name, never a series encoding.
  const identityColor =
    mode === 'byItem'
      ? (id: string) => classColor(tileScope.find((spec) => spec.id === id)?.class ?? '')
      : colorOf
  const tileRows = mode === 'byItem' ? allRows : rows
  const tiles = useMemo(
    () =>
      headline(
        tileRows,
        tileScope,
        mode === 'byItem' ? itemNoise : noise,
        level?.ilevel ?? 0,
      ),
    [tileRows, tileScope, mode, itemNoise, noise, level],
  )

  if (!gear || !slot || !level) {
    return (
      <Panel>
        <PanelHeader title="Loot" />
        <EmptyState>
          No gear comparison has been generated for this tier yet. It is a
          separate pass — <code>wowdps gear</code> — because it costs roughly a
          full simulation matrix of its own.
        </EmptyState>
      </Panel>
    )
  }

  const controls = (
    <>
      <SegmentedControl
        label="Show"
        value={mode}
        onChange={setMode}
        options={[
          { value: 'byItem', label: "One item, every build" },
          { value: 'byBuild', label: "Picked builds, every item" },
        ]}
      />
      {mode === 'byItem' && allRows.length ? (
        <Select
          label="Item"
          value={activeItem?.itemId ?? allRows[0]!.itemId}
          onChange={setItemId}
          options={allRows.map((row) => ({
            value: row.itemId,
            label: row.label,
          }))}
        />
      ) : null}
      {targetCounts.length > 1 ? (
        <Select
          label="Targets"
          value={targetCount}
          onChange={setTargets}
          options={targetCounts.map((count) => ({
            value: count,
            label: count === 1 ? "1 (single target)" : String(count),
          }))}
        />
      ) : null}
      <SegmentedControl
        label="Item level"
        value={level.id}
        onChange={setLevelId}
        options={slot.itemLevels.map((entry) => ({
          value: entry.id,
          label: `${entry.label.split(",")[0]} · ${entry.ilevel}`,
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
              Zero is each spec’s own baseline: its two best{" "}
              {slot.baselineSourceLabel} {slot.label.toLowerCase()}s at item
              level {rows[0]?.baselineIlevel ?? "—"}. A candidate takes the
              place of the <em>weaker</em> of the two, because that is the
              decision a loot council actually makes. Covers{" "}
              {gear.coverage.specs} of {gear.coverage.specsAvailable} builds in
              the tier.
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
                accent={tile.specId ? identityColor(tile.specId) : undefined}
              />
            ))}
          </div>
        ) : null}
      </Panel>

      {mode === 'byItem' ? (
        <Panel>
          <PanelHeader
            title={
              activeItem ? (
                <>
                  <GameLink
                    kind="item"
                    id={activeItem.itemId}
                    name={activeItem.label}
                    ilevel={level.ilevel}
                  />{" "}
                  at {level.ilevel}
                </>
              ) : (
                "No item selected"
              )
            }
            subtitle={
              <>
                Every build the sweep covered, best first. Bars inside the
                shaded band are ties rather than gains. Colour marks direction,
                not identity — the dot beside each name is the class colour.
              </>
            }
          />
          {itemRows.length === 0 ? (
            <EmptyState>
              No build has a result for this item at this item level.
            </EmptyState>
          ) : (
            <>
              <ItemChart rows={itemRows} noise={itemNoise} />
              <ItemTable rows={itemRows} noise={itemNoise} />
              <Note>
                A loot council reading: hand it to the build nearest the top
                whose bar clears the band. Anything inside the band is a coin
                toss, and a negative bar means the drop is worse than what that
                build already wears.
              </Note>
            </>
          )}
        </Panel>
      ) : null}

      {mode === 'byBuild' ? (
        <>
          <Panel>
            <PanelHeader
              title={`Gain over baseline at ${level.ilevel}`}
              subtitle={level.evidence}
            />
            {rows.length === 0 ? (
              <EmptyState>
                None of the selected builds has a gear sweep at {targetCount}{" "}
                {targetCount === 1 ? "target" : "targets"}.
              </EmptyState>
            ) : (
              <>
                <GainChart
                  rows={rows}
                  specs={specs}
                  noise={noise}
                  colorOf={colorOf}
                />
                <Legend
                  items={specs.map((spec) => ({
                    id: spec.id,
                    label: spec.displayName,
                    color: colorOf(spec.id),
                  }))}
                />
                <Note>
                  The shaded band is ±{percent(noise, 2)}, the median of the two
                  runs’ standard errors added in quadrature. A bar inside it is
                  a tie, not a lead. Item levels come from the upgrade ladder
                  measured in simc’s own data, not from a track name — simc’s
                  files do not carry Blizzard’s track labels. Item names in the
                  table below link to Wowhead; the axis labels here are drawn
                  inside the chart, where the tooltip cannot follow.
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
              <GainTable
                rows={rows}
                specs={specs}
                colorOf={colorOf}
                ilevel={level.ilevel}
              />
            </Panel>
          ) : null}
        </>
      ) : null}

      <Panel>
        <PanelHeader
          title={`The baseline: each build’s two best ${slot.baselineSourceLabel} ${slot.label.toLowerCase()}s`}
          subtitle={`Chosen by running every eligible ${slot.baselineSourceLabel} ${slot.label.toLowerCase()} on its own, with the other socket empty, and taking the two that added the most. Runners-up are listed so a close call at the cut is visible.`}
        />
        <BaselineTable
          specs={tileScope}
          targetCount={targetCount}
          items={items}
          colorOf={identityColor}
        />
        <Note>
          Standalone value is not perfectly additive — measured on Arcane Mage,
          a pair is worth about 3% more than the sum of its two parts — so two
          items within a few percent of each other at the cut could swap once
          paired. Hovering an item name asks Wowhead for its card at the item
          level in play; any difficulty name printed in that card is Wowhead’s
          reading of the item level, not something simc’s files say.
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
    row.mean = values.length
      ? values.reduce((sum, value) => sum + value, 0) / values.length
      : 0
    row.best = values.length ? Math.max(...values) : 0
  }
  // Ordered by the best result any shown spec gets, so the top of the chart is
  // "what to hand out first" rather than an alphabetical list.
  return rows.sort((a, b) => b.best - a.best)
}

interface ItemRow {
  specId: string
  label: string
  wowClass: string
  gain: number
  error: number
  dps: number
}

/** One item's result for every build that was swept, best first. */
function buildItemRows(row: Row, specs: GearSpecResult[]): ItemRow[] {
  return specs
    .filter((spec) => row.gains[spec.id] !== undefined)
    .map((spec) => ({
      specId: spec.id,
      label: spec.displayName,
      wowClass: spec.class,
      gain: row.gains[spec.id] ?? 0,
      error: row.errors[spec.id] ?? 0,
      dps: row.dps[spec.id] ?? 0,
    }))
    .sort((a, b) => b.gain - a.gain)
}

/** Median combined standard error across everything on screen, as a fraction. */
function medianNoise(rows: Row[]): number {
  const errors = rows
    .flatMap((row) => Object.values(row.errors))
    .sort((a, b) => a - b)
  if (errors.length === 0) return 0
  const middle = Math.floor(errors.length / 2)
  return errors.length % 2
    ? (errors[middle] ?? 0)
    : ((errors[middle - 1] ?? 0) + (errors[middle] ?? 0)) / 2
}

interface Tile {
  label: string
  value: ReactNode
  caption: ReactNode
  specId?: string
}

function headline(
  rows: Row[],
  specs: GearSpecResult[],
  noise: number,
  ilevel: number,
): Tile[] {
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
    const row = bestRow
    tiles.push({
      label: "Biggest single upgrade",
      value: `${percent(bestGain)}`,
      caption: (
        <>
          <GameLink
            kind="item"
            id={row.itemId}
            name={row.label}
            ilevel={ilevel}
          />{" "}
          for {bestSpec.displayName}.
        </>
      ),
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
      if (!top || gain > (top.gains[spec.id] ?? Number.NEGATIVE_INFINITY))
        top = row
    }
    if (top) wins.set(top.itemId, (wins.get(top.itemId) ?? 0) + 1)
  }
  const contested = [...wins.entries()].sort((a, b) => b[1] - a[1])[0]
  if (contested) {
    const row = rows.find((entry) => entry.itemId === contested[0])
    tiles.push({
      label: "Most contested",
      value: row ? (
        <GameLink
          kind="item"
          id={row.itemId}
          name={row.label}
          ilevel={ilevel}
        />
      ) : (
        "—"
      ),
      caption: `Top pick for ${contested[1]} of ${specs.length} builds shown.`,
    })
  }

  const comparisons = rows.flatMap((row) =>
    specs
      .map((spec) => row.gains[spec.id])
      .filter((gain): gain is number => gain !== undefined),
  )
  const ties = comparisons.filter((gain) => Math.abs(gain) <= noise).length
  tiles.push({
    label: "Too close to call",
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
    ...Object.fromEntries(
      specs.map((spec) => [spec.id, row.gains[spec.id] ?? null]),
    ),
    row,
  }))
  const height =
    rows.length * Math.max(ROW_HEIGHT, specs.length * 11 + 14) + AXIS_BAND

  return (
    <div className="px-2 pb-1">
      <ResponsiveContainer width="100%" height={Math.max(240, height)}>
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 4, right: 28, bottom: 8, left: 8 }}
        >
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
            fill='var(--text-muted)'
            fillOpacity={0.1}
            ifOverflow="extendDomain"
          />
          <ReferenceLine x={0} stroke='var(--baseline)' />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = (payload[0]?.payload as { row?: Row } | undefined)
                ?.row
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
                            ? "tie"
                            : `${gain > 0 ? "+" : ""}${percent(gain, 2)}`,
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
              stroke='var(--surface-1)'
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

function ItemChart({ rows, noise }: { rows: ItemRow[]; noise: number }) {
  const span = Math.max(...rows.map((row) => Math.abs(row.gain)), noise, 0.005)
  return (
    <div className="px-2 pb-2">
      <ResponsiveContainer width="100%" height={rows.length * 26 + AXIS_BAND}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 56, bottom: 4, left: 4 }}
          barCategoryGap={3}
        >
          <CartesianGrid {...GRID} horizontal={false} />
          <XAxis
            type="number"
            domain={[-span * 1.08, span * 1.08]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => percent(value)}
          />
          <YAxis
            type="category"
            dataKey="label"
            tick={AXIS_TICK}
            axisLine={false}
            tickLine={false}
            width={230}
          />
          {/* Inside this band the two runs cannot be told apart. */}
          <ReferenceArea
            x1={-noise}
            x2={noise}
            fill='var(--text-muted)'
            fillOpacity={0.1}
          />
          <ReferenceLine x={0} stroke='var(--hairline)' />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as ItemRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.label}
                  rows={[
                    {
                      id: "gain",
                      label: Math.abs(row.gain) <= row.error ? "Tie" : "Gain",
                      value: `${row.gain >= 0 ? "+" : ""}${percent(row.gain)}`,
                    },
                    {
                      id: "dps",
                      label: "With it equipped",
                      value: fullNumber(row.dps),
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="gain" isAnimationActive={false} radius={2}>
            {rows.map((row) => (
              <Cell
                key={row.specId}
                // One item across many builds is one series, so it gets one colour.
                // Class colour would be a thirteen-way categorical scale and is
                // nowhere near colour-blind safe; it stays a dot beside the name.
                fill={Math.abs(row.gain) <= row.error
                    ? 'var(--text-muted)'
                    : row.gain >= 0
                      ? 'var(--series-1)'
                      : 'var(--series-2)'
                }
                fillOpacity={Math.abs(row.gain) <= row.error ? 0.45 : 1}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function ItemTable({ rows, noise }: { rows: ItemRow[]; noise: number }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Build</th>
            <th className="py-2 pr-4 text-right font-medium">Gain</th>
            <th className="py-2 pr-5 text-right font-medium">DPS with it</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const tie = Math.abs(row.gain) <= Math.max(row.error, noise)
            return (
              <tr
                key={row.specId}
                className="border-b border-hairline/60 last:border-0"
              >
                <td className="py-2 pr-4 pl-5">
                  <span className="inline-flex items-center gap-2">
                    <Dot color={classColor(row.wowClass)} ring />
                    <span className="text-ink">{row.label}</span>
                  </span>
                </td>
                <td
                  className={`py-2 pr-4 text-right tabular-nums ${
                    tie ? "text-ink-muted" : "text-ink"
                  }`}
                >
                  {tie
                    ? "tie"
                    : `${row.gain >= 0 ? "+" : ""}${percent(row.gain)}`}
                </td>
                <td className="py-2 pr-5 text-right tabular-nums text-ink-secondary">
                  {fullNumber(row.dps)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function GainTable({
  rows,
  specs,
  colorOf,
  ilevel,
}: {
  rows: Row[]
  specs: GearSpecResult[]
  colorOf: (id: string) => string
  /** Item level these candidates were run at, so the hover card matches the row. */
  ilevel: number
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
            <tr
              key={row.itemId}
              className="border-b border-hairline/60 last:border-0"
            >
              <td className="py-2 pr-4 pl-5 text-ink">
                <GameLink
                  kind="item"
                  id={row.itemId}
                  name={row.label}
                  ilevel={ilevel}
                />
              </td>
              {specs.map((spec) => {
                const gain = row.gains[spec.id]
                const error = row.errors[spec.id] ?? 0
                if (gain === undefined) {
                  return (
                    <td
                      key={spec.id}
                      className="py-2 pr-4 text-right text-ink-muted"
                    >
                      –
                    </td>
                  )
                }
                const tie = Math.abs(gain) <= error
                return (
                  <td
                    key={spec.id}
                    className={`py-2 pr-4 text-right tabular-nums ${
                      tie ? "text-ink-muted" : "text-ink"
                    }`}
                    title={`${fullNumber(row.dps[spec.id] ?? 0)} DPS, ±${percent(error, 2)}`}
                  >
                    {tie ? "tie" : `${gain > 0 ? "+" : ""}${percent(gain, 2)}`}
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
  // The baseline is worn at the lower of the two item levels, and every pool
  // entry carries the level it was measured at, so each link asks Wowhead about
  // the item at the level this row's numbers came from.
  const link = (id: number, ilevel: number) => (
    <GameLink
      kind="item"
      id={id}
      name={items.get(id)?.name ?? `Item ${id}`}
      ilevel={ilevel}
    />
  )
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
            const target = spec.targets.find(
              (entry) => entry.targets === targetCount,
            )
            if (!target) return null
            const [kept, replaced] = target.baseline.items
            const runnersUp = target.pool
              .filter((entry) => !entry.chosen)
              .slice(0, 2)
            return (
              <tr
                key={spec.id}
                className="border-b border-hairline/60 last:border-0"
              >
                <td className="py-2 pr-4 pl-5">
                  <span className="inline-flex items-center gap-2">
                    <Dot color={colorOf(spec.id)} />
                    <span className="text-ink">{spec.displayName}</span>
                  </span>
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {kept ? link(kept, target.baseline.ilevel) : "—"}
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {replaced ? link(replaced, target.baseline.ilevel) : "—"}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-ink">
                  {fullNumber(target.baseline.dps)}
                </td>
                <td className="py-2 pr-5 text-ink-muted">
                  {runnersUp.length
                    ? runnersUp.map((entry, index) => (
                        <span key={entry.id}>
                          {index ? ", " : null}
                          {link(entry.id, entry.ilevel)} (
                          {fullNumber(entry.standaloneGain)} alone)
                        </span>
                      ))
                    : "—"}
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
