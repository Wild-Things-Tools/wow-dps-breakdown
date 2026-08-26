/**
 * Loot: is this drop an upgrade over what the character already wears?
 *
 * The charts are *diverging* bars around zero, and zero is not "no damage" — it is
 * the spec's own baseline, the best set of Mythic+ items it already has, worn at the
 * lower of the two item levels. So a bar to the left is a real answer ("this drop is
 * a downgrade for this spec"), not a missing one, and the baseline has to be named
 * on screen rather than implied.
 *
 * Two questions, not one, and the view has to keep them apart. "Is this drop an
 * upgrade today" is the per-item comparison: one socket swapped, everything else
 * held. "What should this slot end up holding" is the ceiling, chosen from every
 * combination the pool allows — and it can name a set the first question cannot
 * reach, because that one only ever swaps a single socket out of the farmed pair.
 * Both are published; where they disagree, the disagreement is the finding.
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
 * Form. This view used to have a second mode, "picked builds, every item", which
 * only made sense behind the six-build picker that no longer exists. What is left
 * is the mode that always worked without one: every build the sweep covered, best
 * first, one item at a time — plus a new panel that answers the loot council's
 * other question, "what should each build hope for", for every build at once.
 * Colour is the class colour throughout, except where a mark encodes *direction*
 * rather than identity, which is called out where it happens.
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
import { AXIS_LINE, AXIS_TICK, CURSOR_FILL, GRID, TooltipCard } from '../components/chart'
import { BuildIdentity, makeBuildTick } from '../components/BuildIdentity'
import { GameLink } from '../components/GameLink'
import {
  EmptyState,
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
  GearRunnerUp,
  GearSlot,
  GearSpecResult,
} from '../lib/types'
import { sweepCoverage } from '../lib/sweepCoverage'

/** Two lines of tick text plus the icon, and the axis band beneath the plot. */
const ROW_HEIGHT = 32

/**
 * A percentage whose sign comes from the number rather than from the call site.
 *
 * `percent()` renders a negative as "-0.40%", so a template that prefixes its own
 * minus for a figure it assumes is a lead prints "−-0.40%" the moment the assumption
 * breaks — a malformed string that reads as the opposite of what happened. Every
 * signed figure on this view goes through here so the two can never disagree.
 */
function signedPercent(value: number, digits = 1): string {
  return `${value >= 0 ? '+' : '−'}${percent(Math.abs(value), digits)}`
}
const AXIS_BAND = 44
const TICK_WIDTH = 205

