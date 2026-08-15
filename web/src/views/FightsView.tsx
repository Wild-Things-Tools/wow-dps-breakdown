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

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
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
import { EntityIcon } from '../components/BuildIdentity'
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
  cx,
} from '../components/ui'
import { percent } from '../lib/format'
import { bossIconUrl } from '../lib/gameIcons'
import type {
  ContextPull,
  FightAmplification,
  TargetBand,
  FightAuraWindow,
  FightEncounter,
  FightPattern,
  FightPromotion,
  FightSpread,
  FightsDataset,
  MeasuredFight,
  TierIndex,
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
  tierIndex,
  tier,
  onTierChange,
}: {
  fights: FightsDataset | null
  encounterId: number | null
  onEncounterChange: (id: number) => void
  tierIndex: TierIndex | null
  tier: string | null
  onTierChange: (tier: string) => void
}) {
  const encounters = fights?.encounters ?? []
  const selected =
    encounters.find((entry) => entry.encounterId === encounterId) ??
    encounters.find((entry) => entry.measured?.timeline) ??
    encounters.find((entry) => entry.hasFacts) ??
    encounters[0] ??
    null

  const season = <Season tierIndex={tierIndex} tier={tier} onTierChange={onTierChange} />

  if (!fights) {
    return (
      <Panel>
        <PanelHeader title="Fights" subtitle={PURPOSE} actions={season} />
        <EmptyState>
          No fight data has been published for this season yet. Run{' '}
          <code>wowdps fights</code> to publish what the fight profiles assert, or{' '}
          <code>wowdps fight-probe --publish</code> in CI to measure the encounters from
          Warcraft Logs and publish both halves together.
        </EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Overview
        fights={fights}
        selected={selected}
        onSelect={onEncounterChange}
        season={season}
      />
      {selected ? <Encounter encounter={selected} fights={fights} /> : null}
    </div>
  )
}

/**
 * The season control -- which is the *tier* control, deliberately not a second one.
 *
 * A season and a tier are the same axis here. `fights.json` is already namespaced
 * per tier, `tiers.json` is the only registry of which ones exist, and the header
 * already carries a tier switcher wired to one piece of app state that the URL's
 * `tier=` parameter round-trips. Giving this view its own idea of "season" would
 * create a second source of truth able to disagree with the header, with the URL,
 * and with every other view -- and a link somebody shared would then open on one
 * season's bosses under another season's heading.
 *
 * So this writes to the same state the header does. The boss list is filtered by
 * construction: it is whatever `fights.json` for that tier contains, never two
 * seasons at once.
 *
 * When only one season exists the header hides its switcher, because there is
 * nothing to switch to. This does not hide: on every other view the tier is
 * context, and here it *is* the subject -- the boss list is the season -- so it
 * is stated as a label rather than offered as an inert dropdown.
 */
function Season({
  tierIndex,
  tier,
  onTierChange,
}: {
  tierIndex: TierIndex | null
  tier: string | null
  onTierChange: (tier: string) => void
}) {
  const seasons = tierIndex?.tiers ?? []
  const current = seasons.find((entry) => entry.id === tier)
  if (!tier || seasons.length === 0) return null

  if (seasons.length === 1) {
    return (
      <span className="text-[13px] text-ink-secondary">
        Season{' '}
        <span className="font-medium text-ink">{current?.label ?? tier}</span>
        <span className="text-ink-muted"> · the only one with a dataset</span>
      </span>
    )
  }

  return (
    <Select
      label="Season"
      value={tier}
      onChange={onTierChange}
      options={[...seasons].reverse().map((entry) => ({
        value: entry.id,
        label: entry.id === tierIndex?.current ? `${entry.label} (current)` : entry.label,
      }))}
    />
  )
}

/**
 * A boss's portrait over a lettered tile, exactly as every other icon here works.
 *
 * No per-boss colour: nine invented hues would be nine categorical slots this
 * project does not have and the palette rules forbid, and the boss's name is
 * written out beside the icon in every place it appears. So the tile is the
 * de-emphasis grey and the identity is the name.
 */
function BossIcon({ encounterId, name, size = 22 }: {
  encounterId: number
  name: string
  size?: number
}) {
  return (
    <EntityIcon
      url={bossIconUrl(encounterId)}
      name={name}
      color="var(--text-muted)"
      wash="var(--elevated)"
      size={size}
      labelled
    />
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
  season,
}: {
  fights: FightsDataset
  selected: FightEncounter | null
  onSelect: (id: number) => void
  season: ReactNode
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
        title="Fight shapes this season"
        subtitle={
          <>
            {PURPOSE} Nothing here is simulated yet: these shapes are published so they
            can be checked before anyone pays for nine bosses × twenty-six builds.
          </>
        }
        actions={season}
      />
      <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Bosses this season"
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
        <table className="w-full min-w-[860px] text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
              <th className="py-2 pr-4 pl-5 font-medium">Boss</th>
              <th className="py-2 pr-4 text-right font-medium">Targets asserted</th>
              <th className="py-2 pr-4 text-right font-medium">Targets measured</th>
              <th className="py-2 pr-4 text-right font-medium">Fights sampled</th>
              <th className="py-2 pr-4 text-right font-medium">Fight read</th>
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
                        'inline-flex items-center gap-2 text-left',
                        active ? 'font-semibold text-ink' : 'text-ink hover:underline',
                      )}
                    >
                      <BossIcon encounterId={entry.encounterId} name={entry.name} size={20} />
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
                  <td className="py-2 pr-4 text-right tabular-nums text-ink-secondary">
                    {measured ? <Coverage measured={measured} /> : <Muted>—</Muted>}
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
        one stationary target, which is a fallback, not a reading of the fight.{' '}
        <strong className="font-medium text-ink-secondary">Fight read</strong> is how
        much of each sampled pull the event fetch actually reached: enemy damage-taken
        is paginated and a twenty-player Mythic pull outruns the budget, so a low
        figure means the counts beside it describe the opening minutes and not the
        encounter.
      </Note>
    </Panel>
  )
}

