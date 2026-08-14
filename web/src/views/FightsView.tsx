/**
 * Fights: what each boss actually looks like, so the simulation can be built to match.
 *
 * Every other view here compares specs. This one is a *verification surface* for the
 * machine's reading of the encounters — the site's logs cross-check runs Patchwerk
 * single target against nine different fight shapes, and the residual is dominated by
 * which boss rather than which spec. Fixing that needs a fight shape per boss, and a
 * fight shape is only worth having if somebody who plays the fight can look at it and
 * say "no, that's wrong".
 *
 * So the design rule for this whole view is: **show both claims, resolve neither.**
 *
 * - The step chart draws what the simulation would have alive against what one real
 *   pull had alive, on one axis. A disagreement is the finding; averaging it away
 *   would destroy the only thing the page is for.
 * - The comparison table prints the asserted value, the measured value and the
 *   difference, and no verdict. CLAUDE.md's rule stands: when they differ, the
 *   extraction is the likelier culprit, and that is a sentence for a human.
 * - A boss nobody has measured or asserted anything about reads as *nothing known*,
 *   never as a default of one target. Eight of the nine are in that state and that
 *   gap is the most useful thing on the page — it says where to aim the next probe.
 *
 * Charting notes, per the dataviz method and this project's palette rules. The job is
 * "tell two or three distinct series apart over time", so: step lines (`stepAfter` —
 * a target count holds until something changes, it does not ramp), categorical colour
 * in fixed slot order for the two series that carry identity, the de-emphasis grey for
 * the other sampled pulls (context, not identity), a legend always, and a table twin
 * of the chart underneath so nothing rests on telling colours apart. One y-axis, ever:
 * both series count the same thing.
 */

import { useMemo, type ReactNode } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AXIS_LINE, AXIS_TICK, CURSOR_LINE, GRID, TooltipCard } from '../components/chart'
import {
  Dot,
  EmptyState,
  Legend,
  Note,
  Panel,
  PanelHeader,
  StatTile,
  cx,
} from '../components/ui'
import { percent } from '../lib/format'
import type {
  FightAmplification,
  FightEncounter,
  FightSpread,
  FightsDataset,
  MeasuredFight,
} from '../lib/types'

/** Colour of the line the simulation would draw. Slot 1, fixed. */
const SIM_COLOR = 'var(--series-1)'
/** Colour of the pull the logs recorded. Slot 2, fixed. */
const LOGGED_COLOR = 'var(--series-2)'
/** Measured amplification windows. Slot 4 — a named thing, so identity, not status. */
const AMPLIFY_COLOR = 'var(--series-4)'
/** Other sampled pulls are context, so they share one de-emphasis grey. */
const CONTEXT_COLOR = 'var(--text-muted)'

/** Enough other pulls to show the spread; past this the plot turns into a smear. */
const MAX_CONTEXT_PULLS = 4