export function GearView({
  gear,
  tierBuilds,
}: {
  gear: GearDataset | null
  /**
   * How many builds the tier holds RIGHT NOW, from the manifest.
   *
   * Not readable from `gear.json`: its `coverage.specsAvailable` is the tier's
   * size on the day the sweep ran, so comparing the two says only that the sweep
   * was internally consistent. On 2026-08-26 that printed "Covers 28 of 28 builds
   * in the tier" against a tier of 52.
   */
  tierBuilds: number | null
}) {
  // A slot with no swept spec is a pool nobody has run yet -- offering it would open
  // on an empty comparison. The dataset carries every pool as a slot precisely so
  // that gap is visible in the data; the view just does not make it the landing page.
  const swept = useMemo(
    () => (gear?.slots ?? []).filter((entry) => entry.specs.length > 0),
    [gear],
  )
  // Slots arrive in id order, which is alphabetical and would open the view on
  // rings. The landing slot is the richest comparison instead -- most items in the
  // pool -- which is a property of the data rather than a name hard-coded here, and
  // picks the trinket sweep for as long as it is the largest.
  // The sweep's own two numbers describe the sweep. The tier's size comes from the
  // manifest, which is the only thing that can say whether the sweep is behind.
  const coverage = useMemo(
    () => sweepCoverage(gear?.coverage.specs ?? 0, gear?.coverage.specsAvailable, tierBuilds),
    [gear, tierBuilds],
  )
  const richest = useMemo(
    () => [...swept].sort((a, b) => b.items.length - a.items.length)[0] ?? null,
    [swept],
  )
  const [slotId, setSlotId] = useState<string | null>(null)
  const slot = swept.find((entry) => entry.id === slotId) ?? richest
  const [levelId, setLevelId] = useState<string | null>(null)
  const [targets, setTargets] = useState<number | null>(null)
  const [itemId, setItemId] = useState<number | null>(null)

  const specs = useMemo(() => slot?.specs ?? [], [slot])

  const targetCounts = useMemo(() => {
    const counts = new Set<number>()
    for (const spec of specs) for (const entry of spec.targets) counts.add(entry.targets)
    return [...counts].sort((a, b) => a - b)
  }, [specs])

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
  const best = useMemo(() => bestPerSpec(rows, specs), [rows, specs])

  // Default to the item that tops the most builds' lists -- the one a loot council
  // will actually argue over -- rather than to whatever sorts first.
  const contestedId = useMemo(() => mostContested(best), [best])
  const activeItem =
    rows.find((row) => row.itemId === itemId) ??
    rows.find((row) => row.itemId === contestedId) ??
    rows[0]
  const itemRows = useMemo(
    () => (activeItem ? buildItemRows(activeItem, specs) : []),
    [activeItem, specs],
  )
  const itemNoise = useMemo(() => medianNoise(activeItem ? [activeItem] : []), [activeItem])

  const tiles = useMemo(
    () => headline(rows, specs, noise, level?.ilevel ?? 0),
    [rows, specs, noise, level],
  )
  const ceilings = useMemo(
    () => buildCeilings(specs, targetCount, level?.id ?? ''),
    [specs, targetCount, level],
  )

  if (!gear || !slot || !level) {
    return (
      <Panel>
        <PanelHeader title="Loot" />
        <EmptyState>
          No gear comparison has been generated for this tier yet. It is a separate pass —{' '}
          <code>wowdps gear</code> — because it costs roughly a full simulation matrix of
          its own.
        </EmptyState>
      </Panel>
    )
  }

  const controls = (
    <>
      {swept.length > 1 ? (
        <Select
          label="Slot"
          value={slot.id}
          onChange={(value) => {
            setSlotId(value)
            // Item levels and the picked item belong to the slot that was showing.
            setLevelId(null)
            setItemId(null)
          }}
          options={swept.map((entry) => ({ value: entry.id, label: entry.label }))}
        />
      ) : null}
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
              Zero is each spec’s own baseline: the best set of{' '}
              {slot.baselineSourceLabel} {slot.label.toLowerCase()}s it can farm, at item
              level {rows[0]?.baselineIlevel ?? '—'}. A candidate takes the place of the{' '}
              <em>weakest</em> item in that set, because that is the decision a loot
              council actually makes. {coverage.sentence}
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
                accent={tile.wowClass ? classColor(tile.wowClass) : undefined}
              />
            ))}
          </div>
        ) : null}
      </Panel>

      <Panel>
        <PanelHeader
          title="The best drop for each build"
          subtitle={`Every build the sweep covered, with the single ${slot.candidateSourceLabel} ${slot.label.toLowerCase()} that gains it the most at item level ${level.ilevel}. Nothing is selected — this is the whole sweep.`}
        />
        {best.length === 0 ? (
          <EmptyState>
            None of the swept builds has a result at {targetCount}{' '}
            {targetCount === 1 ? 'target' : 'targets'}.
          </EmptyState>
        ) : (
          <>
            <BestChart rows={best} noise={noise} />
            <BestTable rows={best} noise={noise} ilevel={level.ilevel} />
            <Note>
              The shaded band is ±{percent(noise, 2)}, the median of the two runs’ standard
              errors added in quadrature. A build whose best candidate stops inside it has
              no upgrade here at all. Item levels come from the upgrade ladder measured in
              simc’s own data, not from a track name — simc’s files do not carry
              Blizzard’s track labels.
            </Note>
          </>
        )}
      </Panel>

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
                />{' '}
                at {level.ilevel}
              </>
            ) : (
              'No item selected'
            )
          }
          subtitle="Every build the sweep covered, best first. Bars inside the shaded band are ties rather than gains."
          actions={
            rows.length ? (
              <Select
                label="Item"
                value={activeItem?.itemId ?? rows[0]!.itemId}
                onChange={setItemId}
                options={rows.map((row) => ({ value: row.itemId, label: row.label }))}
              />
            ) : null
          }
        />
        {itemRows.length === 0 ? (
          <EmptyState>No build has a result for this item at this item level.</EmptyState>
        ) : (
          <>
            <ItemChart rows={itemRows} noise={itemNoise} />
            <ItemTable rows={itemRows} noise={itemNoise} />
            <Note>
              A loot council reading: hand it to the build nearest the top whose bar clears
              the band. Anything inside the band is a coin toss, and a negative bar means
              the drop is worse than what that build already wears. The bar colour here is
              the class colour, as everywhere; the shading is direction, and the table
              spells both out.
            </Note>
          </>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title={`The best ${slot.label.toLowerCase()}s each build could wear at ${level.ilevel}`}
          subtitle={
            // Past tense only when something ran. The panel used to assert "was run,
            // and this is the set that won" above an empty state explaining that
            // nothing had been — two adjacent sentences contradicting each other, and
            // the same defect as giving one cause for four.
            ceilings.length === 0 ? (
              <>
                Not the baseline plus its best single drop: the best set out of every way
                of filling {slot.sockets.length === 1 ? 'the socket' : 'every socket'} from
                the whole pool — {slot.baselineSourceLabel} and {slot.candidateSourceLabel}{' '}
                together. The panel above only ever swaps one socket out of what the build
                already farms, so it cannot propose a set whose{' '}
                {slot.baselineSourceLabel} half the baseline did not name.
              </>
            ) : (
              <>
                Not the baseline plus its best single drop. Every way of filling{' '}
                {slot.sockets.length === 1 ? 'the socket' : 'every socket'} from the whole
                pool — {slot.baselineSourceLabel} and {slot.candidateSourceLabel} together —
                was run, and this is the set that won. The panel above swaps one socket out
                of what the build already farms, so it can never propose a set whose{' '}
                {slot.baselineSourceLabel} half the baseline did not name.
              </>
            )
          }
        />
        {ceilings.length === 0 ? (
          <EmptyState>
            <CeilingAbsence specs={specs} targetCount={targetCount} slot={slot} />
          </EmptyState>
        ) : (
          <>
            <CeilingTable rows={ceilings} items={items} ilevel={level.ilevel} />
            <Note>
              <strong>Beyond one swap</strong> marks the builds where the winning set is
              one the per-item comparison could not have reached: it either keeps a{' '}
              {slot.baselineSourceLabel} item the baseline left out, or fills every socket
              from the drop pool.
              Those are the rows where reading the per-item chart alone gives the wrong
              answer to “what should I end up wearing”. A gain inside the run’s own
              noise still reads as a tie, here as everywhere — and “baseline” in the
              gain column is a real answer, meaning no drop at this item level belongs in
              the slot at all.
            </Note>
          </>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title={`The baseline: each build’s best ${slot.baselineSourceLabel} ${slot.label.toLowerCase()}s`}
          subtitle={
            <>
              What the build already wears, and the zero every gain above is measured
              from. Chosen by filling{' '}
              {slot.sockets.length === 1 ? 'the socket' : 'every socket'} every possible
              way from the eligible {slot.baselineSourceLabel} {slot.label.toLowerCase()}s
              and running each, so it is the set that won rather than the items that ranked
              highest on their own. Those are different questions: measured on rings, the
              two named a different pair on half the tier. Where a pool is too large to
              enumerate, the top items by standalone value are taken instead, and the note
              under the table says which happened.
            </>
          }
        />
        <BaselineTable specs={specs} targetCount={targetCount} items={items} />
        <BaselineMethodNote specs={specs} targetCount={targetCount} />
        <Note>
          This pool is still selected by item level rather than by where an item drops,
          and that is wrong in two directions. It cannot tell this season’s dungeon
          trinkets from last season’s — measured against simc’s own table, the two are
          identical in every field it ships — so a few trinkets nobody can farm right now
          are still anchoring a baseline here. And it misses the rotation’s dungeons from
          older expansions entirely, whose trinkets carry item levels from a different
          block. Both are fixed by <code>wowdps gear-pool</code>, which builds the pool
          from Blizzard’s own drop tables — the raid’s encounters for the candidates,
          this season’s eight dungeons for the baseline — and needs API credentials to
          run. Until it has, read the baseline as “the expansion’s dungeon trinkets”
          rather than “this season’s”.
        </Note>
        <Note>
          “Alone” is each item measured by itself with every other socket empty, and it
          is shown because it is legible, not because it decides anything: standalone
          value is not additive — measured on Arcane Mage, a pair is worth about 3% more
          than the sum of its parts — so the item that adds most alone is regularly not
          in the pair that wins. Where the runner-up set is marked a tie, the two
          differ by less than this run measured to, and the choice between them is a coin
          toss rather than a finding. Hovering an item name asks Wowhead for its card at
          the item level in play; any difficulty name printed in that card is Wowhead’s
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
    row.mean = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0
    row.best = values.length ? Math.max(...values) : 0
  }
  // Ordered by the best result any shown spec gets, so the top of the list is
  // "what to hand out first" rather than an alphabetical list.
  return rows.sort((a, b) => b.best - a.best)
}

interface ItemRow {
  specId: string
  build: GearSpecResult
  label: string
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
      build: spec,
      label: spec.displayName,
      gain: row.gains[spec.id] ?? 0,
      error: row.errors[spec.id] ?? 0,
      dps: row.dps[spec.id] ?? 0,
    }))
    .sort((a, b) => b.gain - a.gain)
}