/**
 * How much of each sampled pull the event fetch actually reached.
 *
 * Read from `measured.eventCoverage` where the pipeline published it. Where it
 * did not -- every dataset written before that field existed, including the one
 * on the site today -- it is derived from the per-pull step functions that *are*
 * published: the last step is where the events stopped, and over the fight length
 * that is the coverage. Deriving it here rather than waiting for the next probe
 * run is deliberate: the counts in the file right now were averaged over whole
 * fights that were only partly read, and a reader has no other way to know.
 *
 * Returns null only when there is nothing at all to compute from.
 */
function coverageOf(measured: MeasuredFight): { low: number; high: number; median: number } | null {
  const published = measured.eventCoverage
  if (published) return published

  const timeline = measured.timeline
  if (!timeline) return null
  const pulls = [
    { steps: timeline.representative.steps, duration: timeline.representative.durationSeconds },
    ...timeline.others.map((other) => ({ steps: other.steps, duration: other.durationSeconds })),
  ]
  const values = pulls
    .map(({ steps, duration }) => {
      const last = steps.length ? steps[steps.length - 1]![0] : 0
      return duration > 0 ? Math.min(last / duration, 1) : null
    })
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b)
  if (!values.length) return null
  return {
    low: values[0]!,
    high: values[values.length - 1]!,
    median: values[Math.floor(values.length / 2)]!,
  }
}

/** Below this a count averaged over a fight is a count averaged over its opening. */
const COMPLETE_COVERAGE = 0.95

function Coverage({ measured }: { measured: MeasuredFight }) {
  const coverage = coverageOf(measured)
  if (!coverage) return <Muted>—</Muted>
  const partial = coverage.low < COMPLETE_COVERAGE
  const text =
    coverage.low === coverage.high
      ? percent(coverage.median)
      : `${percent(coverage.low)}–${percent(coverage.high)}`
  return (
    <span
      className={partial ? 'text-ink' : 'text-ink-secondary'}
      title={
        partial
          ? 'The event fetch stopped before the end of these pulls. Counts taken over them describe the part that was read.'
          : 'The event fetch reached the end of these pulls.'
      }
    >
      {text}
      {partial ? <span aria-hidden> ⚠</span> : null}
      <span className="sr-only">
        {partial ? ' — partly read, counts describe a prefix of the fight' : ''}
      </span>
    </span>
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
            title={
              <span className="inline-flex items-center gap-2">
                <BossIcon encounterId={encounter.encounterId} name={encounter.name} />
                {encounter.name}
              </span>
            }
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
      <TargetBandPanel encounter={encounter} />
      <TimelinePanel encounter={encounter} fights={fights} />
      <ComparisonPanel encounter={encounter} />
      {measured && sampled > 0 ? <MeasurementPanel measured={measured} /> : null}
      {measured && sampled > 0 ? <PromotionPanel encounter={encounter} /> : null}
      <ScenarioPanel encounter={encounter} />
    </>
  )
}

