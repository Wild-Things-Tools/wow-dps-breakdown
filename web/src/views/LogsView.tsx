/**
 * The cross-check against real raids, read for what it can actually say.
 *
 * Every row of the underlying file is one build on one boss: the median ranked
 * parse divided by the simulated single-target DPS. Every one of those numbers is
 * below 1.0, and saying so is not a finding -- a Patchwerk sim is a stationary
 * target with no mechanics, so real raids falling short of it is the definition of
 * the two things rather than a measurement of either.
 *
 * What is worth showing is what varies *between* rows, and there are three of those:
 *
 * 1. **Which boss.** Half the spread goes away once you know the encounter, and the
 *    nine bosses come out in nearly the same order for every build in the tier. That
 *    is one fight style being compared against nine fight shapes -- and it is the
 *    measurement that pays for the per-boss scenarios on the Fights view.
 * 2. **Which build, once the boss is taken out.** Dividing each row by its boss's
 *    median leaves the part that is about the build: does a real raid cost this one
 *    more or less than it costs its peers on the same fight.
 * 3. **Whether the ordering survives.** If the sim ranks builds the way the logs do,
 *    it is a usable guide to what to bring. Per boss, because pooling the bosses
 *    mixes reading 1 back in and comes out looking like noise.
 *
 * The arithmetic is all in the dataset (`wowdps logs-analyse`), not here: this file
 * draws it and writes down what it means.
 */

import { useMemo } from 'react'
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
import {
  BuildIdentity,
  EntityIcon,
  type BuildLike,
  makeBuildTick,
} from '../components/BuildIdentity'
import { EmptyState, Note, Panel, PanelHeader, StatTile } from '../components/ui'
import { bossIconUrl, iconInitials } from '../lib/gameIcons'
import { formatDate, percent } from '../lib/format'
import { classColor } from '../lib/palette'
import type { LogsAnalysis, LogsBuildReading, LogsVerification, SpecSummary } from '../lib/types'

const ROW_HEIGHT = 32
const BUILD_TICK_WIDTH = 205
const BOSS_TICK_WIDTH = 232