interface BestRow {
  build: GearSpecResult
  label: string
  itemId: number
  itemName: string
  gain: number
  error: number
  dps: number
  /** The next-best candidate, so a close call at the top is visible. */
  runnerUp?: { itemId: number; itemName: string; gain: number }
}

/** For each swept build, the candidate that gains it the most. */
function bestPerSpec(rows: Row[], specs: GearSpecResult[]): BestRow[] {
  const out: BestRow[] = []
  for (const spec of specs) {
    const ranked = rows
      .filter((row) => row.gains[spec.id] !== undefined)
      .map((row) => ({ row, gain: row.gains[spec.id] ?? 0 }))
      .sort((a, b) => b.gain - a.gain)
    const top = ranked[0]
    if (!top) continue
    const second = ranked[1]
    out.push({
      build: spec,
      label: spec.displayName,
      itemId: top.row.itemId,
      itemName: top.row.label,
      gain: top.gain,
      error: top.row.errors[spec.id] ?? 0,
      dps: top.row.dps[spec.id] ?? 0,
      runnerUp: second
        ? { itemId: second.row.itemId, itemName: second.row.label, gain: second.gain }
        : undefined,
    })
  }
  return out.sort((a, b) => b.gain - a.gain)
}

function mostContested(best: BestRow[]): number | null {
  const wins = new Map<number, number>()
  for (const row of best) wins.set(row.itemId, (wins.get(row.itemId) ?? 0) + 1)
  return [...wins.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null
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
  value: ReactNode
  caption: ReactNode
  wowClass?: string
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
    const spec = bestSpec
    tiles.push({
      label: 'Biggest single upgrade',
      value: `${percent(bestGain)}`,
      caption: (
        <>
          <GameLink kind="item" id={row.itemId} name={row.label} ilevel={ilevel} /> for{' '}
          {spec.displayName}.
        </>
      ),
      wowClass: spec.class,
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
      value: row ? (
        <GameLink kind="item" id={row.itemId} name={row.label} ilevel={ilevel} />
      ) : (
        '—'
      ),
      caption: `Top pick for ${contested[1]} of ${specs.length} builds swept.`,
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
// Charts
// --------------------------------------------------------------------------------

function BestChart({ rows, noise }: { rows: BestRow[]; noise: number }) {
  const byLabel = useMemo(() => new Map(rows.map((row) => [row.label, row.build])), [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: TICK_WIDTH }), [byLabel])
  const span = Math.max(...rows.map((row) => Math.abs(row.gain)), noise, 0.005) * 1.08

  return (
    <div className="px-2 pb-1">
      <ResponsiveContainer width="100%" height={rows.length * ROW_HEIGHT + AXIS_BAND}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 40, bottom: 8, left: 8 }}
          barCategoryGap={4}
        >
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            domain={[Math.min(-noise * 1.4, -span * 0.15), span]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => percent(value, 1)}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          <ReferenceArea x1={-noise} x2={noise} fill="var(--text-muted)" fillOpacity={0.1} />
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as BestRow | undefined
              if (!row) return null
              const tie = Math.abs(row.gain) <= Math.max(row.error, noise)
              return (
                <TooltipCard
                  title={row.label}
                  subtitle={row.itemName}
                  rows={[
                    {
                      id: 'gain',
                      label: tie ? 'Tie' : 'Best gain',
                      color: classColor(row.build.class),
                      value: `${row.gain >= 0 ? '+' : ''}${percent(row.gain, 2)}`,
                      hint: `±${percent(row.error, 2)} on this comparison.`,
                    },
                    ...(row.runnerUp
                      ? [
                          {
                            id: 'runner',
                            label: 'Next best',
                            value: `${percent(row.runnerUp.gain, 2)}`,
                            hint: row.runnerUp.itemName,
                          },
                        ]
                      : []),
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="gain" barSize={14} radius={2} isAnimationActive={false}>
            {rows.map((row) => (
              <Cell
                key={row.build.id}
                fill={classColor(row.build.class)}
                // Inside the noise band the run showed nothing, so the mark is
                // dimmed. Direction and certainty ride on top of identity here;
                // the table says both in words.
                fillOpacity={Math.abs(row.gain) <= Math.max(row.error, noise) ? 0.4 : 1}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function ItemChart({ rows, noise }: { rows: ItemRow[]; noise: number }) {
  const byLabel = useMemo(() => new Map(rows.map((row) => [row.label, row.build])), [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: TICK_WIDTH }), [byLabel])
  const span = Math.max(...rows.map((row) => Math.abs(row.gain)), noise, 0.005)

  return (
    <div className="px-2 pb-2">
      <ResponsiveContainer width="100%" height={rows.length * ROW_HEIGHT + AXIS_BAND}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 56, bottom: 4, left: 4 }}
          barCategoryGap={4}
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
            tick={tick}
            axisLine={false}
            tickLine={false}
            width={TICK_WIDTH}
          />
          {/* Inside this band the two runs cannot be told apart. */}
          <ReferenceArea x1={-noise} x2={noise} fill="var(--text-muted)" fillOpacity={0.1} />
          <ReferenceLine x={0} stroke="var(--hairline)" />
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
                      id: 'gain',
                      label: Math.abs(row.gain) <= row.error ? 'Tie' : 'Gain',
                      color: classColor(row.build.class),
                      value: `${row.gain >= 0 ? '+' : ''}${percent(row.gain)}`,
                    },
                    { id: 'dps', label: 'With it equipped', value: fullNumber(row.dps) },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="gain" isAnimationActive={false} radius={2} barSize={14}>
            {rows.map((row) => (
              <Cell
                key={row.specId}
                fill={classColor(row.build.class)}
                fillOpacity={Math.abs(row.gain) <= row.error ? 0.4 : 1}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Tables
// --------------------------------------------------------------------------------

function BestTable({
  rows,
  noise,
  ilevel,
}: {
  rows: BestRow[]
  noise: number
  ilevel: number
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Build</th>
            <th className="py-2 pr-4 font-medium">Best drop</th>
            <th className="py-2 pr-4 text-right font-medium">Gain</th>
            <th className="py-2 pr-5 font-medium">Next best</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const tie = Math.abs(row.gain) <= Math.max(row.error, noise)
            return (
              <tr key={row.build.id} className="border-b border-hairline/60 last:border-0">
                <td className="py-2 pr-4 pl-5">
                  <BuildIdentity build={row.build} />
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  <GameLink kind="item" id={row.itemId} name={row.itemName} ilevel={ilevel} />
                </td>
                <td
                  className={`py-2 pr-4 text-right tabular-nums ${tie ? 'text-ink-muted' : 'text-ink'}`}
                >
                  {tie ? 'tie' : `${row.gain >= 0 ? '+' : ''}${percent(row.gain, 2)}`}
                </td>
                <td className="py-2 pr-5 text-ink-muted">
                  {row.runnerUp ? (
                    <>
                      <GameLink
                        kind="item"
                        id={row.runnerUp.itemId}
                        name={row.runnerUp.itemName}
                        ilevel={ilevel}
                      />{' '}
                      <span className="tnum">({percent(row.runnerUp.gain, 2)})</span>
                    </>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ItemTable({ rows, noise }: { rows: ItemRow[]; noise: number }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[600px] text-[13px]">
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
              <tr key={row.specId} className="border-b border-hairline/60 last:border-0">
                <td className="py-2 pr-4 pl-5">
                  <BuildIdentity build={row.build} />
                </td>
                <td
                  className={`py-2 pr-4 text-right tabular-nums ${tie ? 'text-ink-muted' : 'text-ink'}`}
                >
                  {tie ? 'tie' : signedPercent(row.gain)}
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

/**
 * Why there is no ceiling, said from the data rather than assumed.
 *
 * Four conditions produce the same empty table and they are four different claims.
 * The first version of this gave one of them — "the pool was too big" — as the
 * explanation for all four, and on the dataset committed today it fired for rings
 * while telling the reader that rings fit inside the budget. That is the project's
 * own "probed and read nothing is not never probed" distinction, got wrong.
 */
function CeilingAbsence({
  specs,
  targetCount,
  slot,
}: {
  specs: GearSpecResult[]
  targetCount: number
  slot: GearSlot
}) {
  const targets = specs
    .map((spec) => spec.targets.find((entry) => entry.targets === targetCount))
    .filter((entry) => entry !== undefined)

  if (targets.length === 0) {
    return (
      <>
        No build in this sweep was run at {targetCount}{' '}
        {targetCount === 1 ? 'target' : 'targets'}. Pick another target count — this
        is a gap in what was swept, not an answer about the {slot.label.toLowerCase()}
        s.
      </>
    )
  }
  if (targets.every((entry) => entry.baseline.method === undefined)) {
    return (
      <>
        This sweep predates the ceiling. Its baselines were still published without
        saying which method chose them, so there is no enumeration over the whole pool
        to read a best set out of — the drops were only ever measured one socket at a
        time. Re-run <code>wowdps gear --slot {slot.id}</code> to fill this in. What
        is below is unaffected and still says what it always did.
      </>
    )
  }
  if (targets.some((entry) => entry.baseline.method === 'additive')) {
    return (
      <>
        This pool is too large to enumerate. Filling{' '}
        {slot.sockets.length === 1 ? 'the socket' : 'every socket'} every possible way
        from all {slot.items.length} {slot.label.toLowerCase()}s is more combinations
        than the sweep’s budget allows, so the baseline below was chosen by ranking
        items on their own and no best set was measured. “Nobody ran it” rather than
        “nothing beats the baseline” — the two look identical from a table and are not
        the same claim.
      </>
    )
  }
  return (
    <>
      The baseline here was enumerated and the ceiling was not: filling{' '}
      {slot.sockets.length === 1 ? 'the socket' : 'every socket'} from what the build
      farms fits the sweep’s budget, and doing it from the whole pool does not. That is
      deliberate — losing a measured baseline to the cost of a measured ceiling would
      be the worse trade — but it means nothing here has been compared against the
      drops except one socket at a time.
    </>
  )
}

/** One build's answer to "what should this slot end up holding". */
interface CeilingRow {
  build: GearSpecResult
  items: { id: number; ilevel: number }[]
  gain: number
  gainError: number
  /** The pipeline's verdict, carried rather than recomputed. */
  isTie: boolean
  isBaseline: boolean
  runnerUp: GearRunnerUp | null
  /**
   * True when the per-item comparison could not have proposed this set.
   *
   * That comparison holds every socket but one and swaps a single drop in, so the
   * only sets it can reach are the baseline itself and "baseline minus its weakest,
   * plus one candidate". Anything else — a farmed item the baseline left out, or
   * every socket filled from the drop pool — is invisible to it however many item
   * levels it tries. This is the whole reason the ceiling is published.
   */
  beyondOneSwap: boolean
}

function buildCeilings(
  specs: GearSpecResult[],
  targetCount: number,
  levelId: string,
): CeilingRow[] {
  const rows: CeilingRow[] = []
  for (const spec of specs) {
    const target = spec.targets.find((entry) => entry.targets === targetCount)
    const best = target?.bestSets?.find((entry) => entry.level === levelId)
    if (!target || !best) continue

    // What the per-item comparison could reach is not a rule to re-derive here — it
    // is a record of what the sweep ran, and every candidate carries it. Deriving it
    // as "the baseline minus its last item" repeats the exact defect the pipeline
    // had: the last item is pool-file order, not the weakest, so on any build where
    // those differ every reachable set would be built around the wrong survivor and
    // ceilings that *are* one swap away would be flagged as beyond it.
    const key = (ids: number[]) => [...ids].sort((a, b) => a - b).join('/')
    const reachable = new Set([key(target.baseline.items)])
    for (const candidate of target.candidates) {
      if (candidate.level !== levelId) continue
      const kept = target.baseline.items.filter((id) => id !== candidate.replaces)
      reachable.add(key([...kept, candidate.id]))
    }

    rows.push({
      build: spec,
      items: best.items,
      gain: best.gain,
      gainError: best.gainError,
      // `?? ` for data written before the flag was published: those rows fall back
      // to the same rule rather than silently reading as "not a tie", which is the
      // direction that overstates a lead.
      isTie: best.isTie ?? Math.abs(best.gain) <= best.gainError,
      isBaseline: best.isBaseline,
      runnerUp: best.runnerUp,
      beyondOneSwap: !reachable.has(key(best.items.map((item) => item.id))),
    })
  }
  // Best gain first, and a build whose ceiling is its baseline sorts to the bottom
  // by the same rule rather than by a special case: its gain is exactly zero.
  return rows.sort((a, b) => b.gain - a.gain)
}

function CeilingTable({
  rows,
  items,
  ilevel,
}: {
  rows: CeilingRow[]
  items: Map<number, GearItemMeta>
  ilevel: number
}) {
  const link = (id: number, at: number) => (
    <GameLink kind="item" id={id} name={items.get(id)?.name ?? `Item ${id}`} ilevel={at} />
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Build</th>
            <th className="py-2 pr-4 font-medium">Best set at {ilevel}</th>
            <th className="py-2 pr-4 text-right font-medium">Over baseline</th>
            <th className="py-2 pr-4 font-medium">Beyond one swap</th>
            <th className="py-2 pr-5 font-medium">Next best set</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            // Published by the pipeline, not recomputed here. The tie rule is the
            // project's uncertainty convention and it lives in one place; the sibling
            // `runnerUp.tie` one column over already came from Python, so recomputing
            // this one left the same rule in two implementations with nothing to
            // catch them diverging.
            const tie = row.isTie
            return (
              <tr key={row.build.id} className="border-b border-hairline/60 last:border-0">
                <td className="py-2 pr-4 pl-5">
                  <BuildIdentity build={row.build} />
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {row.items.map((item, index) => (
                    <span key={item.id}>
                      {index ? ' + ' : null}
                      {link(item.id, item.ilevel)}
                    </span>
                  ))}
                </td>
                <td
                  className={`py-2 pr-4 text-right tabular-nums ${
                    row.isBaseline || tie ? 'text-ink-muted' : 'text-ink'
                  }`}
                >
                  {row.isBaseline ? 'baseline' : tie ? 'tie' : signedPercent(row.gain)}
                </td>
                <td className="py-2 pr-4 text-ink-muted">{row.beyondOneSwap ? 'yes' : '—'}</td>
                <td className="py-2 pr-5 text-ink-muted">
                  {row.runnerUp ? (
                    <>
                      {row.runnerUp.items.map((item, index) => (
                        <span key={item.id}>
                          {index ? ' + ' : null}
                          {link(item.id, item.ilevel)}
                        </span>
                      ))}{' '}
                      ({row.runnerUp.tie ? 'tie' : signedPercent(-row.runnerUp.gap)})
                    </>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Which method picked the baselines on screen, said once rather than per row.
 *
 * The two produce numbers that look identical and disagree about half the time, so
 * leaving it to be inferred from the presence of a runner-up column would be exactly
 * the kind of silence this view exists to avoid. Data written before the method was
 * recorded says so instead of guessing on its behalf.
 */
function BaselineMethodNote({
  specs,
  targetCount,
}: {
  specs: GearSpecResult[]
  targetCount: number
}) {
  // Counts per method, kept as a range rather than a maximum. `usable_by` admits a
  // different number of items to an intellect spec than to a strength one, so one
  // number printed as "per build" can be true of no build on the page. And membership
  // is tested with `has`, not truthiness: a merged dataset keeps older specs verbatim,
  // so an exhaustive spec can arrive with no `combinations` field, and `0` then read
  // as "no exhaustive builds" and left the sentence ending in a bare full stop.
  const seen = new Map<string, { low: number; high: number }>()
  for (const spec of specs) {
    const target = spec.targets.find((entry) => entry.targets === targetCount)
    if (!target) continue
    const method = target.baseline.method ?? 'unrecorded'
    const count = target.baseline.combinations
    const range = seen.get(method) ?? { low: Infinity, high: 0 }
    if (count !== undefined) {
      range.low = Math.min(range.low, count)
      range.high = Math.max(range.high, count)
    }
    seen.set(method, range)
  }
  if (seen.size === 0) return null

  const parts: string[] = []
  const exhaustive = seen.get('exhaustive')
  if (exhaustive) {
    const counted =
      exhaustive.high === 0
        ? ''
        : exhaustive.low === exhaustive.high
          ? ` — ${exhaustive.high} combinations per build —`
          : ` — ${exhaustive.low} to ${exhaustive.high} combinations per build, depending on how many items the spec can use —`
    parts.push(`filling every socket every possible way${counted} and running each`)
  }
  if (seen.has('additive')) {
    parts.push(
      'ranking items by what each adds on its own, because this pool has too many combinations to enumerate inside the sweep’s budget',
    )
  }
  if (seen.has('unrecorded')) {
    parts.push('a method this dataset was written too early to record')
  }
  return (
    <Note>
      Baselines here were chosen by {parts.join('; and by ')}.{' '}
      {seen.size > 1
        ? 'More than one method appears above, which happens when slots or specs were swept at different times — the two are not interchangeable, so read the mixed rows with that in mind.'
        : null}
    </Note>
  )
}

function BaselineTable({
  specs,
  targetCount,
  items,
}: {
  specs: GearSpecResult[]
  targetCount: number
  items: Map<number, GearItemMeta>
}) {
  // The baseline is worn at the lower of the two item levels, and every pool
  // entry carries the level it was measured at, so each link asks Wowhead about
  // the item at the level this row's numbers came from.
  //
  // Two "next best" columns because they answer two questions and the older one
  // stopped deciding anything. "Next best set" is the runner-up combination, which
  // is what the choice was actually made between; "next best alone" is the per-item
  // standalone value, which is legible and no longer the method.
  const link = (id: number, ilevel: number) => (
    <GameLink kind="item" id={id} name={items.get(id)?.name ?? `Item ${id}`} ilevel={ilevel} />
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Build</th>
            <th className="py-2 pr-4 font-medium">Kept</th>
            <th className="py-2 pr-4 font-medium">Replaced by candidates</th>
            <th className="py-2 pr-4 text-right font-medium">Baseline DPS</th>
            <th className="py-2 pr-4 font-medium">Next best set</th>
            <th className="py-2 pr-5 font-medium">Next best alone</th>
          </tr>
        </thead>
        <tbody>
          {specs.map((spec) => {
            const target = spec.targets.find((entry) => entry.targets === targetCount)
            if (!target) return null
            // Which item the candidates displace is published, in two places for two
            // reasons: `baseline.replaces` says it once for the row, and every
            // candidate repeats it as a record of what that variant equipped. Read,
            // never re-derived — "the last item" is pool-file order rather than the
            // weakest, and reading it that way is what the pipeline used to do.
            // Older data carries neither, and falls back to the shape it was written
            // under, which for those runs is what actually ran.
            const replaced =
              target.baseline.replaces ??
              target.candidates[0]?.replaces ??
              target.baseline.items.at(-1)
            const kept = target.baseline.items.filter((id) => id !== replaced)
            // Sorted here rather than taken off the head of `pool`, which arrives in
            // whichever order chose the baseline — by best pair under the exhaustive
            // rule. This column says "alone" and its Note defines the word, so it has
            // to rank by that figure or it prints standalone numbers beside items
            // picked by a different one. On MID2's finger sweep those two orders
            // disagree on four builds of six.
            const runnersUp = target.pool
              .filter((entry) => !entry.chosen)
              .slice()
              .sort((a, b) => b.standaloneGain - a.standaloneGain)
              .slice(0, 2)
            const runnerSet = target.baseline.runnerUp
            return (
              <tr key={spec.id} className="border-b border-hairline/60 last:border-0">
                <td className="py-2 pr-4 pl-5">
                  <BuildIdentity build={spec} />
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {kept.length
                    ? kept.map((id, index) => (
                        <span key={id}>
                          {index ? ' + ' : null}
                          {link(id, target.baseline.ilevel)}
                        </span>
                      ))
                    : '—'}
                </td>
                <td className="py-2 pr-4 text-ink-secondary">
                  {replaced ? link(replaced, target.baseline.ilevel) : '—'}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums text-ink">
                  {fullNumber(target.baseline.dps)}
                </td>
                <td className="py-2 pr-4 text-ink-muted">
                  {runnerSet ? (
                    <>
                      {runnerSet.items.map((item, index) => (
                        <span key={item.id}>
                          {index ? ' + ' : null}
                          {link(item.id, item.ilevel)}
                        </span>
                      ))}{' '}
                      ({runnerSet.tie ? 'tie' : signedPercent(-runnerSet.gap)})
                    </>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="py-2 pr-5 text-ink-muted">
                  {runnersUp.length
                    ? runnersUp.map((entry, index) => (
                        <span key={entry.id}>
                          {index ? ', ' : null}
                          {link(entry.id, entry.ilevel)} ({fullNumber(entry.standaloneGain)} alone)
                        </span>
                      ))
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

/** Spec ids the gear dataset actually covers, in dataset order.
 *
 * The union across slots, not the first slot's: slots are swept independently, so a
 * spec can be covered for trinkets and not yet for rings, and reading only the first
 * would under-report the moment a second slot exists.
 */
export function gearSpecIds(gear: GearDataset | null): string[] {
  const seen = new Set<string>()
  const ids: string[] = []
  for (const slot of gear?.slots ?? []) {
    for (const spec of slot.specs) {
      if (seen.has(spec.id)) continue
      seen.add(spec.id)
      ids.push(spec.id)
    }
  }
  return ids
}