// --------------------------------------------------------------------------------
// What the logs could contribute to the profile, and what stops them
// --------------------------------------------------------------------------------

/**
 * The promotion proposals, shown before anything is written anywhere.
 *
 * The owner does not want to type nine bosses' target counts in by hand, and the
 * probe already measures them. What he also does not want -- and what this panel
 * exists to prevent -- is a pipeline step quietly replacing something he stated
 * with something the log reader computed. A disagreement between the two is the
 * most valuable output this whole subsystem has, and an automatic promotion would
 * consume it silently on the way past.
 *
 * So the proposal is published and the writing is a command somebody runs.
 */
function PromotionPanel({ encounter }: { encounter: FightEncounter }) {
  const promotions = encounter.promotions
  if (!promotions) {
    return (
      <Panel>
        <PanelHeader
          title="What the logs could fill in"
          subtitle="This dataset was published before the promotion machinery existed, so there is nothing to show. The next probe run will carry it."
        />
      </Panel>
    )
  }
  if (!promotions.length) {
    return (
      <Panel>
        <PanelHeader
          title="What the logs could fill in"
          subtitle="Nothing the measurement could contribute to this profile."
        />
      </Panel>
    )
  }

  const ready = promotions.filter((entry) => entry.eligible)
  return (
    <Panel>
      <PanelHeader
        title="What the logs could fill in"
        subtitle={
          <>
            Facts the measurement could become, so nobody has to copy a number across
            by hand. {ready.length} of {promotions.length} are ready to write. Nothing
            here has been applied: a measurement reaches a profile only through the
            command below, and it never overwrites a fact a person asserted.
          </>
        }
      />
      <div className="space-y-3 px-5 pt-4 pb-1">
        {promotions.map((promotion) => (
          <PromotionRow key={promotion.key} promotion={promotion} />
        ))}
      </div>
      {encounter.promoteCommand ? (
        <div className="px-5 pt-3 pb-4">
          <pre className="overflow-x-auto rounded-lg border border-hairline bg-elevated px-4 py-3 text-[12.5px] leading-relaxed text-ink-secondary">
            {encounter.promoteCommand}
          </pre>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-muted">
            Without <code>--write</code> it prints the same list and changes nothing.
          </p>
        </div>
      ) : null}
      <Note>
        A held-back fact is not a failure. &ldquo;A person already said so&rdquo; is the
        permanent answer and stays that way; &ldquo;the event fetch only read the first
        minute&rdquo; is a reason to re-probe with a larger page budget. Both are
        printed rather than resolved, for the same reason the comparison above is.
      </Note>
    </Panel>
  )
}