export function LogsView({
  logs,
  specs,
}: {
  logs: LogsVerification | null
  specs: SpecSummary[]
}) {
  const builds = useMemo(() => new Map(specs.map((spec) => [spec.id, spec])), [specs])

  if (!logs) {
    return (
      <Panel>
        <PanelHeader title="Against real raids" subtitle={PURPOSE} />
        <EmptyState>
          No cross-check has been run for this tier. `wowdps verify` needs Warcraft Logs
          credentials, so it only runs in CI.
        </EmptyState>
      </Panel>
    )
  }

  const analysis = logs.analysis ?? null
  if (!analysis) {
    return (
      <Panel>
        <PanelHeader title="Against real raids" subtitle={PURPOSE} />
        <EmptyState>
          This tier’s comparison file predates the readings. Run `wowdps logs-analyse
          --tier &lt;tier&gt;` — it needs no credentials and spends no API budget.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Headline analysis={analysis} logs={logs} />
      <BossPanel analysis={analysis} />
      <BuildPanel analysis={analysis} builds={builds} />
      <RankPanel analysis={analysis} builds={builds} />
    </div>
  )
}

const PURPOSE =
  'Simulated DPS against the median ranked parse for the same build on a real boss. The size of the gap is not the finding — a stationary target with no mechanics always wins. What the gap is made of is the finding, and it is mostly the boss.'

// --------------------------------------------------------------------------------
// Headline: what the disagreement is made of
// --------------------------------------------------------------------------------

function Headline({ analysis, logs }: { analysis: LogsAnalysis; logs: LogsVerification }) {
  const boss = analysis.varianceExplained.boss
  const build = analysis.varianceExplained.build
  const withheld = logs.withheldForSmallSample ?? 0

  return (
    <Panel>
      <PanelHeader
        title="What the disagreement is made of"
        subtitle={PURPOSE}
        actions={
          <span className="text-[12.5px] text-ink-muted">
            {logs.comparisons.length} comparisons · Mythic · {formatDate(logs.generatedAt)}
          </span>
        }
      />
      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-3">
        <StatTile
          label="Explained by the boss"
          value={boss === null ? '—' : percent(boss, 0)}
          caption="How much of the spread goes away once you know which encounter a row came from. One simulated fight style, nine real fight shapes."
        />
        <StatTile
          label="Explained by the build"
          value={build === null ? '—' : percent(build, 0)}
          caption="The same arithmetic, grouping by build instead. Smaller, and the part that is genuinely about how a spec is modelled."
        />
        <StatTile
          label="Held back as too thin"
          value={String(withheld)}
          caption={`Build-and-boss pairs with fewer than ${logs.minSampleSize ?? 5} ranked parses. A median of a handful is not a distribution, and putting one beside a simulated figure invites a comparison it cannot carry.`}
        />
      </div>
      <Note>
        Warcraft Logs ranks parses, so these medians describe the people who log this
        game well rather than a typical raid night — the level is not comparable to
        yours, and none of the readings below rests on it. What each one uses is how
        the same measurement moves from boss to boss and from build to build.
      </Note>
    </Panel>
  )
}

// --------------------------------------------------------------------------------
// Reading 1: the boss decides
// --------------------------------------------------------------------------------

interface BossRow {
  encounterId: number
  encounterName: string
  builds: number
  median: number
  min: number
  max: number
  rankAgreement: number | null
}

function BossPanel({ analysis }: { analysis: LogsAnalysis }) {
  const rows = analysis.bosses
  const lowest = rows[0]
  const highest = rows[rows.length - 1]

  return (
    <Panel>
      <PanelHeader
        title="How much of the sim a real pull keeps, by boss"
        subtitle="Median across every build with enough parses on that boss, with the range across builds behind it. Ordered by how little of the simulated number survives."
      />
      <BossChart rows={rows} />
      <BossTable rows={rows} />
      {lowest && highest ? (
        <Note>
          A build keeps about {percent(lowest.median, 0)} of its simulated damage on{' '}
          {lowest.encounterName} and about {percent(highest.median, 0)} on{' '}
          {highest.encounterName} — and the order is nearly the same for every build in
          the tier, which is what makes this the encounter’s doing rather than any
          spec’s. It is also the argument for the Fights view: a Patchwerk sim is being
          asked to stand in for nine different fights.
        </Note>
      ) : null}
    </Panel>
  )
}

function BossChart({ rows }: { rows: BossRow[] }) {
  const tick = useMemo(() => makeBossTick(rows), [rows])
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
            domain={[0, 1]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => percent(value, 0)}
          />
          <YAxis
            type="category"
            dataKey="encounterName"
            width={BOSS_TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          {/* 1.0 is where a real pull matches the simulation exactly. Nothing reaches
              it, which is the expected result rather than the interesting one. */}
          <ReferenceLine x={1} stroke="var(--baseline)" strokeDasharray="4 3" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as BossRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.encounterName}
                  rows={[
                    {
                      id: 'median',
                      label: 'Median build keeps',
                      color: 'var(--series-1)',
                      value: percent(row.median, 0),
                      hint: `Across ${row.builds} builds, from ${percent(row.min, 0)} to ${percent(row.max, 0)}.`,
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="median" barSize={14} radius={2} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell key={row.encounterId} fill="var(--series-1)" />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

/**
 * Boss portrait plus name on the category axis.
 *
 * Written out rather than reduced to the picture: nine raid bosses are not a set a
 * reader can be assumed to recognise by icon, and the icon may not arrive at all.
 */
function makeBossTick(rows: BossRow[]) {
  const byName = new Map(rows.map((row) => [row.encounterName, row]))
  // Recharts types a tick's coordinates as `string | number`, so they are narrowed
  // here rather than asserted -- same treatment as `makeBuildTick`.
  return function BossTick(props: {
    x?: string | number
    y?: string | number
    payload?: { value?: string | number }
  }) {
    const { x, y, payload } = props
    if (typeof x !== 'number' || typeof y !== 'number') return null
    const row = byName.get(String(payload?.value ?? ''))
    if (!row) return null
    const url = bossIconUrl(row.encounterId)
    return (
      <g transform={`translate(${x - BOSS_TICK_WIDTH},${y})`}>
        <rect x={0} y={-9} width={18} height={18} rx={4} fill="var(--elevated)" />
        {/* Initials under the image, as every other icon on the site does it: an
            opaque portrait covers them, a blocked CDN leaves a lettered tile. */}
        <text
          x={9}
          y={0}
          dy={3}
          textAnchor="middle"
          fill="var(--text-muted)"
          fontSize={8}
          fontWeight={600}
        >
          {iconInitials(row.encounterName)}
        </text>
        {url ? (
          <image
            href={url}
            x={0}
            y={-9}
            width={18}
            height={18}
            clipPath="inset(0 round 4px)"
            preserveAspectRatio="xMidYMid slice"
          />
        ) : null}
        <text x={24} y={0} dominantBaseline="middle" className="fill-ink" fontSize={12.5}>
          {row.encounterName}
        </text>
      </g>
    )
  }
}

function BossTable({ rows }: { rows: BossRow[] }) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[620px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">Boss</th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">Builds</th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">Median keeps</th>
            <th scope="col" className="py-2.5 pr-5 text-right font-medium">Range across builds</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.encounterId} className="border-b border-hairline/60 last:border-0">
              <td className="py-2 pr-4 pl-5">
                <span className="flex items-center gap-2">
                  <EntityIcon
                    url={bossIconUrl(row.encounterId)}
                    name={row.encounterName}
                    color="var(--text-muted)"
                    wash="var(--elevated)"
                    size={20}
                    labelled
                  />
                  <span className="text-ink">{row.encounterName}</span>
                </span>
              </td>
              <td className="tnum py-2 pr-4 text-right text-ink-secondary">{row.builds}</td>
              <td className="tnum py-2 pr-4 text-right font-medium text-ink">
                {percent(row.median, 0)}
              </td>
              <td className="tnum py-2 pr-5 text-right text-ink-secondary">
                {percent(row.min, 0)} – {percent(row.max, 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Reading 2: what is left of a build once the boss is removed
// --------------------------------------------------------------------------------

interface BuildRow extends LogsBuildReading {
  build: BuildLike | undefined
  value: number
  delta: number
}

function buildRows(analysis: LogsAnalysis, builds: Map<string, SpecSummary>): BuildRow[] {
  return analysis.perBuild
    .filter((entry): entry is LogsBuildReading & { vsField: number } => entry.vsField !== null)
    .map((entry) => ({
      ...entry,
      build: builds.get(entry.specId),
      value: entry.vsField,
      delta: entry.vsField - 1,
    }))
}

function BuildPanel({
  analysis,
  builds,
}: {
  analysis: LogsAnalysis
  builds: Map<string, SpecSummary>
}) {
  const rows = useMemo(() => buildRows(analysis, builds), [analysis, builds])
  const bias = analysis.sampleSizeBias

  if (rows.length === 0) {
    return (
      <Panel>
        <PanelHeader title="What is left once the boss is taken out" subtitle={VS_FIELD} />
        <EmptyState>
          No build was logged on at least {analysis.minBossSample} bosses, so there is
          nothing to average the encounter out of.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <Panel>
      <PanelHeader title="What is left once the boss is taken out" subtitle={VS_FIELD} />
      <BuildChart rows={rows} />
      <BuildTable rows={rows} />
      <Note>
        Read this as “the simulation flatters this build” or “the fight asks something
        of it that a stationary target never does” — the comparison cannot tell those
        two apart, and neither is an error.{' '}
        {bias === null
          ? null
          : `The obvious way it could be an artefact is parse counts: a build few people log is represented only by its very best players. Measured, that correlation is ${bias.toFixed(2)} — near enough to zero that the ordering here is not simply a popularity ranking.`}
      </Note>
    </Panel>
  )
}

const VS_FIELD =
  'Each build’s shortfall divided by the shortfall of every build on the same boss. To the right of 1.0 a real raid costs this build less than it costs its peers on the same fight; to the left, more.'

function BuildChart({ rows }: { rows: BuildRow[] }) {
  const byLabel = useMemo(() => {
    const map = new Map<string, BuildLike>()
    for (const row of rows) if (row.build) map.set(row.displayName, row.build)
    return map
  }, [rows])
  const tick = useMemo(() => makeBuildTick(byLabel, { width: BUILD_TICK_WIDTH }), [byLabel])
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
            dataKey="displayName"
            width={BUILD_TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as BuildRow | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.displayName}
                  rows={[
                    {
                      id: 'vsField',
                      label: 'Against the field',
                      color: row.build ? classColor(row.build.class) : 'var(--series-1)',
                      value: `${row.value.toFixed(2)}x`,
                      hint: describeVsField(row.value),
                    },
                    {
                      id: 'spread',
                      label: 'Across bosses',
                      color: 'var(--baseline)',
                      value:
                        row.vsFieldMin === null || row.vsFieldMax === null
                          ? '—'
                          : `${row.vsFieldMin.toFixed(2)}x – ${row.vsFieldMax.toFixed(2)}x`,
                      hint: `${row.bosses} bosses, ${row.sampleSize} ranked parses in total.`,
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="delta" barSize={14} radius={2} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell
                key={row.specId}
                fill={row.build ? classColor(row.build.class) : 'var(--series-1)'}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function describeVsField(value: number): string {
  if (value >= 1.08) return 'Loses noticeably less to a real raid than its peers on the same fights.'
  if (value >= 1.02) return 'Loses slightly less than its peers.'
  if (value > 0.98) return 'Loses about as much as everything else does.'
  if (value > 0.92) return 'Loses slightly more than its peers.'
  return 'Loses noticeably more to a real raid than its peers on the same fights.'
}

function BuildTable({ rows }: { rows: BuildRow[] }) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[720px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">Build</th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">Against the field</th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">Across bosses</th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">Bosses</th>
            <th scope="col" className="py-2.5 pr-5 font-medium">Reading</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.specId} className="border-b border-hairline/60 last:border-0">
              <td className="py-2 pr-4 pl-5">
                {row.build ? (
                  <BuildIdentity build={row.build} />
                ) : (
                  <span className="text-ink">{row.displayName}</span>
                )}
              </td>
              <td className="tnum py-2 pr-4 text-right font-medium text-ink">
                {row.value.toFixed(2)}x
              </td>
              <td className="tnum py-2 pr-4 text-right text-ink-secondary">
                {row.vsFieldMin === null || row.vsFieldMax === null
                  ? '—'
                  : `${row.vsFieldMin.toFixed(2)} – ${row.vsFieldMax.toFixed(2)}`}
              </td>
              <td className="tnum py-2 pr-4 text-right text-ink-secondary">{row.bosses}</td>
              <td className="py-2 pr-5 text-ink-secondary">{describeVsField(row.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Reading 3: does the simulated ordering survive contact
// --------------------------------------------------------------------------------

function RankPanel({
  analysis,
  builds,
}: {
  analysis: LogsAnalysis
  builds: Map<string, SpecSummary>
}) {
  const rows = analysis.bosses.filter(
    (boss): boss is BossRow & { rankAgreement: number } => boss.rankAgreement !== null,
  )
  const movers = useMemo(
    () =>
      analysis.perBuild
        .filter((entry): entry is LogsBuildReading & { rankMove: number } => entry.rankMove !== null)
        .slice()
        .sort((a, b) => a.rankMove - b.rankMove),
    [analysis],
  )
  const pooled = analysis.pooledRankAgreement

  if (rows.length === 0) {
    return null
  }

  const ordered = rows.slice().sort((a, b) => b.rankAgreement - a.rankAgreement)
  const best = ordered[0]
  const worst = ordered[ordered.length - 1]

  return (
    <Panel>
      <PanelHeader
        title="Does the simulated ordering survive contact with the boss?"
        subtitle="Rank agreement between the order the sim puts the builds in and the order the logs put them in, per boss. +1 is the same order, 0 is no relationship, −1 is backwards."
      />
      <RankChart rows={ordered} />
      <div className="grid gap-3 px-5 pb-2 sm:grid-cols-2">
        <StatTile
          label="Best agreement"
          value={best ? best.rankAgreement.toFixed(2) : '—'}
          caption={best ? best.encounterName : undefined}
        />
        <StatTile
          label="Worst agreement"
          value={worst ? worst.rankAgreement.toFixed(2) : '—'}
          caption={worst ? worst.encounterName : undefined}
        />
      </div>
      <Note>
        {pooled === null ? null : (
          <>
            Pooled over all nine bosses the figure is {pooled.toFixed(2)} — near zero,
            and that number is the trap rather than the finding: pooling mixes the boss
            effect back in, so it hides both the bosses the sim gets right and the ones
            it gets backwards.{' '}
          </>
        )}
        A single-target ranking is a guide to a single-target fight. On this evidence it
        is not a guide to who tops the meters on a specific boss, which is the question
        most people are actually asking.
      </Note>
      <MoversTable rows={movers} builds={builds} />
    </Panel>
  )
}

function RankChart({ rows }: { rows: Array<BossRow & { rankAgreement: number }> }) {
  const tick = useMemo(() => makeBossTick(rows), [rows])
  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={Math.max(200, rows.length * ROW_HEIGHT + 50)}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 40, bottom: 4, left: 8 }}
          barCategoryGap={4}
        >
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            domain={[-1, 1]}
            ticks={[-1, -0.5, 0, 0.5, 1]}
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={(value: number) => value.toFixed(1)}
          />
          <YAxis
            type="category"
            dataKey="encounterName"
            width={BOSS_TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          <ReferenceLine x={0} stroke="var(--baseline)" />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as (BossRow & { rankAgreement: number }) | undefined
              if (!row) return null
              return (
                <TooltipCard
                  title={row.encounterName}
                  rows={[
                    {
                      id: 'rho',
                      label: 'Rank agreement',
                      color: row.rankAgreement >= 0 ? 'var(--series-1)' : 'var(--series-2)',
                      value: row.rankAgreement.toFixed(2),
                      hint: describeAgreement(row.rankAgreement, row.builds),
                    },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="rankAgreement" barSize={14} radius={2} isAnimationActive={false}>
            {rows.map((row) => (
              <RBCell
                key={row.encounterId}
                fill={row.rankAgreement >= 0 ? 'var(--series-1)' : 'var(--series-2)'}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function describeAgreement(value: number, builds: number): string {
  const over = `over ${builds} builds`
  if (value >= 0.4) return `The sim broadly names the same winners here, ${over}.`
  if (value >= 0.15) return `A weak relationship ${over}.`
  if (value > -0.15) return `The simulated order says essentially nothing here, ${over}.`
  return `The simulated order is closer to backwards than right here, ${over}.`
}

function MoversTable({
  rows,
  builds,
}: {
  rows: Array<LogsBuildReading & { rankMove: number }>
  builds: Map<string, SpecSummary>
}) {
  if (rows.length === 0) return null
  return (
    <>
      <PanelHeader
        title="Which builds move, and which way"
        subtitle="Median change in position from the simulated ranking to the logged one, taken per boss. Negative means real raids place it higher than the sim does."
      />
      <div className="overflow-x-auto pb-2">
        <table className="w-full min-w-[620px] border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">Build</th>
              <th scope="col" className="py-2.5 pr-4 text-right font-medium">Rank move</th>
              <th scope="col" className="py-2.5 pr-5 font-medium">Reading</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const build = builds.get(row.specId)
              return (
                <tr key={row.specId} className="border-b border-hairline/60 last:border-0">
                  <td className="py-2 pr-4 pl-5">
                    {build ? (
                      <BuildIdentity build={build} />
                    ) : (
                      <span className="text-ink">{row.displayName}</span>
                    )}
                  </td>
                  <td className="tnum py-2 pr-4 text-right font-medium text-ink">
                    {row.rankMove > 0 ? `+${row.rankMove}` : row.rankMove}
                  </td>
                  <td className="py-2 pr-5 text-ink-secondary">
                    {row.rankMove <= -3
                      ? 'Real raids place it higher than the simulation does.'
                      : row.rankMove >= 3
                        ? 'Real raids place it lower than the simulation does.'
                        : 'Lands about where the simulation puts it.'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}