export function FightsView({
  fights,
  encounterId,
  onEncounterChange,
}: {
  fights: FightsDataset | null
  encounterId: number | null
  onEncounterChange: (id: number) => void
}) {
  const encounters = fights?.encounters ?? []
  const selected =
    encounters.find((entry) => entry.encounterId === encounterId) ??
    encounters.find((entry) => entry.measured?.timeline) ??
    encounters.find((entry) => entry.hasFacts) ??
    encounters[0] ??
    null

  if (!fights) {
    return (
      <Panel>
        <PanelHeader title="Fights" subtitle={PURPOSE} />
        <EmptyState>
          No fight data has been published for this tier yet. Run{' '}
          <code>wowdps fights</code> to publish what the fight profiles assert, or{' '}
          <code>wowdps fight-probe --publish</code> in CI to measure the encounters from
          Warcraft Logs and publish both halves together.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Overview fights={fights} selected={selected} onSelect={onEncounterChange} />
      {selected ? <Encounter encounter={selected} fights={fights} /> : null}
    </div>
  )
}

const PURPOSE =
  'What each boss looks like — how many things are alive when, where the phases are, when damage is amplified — so a simulation can be built to match it instead of comparing every fight to a single stationary target.'

// --------------------------------------------------------------------------------
// The overview: every boss in the tier, and what is known about it
// --------------------------------------------------------------------------------

function Overview({
  fights,
  selected,
  onSelect,
}: {
  fights: FightsDataset
  selected: FightEncounter | null
  onSelect: (id: number) => void
}) {
  const { coverage, measurement } = fights
  const untouched = coverage.encounters - new Set(
    fights.encounters
      .filter((entry) => entry.hasFacts || (entry.measured?.fightsSampled ?? 0) > 0)
      .map((entry) => entry.encounterId),
  ).size

  return (
    <Panel>
      <PanelHeader
        title="Fight shapes in this tier"
        subtitle={
          <>
            {PURPOSE} Nothing here is simulated yet: these shapes are published so they
            can be checked before anyone pays for nine bosses × twenty-six builds.
          </>
        }
      />
      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Bosses in the tier"
          value={coverage.encounters}
          caption="Every encounter the fight profile file lists, whether or not anything is known about it."
        />
        <StatTile
          label="Anything asserted"
          value={`${coverage.asserted} of ${coverage.encounters}`}
          caption="Facts a person wrote down from playing the fight. The API cannot supply these."
        />
        <StatTile
          label="Measured from logs"
          value={`${coverage.measured} of ${coverage.encounters}`}
          caption={
            measurement
              ? `Probed on page ${measurement.rankingsPage ?? '?'} of the ${
                  measurement.metric ?? 'dps'
                } rankings, ${measurement.reportsPerEncounter ?? '?'} report(s) per boss.`
              : 'No probe run has fed this file. Every measured column is empty for that reason.'
          }
        />
        <StatTile
          label="Nothing known"
          value={untouched}
          caption="Neither measured nor asserted. Not a claim that these are single-target fights — a gap, and where to aim next."
        />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              <th className="py-2 pr-4 pl-5 font-medium">Boss</th>
              <th className="py-2 pr-4 text-right font-medium">Targets asserted</th>
              <th className="py-2 pr-4 text-right font-medium">Targets measured</th>
              <th className="py-2 pr-4 text-right font-medium">Fights sampled</th>
              <th className="py-2 pr-5 font-medium">State</th>
            </tr>
          </thead>
          <tbody>
            {fights.encounters.map((entry) => {
              const measured = entry.measured
              const asserted = entry.facts.find((fact) => fact.key === 'targets')
              const known = asserted?.source !== 'default'
              const sampled = measured?.fightsSampled ?? 0
              const active = entry.encounterId === selected?.encounterId
              return (
                <tr
                  key={entry.encounterId}
                  className={cx(
                    'border-b border-hairline/60 last:border-0',
                    active && 'bg-elevated',
                  )}
                >
                  <td className="py-2 pr-4 pl-5">
                    <button
                      type="button"
                      onClick={() => onSelect(entry.encounterId)}
                      aria-current={active ? 'true' : undefined}
                      className={cx(
                        'text-left',
                        active ? 'font-semibold text-ink' : 'text-ink hover:underline',
                      )}
                    >
                      {entry.name}
                    </button>
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums text-ink-secondary">
                    {known ? entry.profile.baselineTargets : <Muted>not asserted</Muted>}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums text-ink-secondary">
                    {measured?.peakTargets ? (
                      spreadText(measured.peakTargets)
                    ) : (
                      <Muted>not measured</Muted>
                    )}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums text-ink-secondary">
                    {measured ? sampled : <Muted>—</Muted>}
                  </td>
                  <td className="py-2 pr-5 text-ink-muted">{stateOf(entry)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <Note>
        A boss with nothing in either column has not been measured and nobody has
        written anything down about it. The scenario the site would build for it is
        one stationary target, which is a fallback, not a reading of the fight.
      </Note>
    </Panel>
  )
}

function stateOf(entry: FightEncounter): string {
  const sampled = entry.measured?.fightsSampled ?? 0
  if (entry.hasFacts && sampled > 0) return 'Asserted and measured'
  if (entry.hasFacts) return 'Asserted, never measured'
  if (sampled > 0) return 'Measured, nothing asserted'
  if (entry.measured) return 'Probed, no fights read'
  return 'Nothing measured or asserted yet'
}

// --------------------------------------------------------------------------------
// One boss
// --------------------------------------------------------------------------------

function Encounter({
  encounter,
  fights,
}: {
  encounter: FightEncounter
  fights: FightsDataset
}) {
  const measured = encounter.measured
  const sampled = measured?.fightsSampled ?? 0

  if (!encounter.hasFacts && sampled === 0) {
    return (
      <>
        <Panel>
          <PanelHeader
            title={encounter.name}
            subtitle="Nothing measured or asserted yet."
          />
          <EmptyState>
            {encounter.measured
              ? 'A probe looked at this encounter and read no fights from it. That is a different state from never having looked, and it usually means the rankings carried no report codes for the difficulty asked for.'
              : 'No probe has looked at this boss and nobody has written down what it does. Until one of those happens the site has no fight shape for it.'}{' '}
            The scenario below is the project fallback — one stationary target for 300
            seconds — and is not a reading of this fight.
          </EmptyState>
        </Panel>
        <ScenarioPanel encounter={encounter} />
      </>
    )
  }

  return (
    <>
      <TimelinePanel encounter={encounter} fights={fights} />
      <ComparisonPanel encounter={encounter} />
      {measured && sampled > 0 ? <MeasurementPanel measured={measured} /> : null}
      <ScenarioPanel encounter={encounter} />
    </>
  )
}

// --------------------------------------------------------------------------------
// The step chart — the centrepiece
// --------------------------------------------------------------------------------

interface StepSeries {
  id: string
  label: string
  color: string
  steps: Array<[number, number]>
  end: number
  width: number
  opacity: number
}

/**
 * One row per breakpoint across every series, each series forward-filled.
 *
 * Series are drawn `stepAfter`, so a value holds until the next breakpoint — which
 * is what a target count does. Past its own fight's end a series goes null rather
 * than flat, so a 288-second pull does not appear to run alongside a 400-second one.
 */
function buildStepRows(series: StepSeries[]): Array<Record<string, number | null>> {
  const breakpoints = new Set<number>()
  for (const entry of series) {
    for (const [second] of entry.steps) breakpoints.add(second)
    breakpoints.add(entry.end)
  }
  return [...breakpoints]
    .sort((a, b) => a - b)
    .map((second) => {
      const row: Record<string, number | null> = { second }
      for (const entry of series) {
        if (second > entry.end) {
          row[entry.id] = null
          continue
        }
        let value = 0
        for (const [at, count] of entry.steps) {
          if (at > second) break
          value = count
        }
        row[entry.id] = value
      }
      return row
    })
}

function TimelinePanel({
  encounter,
  fights,
}: {
  encounter: FightEncounter
  fights: FightsDataset
}) {
  const timeline = encounter.measured?.timeline ?? null
  const representative = timeline?.representative ?? null
  const targetsFact = encounter.facts.find((fact) => fact.key === 'targets')
  const simIsFallback = targetsFact?.source === 'default'

  const series = useMemo<StepSeries[]>(() => {
    const built: StepSeries[] = [
      {
        id: 'sim',
        label: simIsFallback
          ? `As simulated today (fallback: ${encounter.scenario.targets} target)`
          : `As simulated from the profile (${encounter.scenario.targets} targets)`,
        color: SIM_COLOR,
        steps: encounter.scenario.steps,
        end: encounter.scenario.maxTime,
        // Wider than the 2px the other charts use, and only here: where the
        // simulation and the pull agree the two lines land on exactly the same
        // pixels, and a 2px line underneath a 2px line reads as one series having
        // failed to draw. The extra width leaves a visible halo, so agreement
        // looks like agreement rather than like missing data.
        width: 3.5,
        opacity: 1,
      },
    ]
    if (representative) {
      built.push({
        id: 'logged',
        label: `As logged: ${representative.reportCode} fight ${representative.fightId}`,
        color: LOGGED_COLOR,
        steps: representative.steps,
        end: representative.durationSeconds,
        width: 2,
        opacity: 1,
      })
    }
    for (const other of (timeline?.others ?? []).slice(0, MAX_CONTEXT_PULLS)) {
      built.push({
        id: `other-${other.reportCode}-${other.fightId}`,
        label: `${other.reportCode} fight ${other.fightId}`,
        color: CONTEXT_COLOR,
        steps: other.steps,
        end: other.durationSeconds,
        width: 1,
        opacity: 0.55,
      })
    }
    return built
  }, [encounter, representative, timeline, simIsFallback])

  const rows = useMemo(() => buildStepRows(series), [series])
  const peak = rows.reduce((best, row) => {
    for (const [key, value] of Object.entries(row)) {
      if (key !== 'second' && typeof value === 'number') best = Math.max(best, value)
    }
    return best
  }, 0)
  const span = Math.max(...series.map((entry) => entry.end), 1)

  const contextCount = timeline?.others.length ?? 0
  const legend = [
    { id: 'sim', label: series[0]!.label, color: SIM_COLOR },
    ...(representative
      ? [{ id: 'logged', label: `As logged (${representative.reportCode})`, color: LOGGED_COLOR }]
      : []),
    ...(contextCount
      ? [
          {
            id: 'context',
            label: `${Math.min(contextCount, MAX_CONTEXT_PULLS)} other sampled pull(s)`,
            color: CONTEXT_COLOR,
          },
        ]
      : []),
    ...(representative?.auras.length
      ? [{ id: 'amp', label: 'Aura window measured on an enemy', color: AMPLIFY_COLOR }]
      : []),
  ]

  return (
    <Panel>
      <PanelHeader
        title={`${encounter.name}: how many things are alive, and when`}
        subtitle={
          representative ? (
            <>
              The logged line is <strong>one real pull</strong>, drawn whole —{' '}
              {representative.reportCode} fight {representative.fightId},{' '}
              {Math.round(representative.durationSeconds)}s,{' '}
              {representative.kill ? 'a kill' : 'a wipe'}. It is not an average of the
              sampled pulls: averaging per second across kills of different lengths
              produces a shape no pull had. The pooled claim is the spread in the table
              below.
            </>
          ) : (
            <>
              Nothing has been measured for this boss, so only the line the simulation
              would draw is shown. It comes from{' '}
              {simIsFallback ? 'the project fallback' : 'the asserted fight profile'}.
            </>
          )
        }
      />

      <div className="px-2 py-4">
        <ResponsiveContainer width="100%" height={340}>
          <LineChart data={rows} margin={{ top: 24, right: 24, bottom: 8, left: 8 }}>
            <CartesianGrid {...GRID} />
            <XAxis
              dataKey="second"
              type="number"
              domain={[0, span]}
              tick={AXIS_TICK}
              axisLine={AXIS_LINE}
              tickLine={false}
              tickFormatter={(value: number) => `${Math.round(value)}s`}
              label={{
                value: 'Seconds into the fight',
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
              width={54}
              allowDecimals={false}
              domain={[0, Math.max(peak + 1, 2)]}
              label={{
                value: 'Enemies alive',
                angle: -90,
                position: 'insideLeft',
                fill: 'var(--text-muted)',
                fontSize: 11.5,
              }}
            />

            {/* Measured aura windows on the drawn pull. What an aura DOES is not in
                the API, so this is a window and never a magnitude. */}
            {(representative?.auras ?? []).map((aura, index) => (
              <ReferenceArea
                key={`${aura.abilityId}-${index}`}
                x1={aura.start}
                x2={aura.start + aura.duration}
                fill={AMPLIFY_COLOR}
                fillOpacity={0.14}
                label={{
                  value: aura.ability,
                  position: 'insideTop',
                  fill: 'var(--text-muted)',
                  fontSize: 11,
                }}
              />
            ))}

            {/* Asserted amplification windows, in the de-emphasis grey: somebody's
                word about the fight, sitting beside what was measured. */}
            {encounter.profile.amplifications.map((amp, index) => (
              <ReferenceArea
                key={`asserted-${index}`}
                x1={amp.first}
                x2={amp.first + amp.duration}
                fill={CONTEXT_COLOR}
                fillOpacity={0.12}
                label={{
                  value: 'asserted',
                  position: 'insideBottom',
                  fill: 'var(--text-muted)',
                  fontSize: 11,
                }}
              />
            ))}

            {(representative?.phases ?? [])
              .filter((phase) => phase.start > 0)
              .map((phase) => (
                <ReferenceLine
                  key={phase.id}
                  x={phase.start}
                  stroke="var(--baseline)"
                  label={{
                    value: shortPhase(phase.name),
                    position: 'top',
                    fill: 'var(--text-muted)',
                    fontSize: 11,
                  }}
                />
              ))}

            <Tooltip
              cursor={CURSOR_LINE}
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null
                const shown = payload.filter((entry) => entry.value !== null)
                if (!shown.length) return null
                return (
                  <TooltipCard
                    title={`${Math.round(Number(label))}s into the fight`}
                    rows={shown.map((entry) => ({
                      id: String(entry.dataKey),
                      label: String(entry.name),
                      color: entry.color,
                      value: `${Number(entry.value ?? 0)} alive`,
                    }))}
                  />
                )
              }}
            />

            {/* Context pulls first so the two lines that carry identity are drawn on
                top of them: Recharts paints in element order, and a 1px grey line
                laid over the simulated line hides exactly the comparison the chart
                exists for. */}
            {drawOrder(series).map((entry) => (
              <Line
                key={entry.id}
                // A target count holds until something dies or spawns. It does not
                // ramp, so the line must not either.
                type="stepAfter"
                dataKey={entry.id}
                name={entry.label}
                stroke={entry.color}
                strokeWidth={entry.width}
                strokeOpacity={entry.opacity}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <Legend items={legend} />

      {/* The table twin of the chart, rendered even when only the simulated line
          exists: a value that is only reachable by reading a line off a plot is a
          value this project treats as not published. */}
      <StepTable rows={rows} series={series} />


      {encounter.measured?.caveats.length ? (
        <div className="px-5 pb-4">
          <ul className="space-y-1 text-[12.5px] leading-relaxed text-ink-muted">
            {encounter.measured.caveats.map((caveat) => (
              <li key={caveat} className="flex gap-2">
                <span aria-hidden>·</span>
                <span>{caveat}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <Note>
        {representative ? (
          <>
            An enemy is counted from the first damage it takes, because the API has no
            spawn event — an add that stood around before anyone hit it looks late. An
            enemy that despawns instead of dying ends at its last hit. Where the two
            lines coincide the simulation and the pull agree; the simulated line is drawn
            wider so that reads as agreement rather than as a series that failed to draw.
            Shaded bands are aura windows on an enemy; the API says when an aura was up
            and never what it was worth, so a magnitude here would be an invention. Phase
            boundaries come from this pull's own transitions.{' '}
            {fights.measurement?.samplingBias ?? ''}
          </>
        ) : (
          <>
            There is no logged line because no probe has measured this encounter, so
            nothing on this chart is evidence — it is the shape the simulation would
            run, and the shaded band, if there is one, is an asserted amplification
            window whose magnitude no log can ever confirm.
          </>
        )}
      </Note>
    </Panel>
  )
}

/** Context pulls underneath, then the simulated line, then the logged pull on top. */
function drawOrder(series: StepSeries[]): StepSeries[] {
  const rank = (entry: StepSeries) => (entry.id === 'logged' ? 2 : entry.id === 'sim' ? 1 : 0)
  return [...series].sort((a, b) => rank(a) - rank(b))
}

/** Phase names run long; the full name is in the table under the measurement panel. */
function shortPhase(name: string): string {
  return name.length > 20 ? `${name.slice(0, 19).trimEnd()}…` : name
}

function StepTable({
  rows,
  series,
}: {
  rows: Array<Record<string, number | null>>
  series: StepSeries[]
}) {
  // The two lines that carry identity. The context pulls stay out of the table:
  // they are drawn as spread, and one column each would make it unreadable.
  const columns = series.filter((entry) => entry.id === 'sim' || entry.id === 'logged')
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[420px] text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th className="py-2 pr-4 pl-5 font-medium">Second</th>
            {columns.map((column) => (
              <th key={column.id} className="py-2 pr-4 text-right font-medium">
                <span className="inline-flex items-center gap-1.5">
                  <Dot color={column.color} />
                  {column.id === 'sim' ? 'Simulated' : 'Logged'}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.second} className="border-b border-hairline/60 last:border-0">
              <td className="py-1.5 pr-4 pl-5 tabular-nums text-ink-secondary">
                {formatSecond(row.second ?? 0)}
              </td>
              {columns.map((column) => (
                <td
                  key={column.id}
                  className="py-1.5 pr-4 text-right tabular-nums text-ink"
                >
                  {row[column.id] === null ? <Muted>ended</Muted> : row[column.id]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --------------------------------------------------------------------------------
// Asserted against measured
// --------------------------------------------------------------------------------

function ComparisonPanel({ encounter }: { encounter: FightEncounter }) {
  const facts = encounter.facts.filter((fact) => fact.source !== 'default')
  const measured = encounter.measured

  return (
    <Panel>
      <PanelHeader
        title="What is asserted, and what was measured"
        subtitle={
          measured?.fightsSampled
            ? `Both claims, side by side, over ${measured.fightsSampled} sampled fight(s) from ${measured.reports.length} report(s). Nothing here is reconciled.`
            : 'Nothing has been measured for this boss, so the right-hand side is empty. The left-hand side is somebody stating what they know from playing it.'
        }
      />

      {encounter.comparison.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-[13px]">
            <thead>
              <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
                <th className="py-2 pr-4 pl-5 font-medium">Fact</th>
                <th className="py-2 pr-4 text-right font-medium">Profile says</th>
                <th className="py-2 pr-4 text-right font-medium">Logs measured</th>
                <th className="py-2 pr-4 text-right font-medium">Difference</th>
                <th className="py-2 pr-5 font-medium">Where it comes from</th>
              </tr>
            </thead>
            <tbody>
              {encounter.comparison.map((row) => (
                <tr key={row.fact} className="border-b border-hairline/60 last:border-0">
                  <td className="py-2 pr-4 pl-5 text-ink">{row.fact}</td>
                  <td className="py-2 pr-4 text-right tabular-nums text-ink">
                    {row.profile === null ? <Muted>not asserted</Muted> : String(row.profile)}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums text-ink">
                    {row.measured === null ? <Muted>not measured</Muted> : String(row.measured)}
                  </td>
                  <td
                    className={cx(
                      'py-2 pr-4 text-right tabular-nums',
                      row.delta ? 'text-ink' : 'text-ink-muted',
                    )}
                  >
                    {row.delta === null ? '—' : row.delta === 0 ? 'none' : signed(row.delta)}
                  </td>
                  <td className="py-2 pr-5 text-[12.5px] text-ink-muted">
                    {row.provenance}. {row.note}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="space-y-3 px-5 pt-4 pb-1">
        {facts.length === 0 ? (
          <p className="text-[13px] text-ink-muted">
            Nobody has written down anything about this boss. Everything above comes
            from the logs alone.
          </p>
        ) : null}
        {facts.map((fact) => (
          <div key={fact.key} className="rounded-lg border border-hairline px-4 py-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-[13px] font-medium text-ink">{fact.label}</span>
              <span className="text-[11.5px] tracking-wide text-ink-muted uppercase">
                {fact.sourceLabel}
                {fact.statedBy ? ` · ${fact.statedBy}` : ''}
                {fact.sample ? ` · ${fact.sample} fight(s)` : ''}
              </span>
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-secondary">
              {fact.detail}
            </p>
          </div>
        ))}
        {encounter.profile.amplifications.map((amp) => (
          <AmplificationNote key={`${amp.ability}-${amp.first}`} amplification={amp} />
        ))}
      </div>

      <Note>
        A disagreement between these two columns is a finding, not a merge conflict,
        and it is deliberately left unresolved. The likelier culprit is the extraction:
        the owner plays these fights, and the log reader is a pile of assumptions about
        event streams. Fix the extraction before editing the profile.
      </Note>
    </Panel>
  )
}

function AmplificationNote({ amplification }: { amplification: FightAmplification }) {
  return (
    <div className="rounded-lg border border-hairline px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium text-ink">
          Amplification: {amplification.ability}
        </span>
        <span className="text-[11.5px] tracking-wide text-ink-muted uppercase">
          magnitude asserted, never measurable
        </span>
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-secondary">
        Stated as ×{amplification.multiplier} for {amplification.duration}s from{' '}
        {amplification.first}s, landing on{' '}
        {amplification.target === 'unknown'
          ? 'a target that was not specified'
          : `the ${amplification.target} target`}
        . No field in the Warcraft Logs API says what an aura does, so the window can be
        measured and the multiplier can only ever be somebody&rsquo;s word.{' '}
        {amplification.representable
          ? 'simc can express this as a vulnerable raid event.'
          : 'simc cannot express this — see the scenario below.'}
      </p>
    </div>
  )
}

// --------------------------------------------------------------------------------
// What the logs saw
// --------------------------------------------------------------------------------

function MeasurementPanel({ measured }: { measured: MeasuredFight }) {
  const timeline = measured.timeline
  return (
    <Panel>
      <PanelHeader
        title="What the logs saw"
        subtitle={`Pooled across ${measured.fightsSampled} fight(s) in ${measured.reports.length} report(s): ${measured.reports.join(', ')}. Every number carries the range it was pooled from, because a handful of kills is a handful of guilds having different pulls.`}
      />

      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Kill time"
          value={spreadText(measured.durationSeconds, 0, 's')}
          caption={sampleCaption(measured.durationSeconds)}
        />
        <StatTile
          label="Targets, peak"
          value={spreadText(measured.peakTargets)}
          caption={`The highest concurrent count, held for ${shareText(
            measured.peakTargetShare,
          )} of the fight.`}
        />
        <StatTile
          label="Targets, mean"
          value={spreadText(measured.meanTargets, 2)}
          caption="Time-weighted. This is the number one desired_targets would have to stand for."
        />
        <StatTile
          label="Raid size"
          value={spreadText(measured.raidSize)}
          caption={`The log's own group size. ${spreadText(measured.playersListed)} player actors were listed.`}
        />
      </div>

      {measured.adds?.length ? (
        <SubTable
          title="Enemies, by share of the damage taken"
          columns={['NPC', 'Instances', 'First seen', 'Lifetime', 'Cadence', 'Share', 'At pull']}
          rows={measured.adds.map((add) => [
            add.name,
            spreadText(add.instances),
            spreadText(add.firstSeen, 1, 's'),
            spreadText(add.lifetime, 1, 's'),
            add.cadence ? spreadText(add.cadence, 1, 's') : <Muted>once</Muted>,
            add.damageShare ? percent(add.damageShare.median) : <Muted>—</Muted>,
            add.presentAtPull ? 'yes' : 'no',
          ])}
        />
      ) : null}

      {measured.phases?.length ? (
        <SubTable
          title="Phases"
          columns={['Phase', 'Starts', 'Lasts', 'Seen in']}
          rows={measured.phases.map((phase) => [
            `${phase.id}. ${phase.name}${phase.isIntermission ? ' (intermission)' : ''}`,
            spreadText(phase.start, 0, 's'),
            spreadText(phase.duration, 0, 's'),
            `${phase.seenInFights} fight(s)`,
          ])}
        />
      ) : null}

      {measured.auras?.length ? (
        <SubTable
          title="Auras on enemies"
          columns={['Ability', 'Starts', 'Lasts', 'Targets', 'Seen in']}
          rows={measured.auras.map((aura) => [
            `${aura.ability} (${aura.abilityId})`,
            spreadText(aura.start, 1, 's'),
            <>
              {spreadText(aura.duration, 1, 's')}
              {aura.anyTruncated ? <Muted> · some windows truncated</Muted> : null}
            </>,
            aura.distinctTargets,
            `${aura.seenInFights} fight(s)`,
          ])}
        />
      ) : null}

      <Note>
        These are windows, never magnitudes: the API gives an ability id, a name and a
        start and end, and nothing anywhere says what the aura does. Auras a player
        applied are dropped, because a warrior&rsquo;s debuff and a boss buffing its own
        add arrive in the same event stream — an earlier version of this nominated
        Avenging Wrath as a boss mechanic.{' '}
        {timeline ? timeline.why : ''}
      </Note>
    </Panel>
  )
}

function SubTable({
  title,
  columns,
  rows,
}: {
  title: string
  columns: string[]
  rows: Array<Array<ReactNode>>
}) {
  return (
    <div className="border-t border-hairline">
      <h3 className="px-5 pt-4 pb-2 text-[13px] font-semibold text-ink">{title}</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              {columns.map((column, index) => (
                <th
                  key={column}
                  className={cx(
                    'py-2 pr-4 font-medium',
                    index === 0 ? 'pl-5' : 'text-right',
                  )}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b border-hairline/60 last:border-0">
                {row.map((cell, index) => (
                  <td
                    key={index}
                    className={cx(
                      'py-2 pr-4',
                      index === 0
                        ? 'pl-5 text-ink'
                        : 'text-right tabular-nums text-ink-secondary',
                    )}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------------
// The simulation this produces
// --------------------------------------------------------------------------------

function ScenarioPanel({ encounter }: { encounter: FightEncounter }) {
  const { scenario } = encounter
  return (
    <Panel>
      <PanelHeader
        title="The simulation this fight shape produces"
        subtitle="What SimulationCraft would be told to run for this boss. Nothing here is wired into the nightly run yet: nine bosses across twenty-six builds is a cost decision, and these shapes are one boss deep."
      />
      <div className="px-5 pb-4">
        <pre className="overflow-x-auto rounded-lg border border-hairline bg-elevated px-4 py-3 text-[12.5px] leading-relaxed text-ink-secondary">
          {[
            `desired_targets=${scenario.targets}`,
            `max_time=${scenario.maxTime}`,
            ...scenario.options,
          ].join('\n')}
        </pre>
        <p className="mt-2 text-[12.5px] leading-relaxed text-ink-muted">
          No <code>fight_style</code> is set, deliberately: naming one makes simc clear
          the raid events the scenario is built out of, which silently turns it back
          into a plain single-target sim.
        </p>
      </div>

      {scenario.unrepresented.length ? (
        <div className="border-t border-hairline px-5 py-4">
          <h3 className="text-[13px] font-semibold text-ink">
            What this scenario does not model
          </h3>
          <ul className="mt-2 space-y-1.5 text-[12.5px] leading-relaxed text-ink-secondary">
            {scenario.unrepresented.map((entry) => (
              <li key={entry} className="flex gap-2">
                <span aria-hidden className="text-ink-muted">
                  ·
                </span>
                <span>{entry}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <Note>
        {scenario.asserted.length
          ? `Asserted rather than measured, so a reader of the resulting numbers knows which part is somebody's word: ${scenario.asserted.join(', ')}. `
          : ''}
        A scenario that silently modelled three quarters of an encounter would be worse
        than one that says which quarter is missing, so anything simc cannot express is
        listed rather than dropped.
      </Note>
    </Panel>
  )
}

// --------------------------------------------------------------------------------
// Small helpers
// --------------------------------------------------------------------------------

function Muted({ children }: { children: ReactNode }) {
  return <span className="text-ink-muted">{children}</span>
}

function formatSecond(value: number): string {
  return Number.isInteger(value) ? `${value}s` : `${value.toFixed(1)}s`
}

function signed(value: number): string {
  return `${value > 0 ? '+' : ''}${Number(value.toFixed(2))}`
}

/** A pooled figure, never as a bare median. */
function spreadText(
  spread: FightSpread | null | undefined,
  digits = 0,
  unit = '',
): string {
  if (!spread) return '—'
  const fmt = (value: number) => `${value.toFixed(digits)}${unit}`
  if (spread.low === spread.high) return fmt(spread.median)
  return `${fmt(spread.median)} (${fmt(spread.low)}–${fmt(spread.high)})`
}

/** A pooled fraction, read as a percentage. */
function shareText(spread: FightSpread | null | undefined): string {
  if (!spread) return '—'
  if (spread.low === spread.high) return percent(spread.median)
  return `${percent(spread.median)} (${percent(spread.low)}–${percent(spread.high)})`
}

function sampleCaption(spread: FightSpread | null | undefined): string {
  if (!spread) return 'Not measured.'
  return spread.low === spread.high
    ? `Every one of the ${spread.n} sampled fights agreed.`
    : `Median of ${spread.n} sampled fights, with the observed range.`
}