function PromotionRow({ promotion }: { promotion: FightPromotion }) {
  return (
    <div className="rounded-lg border border-hairline px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium text-ink">
          {promotion.label}: {promotion.summary}
        </span>
        <span
          className={cx(
            'rounded-full border px-2 py-px text-[11px] tracking-wide uppercase',
            promotion.eligible
              ? 'border-hairline text-ink-secondary'
              : 'border-hairline text-ink-muted',
          )}
        >
          {promotion.eligible ? 'ready to write' : 'held back'}
        </span>
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-secondary">
        {promotion.reason}
      </p>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-muted">
        Measured from {promotion.sample} fight(s)
        {promotion.reports.length ? ` (${promotion.reports.join(', ')})` : ''}:{' '}
        {promotion.evidence}
      </p>
      {promotion.disagrees ? (
        <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink">
          The profile currently says <code>{JSON.stringify(promotion.current)}</code>.
          This project&rsquo;s rule is that the extraction is the likelier culprit.
        </p>
      ) : null}
    </div>
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

/**
 * How many pulls the probe sampled, from the patterns themselves.
 *
 * Every pull is in exactly one pattern's `pulls` or in its `unmatched`, so either
 * side of the first entry adds up to the sample. Taken from the first pattern rather
 * than summed, because summing `pulls` across patterns silently drops the pulls that
 * matched nothing.
 */
/**
 * What the clustering found, said in words rather than left to the control.
 *
 * Three sentences at most, and none of them a hedge: how many shapes there were, how
 * tightly this one holds together, and how thin the sample is. The last is the one
 * that matters most today -- three pulls per boss cannot establish a pattern, and a
 * chooser with two options on it looks far more confident than the evidence is.
 */
function PatternNote({
  patterns,
  pattern,
}: {
  patterns: FightPattern[]
  pattern: FightPattern
}) {
  const total = sampledPulls(patterns)
  return (
    <Note>
      {patterns.length > 1 ? (
        <>
          These {total} pulls came out as <strong>{patterns.length} different shapes</strong>,
          grouped by their target-count curve over normalised fight time. This one is what{' '}
          {pattern.pulls} of them looked like; the others are a click away.{' '}
        </>
      ) : pattern.pulls > 1 ? (
        <>
          All {pattern.pulls} sampled pulls had the same shape, so there is nothing to
          choose between.{' '}
        </>
      ) : (
        <>
          No two of the {total} sampled pulls agreed on a shape, so this is one pull
          rather than a pattern. Treat the curve as an example, not as what the fight
          looks like.{' '}
        </>
      )}
      {pattern.pulls > 1 ? (
        <>The pulls in it disagree on at most {percent(pattern.spread, 0)} of the fight. </>
      ) : null}
      {total < 5 ? (
        <>
          {total} pulls is too few to call any of this a distribution — raise the
          probe’s <code>--reports</code> before reading a split as a real difference in
          how the fight goes, and check that both pulls were read to the end.
        </>
      ) : null}
    </Note>
  )
}

function sampledPulls(patterns: FightPattern[]): number {
  const first = patterns[0]
  return first ? first.pulls + first.unmatched.length : 0
}

/**
 * How many targets are up, and when — the aggregate across every kill.
 *
 * This is the answer to the question the whole page exists for, and it is answered
 * from the *distribution* over kills rather than from one representative pull: a
 * shaded inter-quartile band with the median drawn through it, and a fainter
 * min/max envelope behind. Where the band is tight, that many targets were reliably
 * up at that moment; where it flares, the sampled kills genuinely disagreed. The
 * simulated line rides over it so the reader sees what the sim would run against
 * what the kills actually did.
 *
 * Falls back cleanly: a dataset without a band (an older probe run, or a boss with
 * too few fully-read kills) simply does not render this panel, and the per-pull
 * timeline below still does.
 */
function TargetBandPanel({ encounter }: { encounter: FightEncounter }) {
  const band: TargetBand | null = encounter.measured?.targetBand ?? null
  const targetsFact = encounter.facts.find((fact) => fact.key === 'targets')
  const simIsFallback = targetsFact?.source === 'default'

  const rows = useMemo(() => {
    if (!band) return []
    // Resample the simulated step function onto the band's own time points, so the
    // two are drawn on one axis without a second scale.
    const sim = encounter.scenario.steps
    const simAt = (second: number) => {
      let value = 0
      for (const [t, count] of sim) {
        if (t <= second) value = count
        else break
      }
      return value
    }
    const scale = band.medianLengthSeconds / (encounter.scenario.maxTime || band.medianLengthSeconds)
    return band.band.map((point) => ({
      second: point.second,
      median: point.median,
      iqr: [point.low, point.high] as [number, number],
      envelope: [point.min, point.max] as [number, number],
      sim: simAt(point.second / (scale || 1)),
    }))
  }, [band, encounter.scenario])

  if (!band || rows.length === 0) return null

  const peak = Math.max(...band.band.map((point) => point.max), encounter.scenario.targets)

  return (
    <Panel>
      <PanelHeader
        title={
          <span className="inline-flex items-center gap-2">
            <BossIcon encounterId={encounter.encounterId} name={encounter.name} />
            {`${encounter.name}: how many targets are up, and when`}
          </span>
        }
        subtitle={
          <>
            Across <strong>{band.fights} kills</strong>, read in full. The dark band is
            where the middle half of kills sat; the faint band is the full range; the line
            is the median. Time is shown in seconds at the median kill length (
            {Math.round(band.medianLengthSeconds)}s).
          </>
        }
      />
      <div className="px-2 py-4">
        <ResponsiveContainer width="100%" height={320}>
          <ComposedChart data={rows} margin={{ top: 16, right: 24, bottom: 8, left: 8 }}>
            <CartesianGrid {...GRID} />
            <XAxis
              dataKey="second"
              type="number"
              domain={[0, Math.round(band.medianLengthSeconds)]}
              tick={AXIS_TICK}
              axisLine={AXIS_LINE}
              tickLine={false}
              tickFormatter={(value: number) => `${Math.round(value)}s`}
            />
            <YAxis
              allowDecimals={false}
              domain={[0, Math.ceil(peak)]}
              tick={AXIS_TICK}
              axisLine={AXIS_LINE}
              tickLine={false}
              width={28}
            />
            {/* Full range behind, inter-quartile in front: two stacked range areas. */}
            <Area
              dataKey="envelope"
              stroke="none"
              fill={LOGGED_COLOR}
              fillOpacity={0.12}
              isAnimationActive={false}
              activeDot={false}
            />
            <Area
              dataKey="iqr"
              stroke="none"
              fill={LOGGED_COLOR}
              fillOpacity={0.28}
              isAnimationActive={false}
              activeDot={false}
            />
            <Line
              dataKey="median"
              name="Median kill"
              type="stepAfter"
              stroke={LOGGED_COLOR}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              dataKey="sim"
              name={simIsFallback ? 'Simulated (fallback)' : 'Simulated'}
              type="stepAfter"
              stroke={SIM_COLOR}
              strokeWidth={3}
              strokeOpacity={0.9}
              dot={false}
              isAnimationActive={false}
            />
            <Tooltip
              cursor={CURSOR_LINE}
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null
                const row = payload[0]?.payload as (typeof rows)[number] | undefined
                if (!row) return null
                return (
                  <TooltipCard
                    title={`${Math.round(Number(label))}s into the fight`}
                    rows={[
                      {
                        id: 'median',
                        label: 'Median kill',
                        color: LOGGED_COLOR,
                        value: `${row.median} up`,
                      },
                      {
                        id: 'iqr',
                        label: 'Middle half',
                        color: LOGGED_COLOR,
                        value: `${row.iqr[0]}–${row.iqr[1]}`,
                      },
                      {
                        id: 'range',
                        label: 'Full range',
                        color: 'var(--text-muted)',
                        value: `${row.envelope[0]}–${row.envelope[1]}`,
                      },
                      { id: 'sim', label: 'Simulated', color: SIM_COLOR, value: `${row.sim} up` },
                    ]}
                  />
                )
              }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <Legend
        items={[
          { id: 'median', label: `Median of ${band.fights} kills`, color: LOGGED_COLOR },
          { id: 'sim', label: simIsFallback ? 'Simulated (fallback)' : 'Simulated', color: SIM_COLOR },
        ]}
      />
      <Note>{band.why}</Note>
    </Panel>
  )
}

function TimelinePanel({
  encounter,
  fights,
}: {
  encounter: FightEncounter
  fights: FightsDataset
}) {
  const timeline = encounter.measured?.timeline ?? null
  const targetsFact = encounter.facts.find((fact) => fact.key === 'targets')
  const simIsFallback = targetsFact?.source === 'default'

  // Pulls of one boss are not all the same fight, so the probe groups them by
  // shape. Ordered most-shared first, which is what this opens on. A file written
  // before the clustering has no `patterns` and falls back to its single pull.
  const patterns = timeline?.patterns ?? null
  const [patternId, setPatternId] = useState<string | null>(null)
  useEffect(() => {
    // A new boss has different patterns, and an id from the last one names nothing.
    setPatternId(null)
  }, [encounter.encounterId])

  const pattern: FightPattern | null =
    patterns?.find((entry) => entry.id === patternId) ?? patterns?.[0] ?? null
  const representative = pattern?.representative ?? timeline?.representative ?? null
  // When several shapes exist, the context curves are this pattern's own pulls: a
  // curve of a different shape drawn faintly under a labelled one is the artefact
  // the clustering exists to avoid. With one shape there is nothing else to be.
  const contextPulls: ContextPull[] = pattern
    ? pattern.alsoInThisPattern
    : (timeline?.others ?? [])

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
    for (const other of contextPulls.slice(0, MAX_CONTEXT_PULLS)) {
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
  }, [encounter, representative, contextPulls, simIsFallback])

  const rows = useMemo(() => buildStepRows(series), [series])
  const { bands, notDrawn: hiddenBands, permanent: permanentAuras } = useMemo(
    () => drawableBands(representative?.auras ?? [], representative?.durationSeconds ?? 0),
    [representative],
  )
  const peak = rows.reduce((best, row) => {
    for (const [key, value] of Object.entries(row)) {
      if (key !== 'second' && typeof value === 'number') best = Math.max(best, value)
    }
    return best
  }, 0)
  const span = Math.max(...series.map((entry) => entry.end), 1)

  const contextCount = contextPulls.length
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
    ...(bands.length
      ? [
          {
            id: 'amp',
            label: hiddenBands
              ? `Aura window measured on an enemy (${hiddenBands} further window(s) not drawn)`
              : 'Aura window measured on an enemy',
            color: AMPLIFY_COLOR,
          },
        ]
      : []),
  ]

  // Named rather than shaded: see PERMANENT_AURA_SHARE. Pooled by ability so one
  // buff on three enemies is one line, and the enemy is what the reader wants.
  const permanentNames = [
    ...new Map(
      permanentAuras.map((aura) => [
        aura.abilityId,
        aura.actorName ? `${aura.ability} on ${aura.actorName}` : aura.ability,
      ]),
    ).values(),
  ]

  return (
    <Panel>
      <PanelHeader
        title={
          <span className="inline-flex items-center gap-2">
            <BossIcon encounterId={encounter.encounterId} name={encounter.name} />
            {`${encounter.name}: how many things are alive, and when`}
          </span>
        }
        actions={
          patterns && patterns.length > 1 ? (
            <SegmentedControl
              label="Kill pattern"
              value={pattern?.id ?? patterns[0]!.id}
              onChange={setPatternId}
              options={patterns.map((entry) => ({
                value: entry.id,
                label: `${entry.pulls} of ${sampledPulls(patterns)} · ${entry.label}`,
                title: `${entry.reportCodes.join(', ')} — these pulls disagreed on at most ${percent(entry.spread, 0)} of the fight`,
              }))}
            />
          ) : null
        }
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
            {bands.map((aura, index) => (
              <ReferenceArea
                key={`${aura.abilityId}-${index}`}
                x1={aura.start}
                x2={aura.start + aura.duration}
                fill={AMPLIFY_COLOR}
                fillOpacity={0.14}
                label={{
                  // Named with the enemy where the extraction resolved one: an
                  // ability floating over a three-target chart does not answer the
                  // question the band exists to raise.
                  value: aura.actorName ? `${aura.ability} on ${aura.actorName}` : aura.ability,
                  // Anchored to the side of the band that faces into the plot. A
                  // centred label on a narrow band at t=0 runs off the left edge --
                  // "Divine Shield on General Amias Bellamy" rendered as "hield on
                  // General Amias Bellamy" until this was per-band.
                  position: aura.start < span / 2 ? 'insideTopLeft' : 'insideTopRight',
                  // One row per band. Real windows are narrow and cluster, so three
                  // labels on one line overlap into an unreadable smear -- measured
                  // on Lightblinded Vanguard, where all three land inside the first
                  // fifty seconds. A row each is the whole fix.
                  dy: index * 13,
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

      {permanentNames.length ? (
        <Note>
          Up for essentially the whole pull, so listed rather than shaded — a band marks
          a stretch that differs from the rest of the fight, and these have none:{' '}
          {permanentNames.join(', ')}.
        </Note>
      ) : null}

      {patterns && pattern ? <PatternNote patterns={patterns} pattern={pattern} /> : null}

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

/** Bands one ability may contribute before the rest are dropped. */
const MAX_BANDS_PER_ABILITY = 3

/**
 * Bands the chart will draw at all, across every ability.
 *
 * Three, not six, and the reason is that translucent bands *stack*: six at 14%
 * opacity that happen to overlap read as one 60% block, and the per-ability merge
 * cannot help because they belong to different abilities. Six was fine on the
 * three-pull dataset and turned Vaelgor & Ezzorak into a solid wash on the six-pull
 * one, which is the second time this chart has had to be defended from its own aura
 * data. The ones kept are the *shortest* windows, because a short window is the one
 * that marks something and a long one is on its way to being permanent.
 */
const MAX_BANDS = 3

/**
 * Share of the fight above which an aura is reported rather than drawn.
 *
 * Two thirds, and the choice is about what a band *means*: it marks a stretch that
 * differs from the rest of the fight, so an aura covering most of the fight has no
 * such stretch to mark. Lightblinded Vanguard's `Light Infused` runs 285s of a 285s
 * pull; shading it is shading the plot.
 */
const PERMANENT_AURA_SHARE = 0.66

/**
 * Aura windows reduced to the bands worth drawing, and a count of what was not.
 *
 * A shaded band is a heavy mark and an aura has no natural limit on how often it
 * lands: the published MID2 run carries roughly two hundred windows on the drawn
 * pull, across about twenty abilities -- most of them player debuffs, which the
 * extraction's aura filter now drops and this file predates. Drawn at 14% opacity
 * each they turn the plot into a solid block with the step lines invisible
 * underneath. So overlapping windows of one ability are merged (five copies of an
 * add carrying the same buff is one thing happening, not five bands), each
 * ability contributes at most a few, and the whole chart at most a handful.
 *
 * Everything dropped is counted and named in the legend. A cap that silently
 * showed the first six would be worse than the wash it replaced.
 *
 * The pipeline merges and caps per ability too, from the same reasoning. This is
 * repeated here because the dataset on the site right now predates that and a
 * reader cannot wait for the next probe run to see the chart; re-merging
 * already-merged windows is a no-op.
 */
function drawableBands(
  auras: FightAuraWindow[],
  fightLength: number,
): {
  bands: FightAuraWindow[]
  notDrawn: number
  permanent: FightAuraWindow[]
} {
  // An aura that is up for nearly the whole fight is not a window, and shading it
  // says nothing except that the fight happened. Six of those at 14% opacity is the
  // same solid block the per-ability cap was added to prevent, arriving by a
  // different route -- visible the moment the probe sampled six pulls instead of
  // three and more of the encounter's own long buffs survived the aura filter.
  // They are named under the chart instead, where a permanent aura belongs.
  const permanent: FightAuraWindow[] = []
  const windows: FightAuraWindow[] = []
  for (const aura of auras) {
    if (fightLength > 0 && aura.duration / fightLength >= PERMANENT_AURA_SHARE) permanent.push(aura)
    else windows.push(aura)
  }
  auras = windows

  const byAbility = new Map<number, FightAuraWindow[]>()
  for (const aura of auras) {
    const group = byAbility.get(aura.abilityId)
    if (group) group.push(aura)
    else byAbility.set(aura.abilityId, [aura])
  }

  let notDrawn = 0
  const candidates: FightAuraWindow[] = []
  for (const group of byAbility.values()) {
    const merged: FightAuraWindow[] = []
    for (const aura of [...group].sort((a, b) => a.start - b.start)) {
      const last = merged[merged.length - 1]
      if (last && aura.start <= last.start + last.duration) {
        last.duration =
          Math.max(last.start + last.duration, aura.start + aura.duration) - last.start
        continue
      }
      merged.push({ ...aura })
    }
    notDrawn += Math.max(merged.length - MAX_BANDS_PER_ABILITY, 0)
    candidates.push(...merged.slice(0, MAX_BANDS_PER_ABILITY))
  }

  // Shortest first to choose, then back into time order to draw.
  candidates.sort((a, b) => a.duration - b.duration || a.start - b.start)
  notDrawn += Math.max(candidates.length - MAX_BANDS, 0)
  const bands = candidates.slice(0, MAX_BANDS).sort((a, b) => a.start - b.start)
  return { bands, notDrawn, permanent }
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
  const measuredTarget = amplification.targetSource === 'logs'
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
          ? 'a target nobody has named yet'
          : `the ${amplification.target} target`}
        . No field in the Warcraft Logs API says what an aura does, so the window can be
        measured and the multiplier can only ever be somebody&rsquo;s word.{' '}
        {amplification.representable
          ? 'simc can express this as a vulnerable raid event.'
          : 'simc cannot express this — see the scenario below.'}
      </p>
      {/* Which of the targets carries it: a field the person who wrote this fact
          left blank, and the one part of an amplification the logs *can* answer.
          Its provenance is per field, beside the magnitude's, because the two
          halves genuinely come from different places. */}
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-muted">
        {measuredTarget ? (
          <>
            <strong className="font-medium text-ink-secondary">
              Which target: measured from the logs.
            </strong>{' '}
            {amplification.targetEvidence}
          </>
        ) : amplification.target === 'unknown' ? (
          <>
            <strong className="font-medium text-ink-secondary">
              Which target: not yet established.
            </strong>{' '}
            Aura windows are keyed on the enemy that carried them, so a probe run can
            name it — see &ldquo;Carried by&rdquo; above and the promotion below. Until
            an enemy is named <em>and</em> the fight nominates a priority target, simc
            has nothing to point a <code>vulnerable</code> raid event at: its generated
            adds have no name to pass to <code>target=</code>.
          </>
        ) : (
          <>
            <strong className="font-medium text-ink-secondary">
              Which target: asserted.
            </strong>{' '}
            Nobody has measured it against a log.
          </>
        )}
      </p>
    </div>
  )
}

// --------------------------------------------------------------------------------
// What the logs saw
// --------------------------------------------------------------------------------

function MeasurementPanel({ measured }: { measured: MeasuredFight }) {
  const timeline = measured.timeline
  const coverage = coverageOf(measured)
  const partial = !!coverage && coverage.low < COMPLETE_COVERAGE
  const coverageText = coverage
    ? coverage.low === coverage.high
      ? percent(coverage.median)
      : `${percent(coverage.low)}–${percent(coverage.high)}`
    : 'an unrecorded amount of'
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
          value={partial ? '—' : spreadText(measured.meanTargets, 2)}
          caption={
            partial
              ? `Not reported: the event fetch reached only ${coverageText} of these pulls, so a time-weighted mean over them is a mean over their opening minutes. The peak above is unaffected — a shorter read can only make it too small.`
              : 'Time-weighted, and it counts an enemy only while it is being damaged: a target the raid switches off drops out of it while still alive.'
          }
        />
        <StatTile
          label="Raid size"
          value={spreadText(measured.raidSize)}
          caption={`The log's own group size — fight metadata, so a partial event fetch does not touch it. ${spreadText(measured.playersListed)} player actors were listed.`}
        />
      </div>

      {partial ? (
        <div className="px-5 pb-4">
          <p className="rounded-lg border border-hairline bg-elevated px-4 py-3 text-[12.5px] leading-relaxed text-ink-secondary">
            <strong className="font-medium text-ink">
              The event fetch reached {coverageText} of these pulls.
            </strong>{' '}
            Enemy damage-taken is paginated and bounded, and a twenty-player Mythic
            pull generates it faster than the probe&rsquo;s page budget allows, so the
            stream stops part-way and every enemy&rsquo;s last recorded hit is the cut
            point rather than its death. Counts here are averaged over the part that
            was read. Kill time and raid size come from the fight&rsquo;s own metadata
            and are unaffected. Raising <code>--max-pages</code> on the probe is what
            fixes it, at a cost in points.
          </p>
        </div>
      ) : null}

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
          columns={['Ability', 'Starts', 'Lasts', 'Carried by', 'Seen in']}
          rows={measured.auras.map((aura) => [
            `${aura.ability} (${aura.abilityId})`,
            spreadText(aura.start, 1, 's'),
            <>
              {spreadText(aura.duration, 1, 's')}
              {aura.anyTruncated ? <Muted> · some windows truncated</Muted> : null}
            </>,
            // Which enemy, by name. This is the answer to "the amplification sits
            // on one of the three targets -- which one?", and it is the reason the
            // column that used to hold a count now holds names.
            aura.carriedBy?.length ? (
              <span title={aura.roleEvidence}>
                {aura.carriedBy.map((carrier) => carrier.name).join(', ')}
                <Muted> · {aura.role}</Muted>
              </span>
            ) : (
              <Muted>{aura.distinctTargets} target(s), not named</Muted>
            ),
            `${aura.seenInFights} fight(s)`,
          ])}
        />
      ) : null}

      <Note>
        These are windows, never magnitudes: the API gives an ability id, a name and a
        start and end, and nothing anywhere says what the aura does. Auras a player
        applied are dropped, because a warrior&rsquo;s debuff and a boss buffing its own
        add arrive in the same event stream — an earlier version of this nominated
        Avenging Wrath as a boss mechanic. &ldquo;Carried by&rdquo; names the enemy the
        aura landed on and says whether it is the priority target or an add; where the
        enemies were hit about equally nothing nominates a boss and it reads{' '}
        <em>unknown</em>, which is a fact about the encounter rather than a gap.{' '}
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
