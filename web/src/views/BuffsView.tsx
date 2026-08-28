/**
 * What a tier set and an outside Power Infusion are worth, per build.
 *
 * The view a raid leader arrives at with one question — *who should the Priest
 * press it on* — and one a player arrives at with another: *is my four-piece worth
 * chasing*. Both are answered by a difference, never a level, so every figure here
 * is a gain over the same build without it.
 *
 * Two columns of numbers rather than one, deliberately. A percentage alone hides
 * that 1% on a 600k build is worth more raid damage than 2% on a 300k one, and the
 * raid leader is choosing between builds, not within one.
 *
 * The four-piece figure is what it adds **over the two-piece**. Nobody chooses
 * between four pieces and none; they choose whether the third and fourth are worth
 * their slots.
 */
import { useMemo, useState } from 'react'
import type { BuffDataset, BuffSpec } from '../lib/types'
import { sweepCoverage } from '../lib/sweepCoverage'
import { BuildIdentity } from '../components/BuildIdentity'
import { EmptyState, Note, Panel, PanelHeader, Select } from '../components/ui'
import { classColor } from '../lib/palette'
import { fullNumber, percent } from '../lib/format'

/** The pipeline's spec id (`mage_fire`), which the icon map is keyed on. */
function specSlug(spec: BuffSpec): string {
  return `${spec.class} ${spec.spec}`.toLowerCase().replace(/[^a-z0-9]+/g, '_')
}

type Measure = 'powerInfusion' | 'fourPiece' | 'twoPiece' | 'crossover'

const MEASURES: { value: Measure; label: string; blurb: string }[] = [
  {
    value: 'powerInfusion',
    label: 'Power Infusion',
    blurb:
      'What one Priest pressing Power Infusion on this build is worth, with the buff landing on cooldown from the pull.',
  },
  {
    value: 'fourPiece',
    label: 'Tier set, 4-piece',
    blurb:
      'What the third and fourth set pieces add over wearing two. That is the choice being made — nobody picks between four and none.',
  },
  {
    value: 'twoPiece',
    label: 'Tier set, 2-piece',
    blurb: 'What the first two set pieces add over wearing none.',
  },
  {
    value: 'crossover',
    label: 'Season boundary',
    blurb:
      'Keeping last season’s 4-piece is the option you already have, so this is three states rather than one gain: keep it, sit in the split while the first two new pieces replace it, or change over fully.',
  },
]

function gainOf(spec: BuffSpec, measure: Measure): { absolute: number | null; share: number | null } {
  if (measure === 'crossover') {
    // Ranked by what changing over fully is worth against what the player has now.
    // The table shows all three states, so the sort key is a choice about ordering
    // rather than about which comparison matters.
    const over = spec.crossover?.currentFourOverPreviousFour ?? null
    return { absolute: null, share: over }
  }
  if (measure === 'powerInfusion') {
    return { absolute: spec.powerInfusionGain, share: spec.powerInfusionPercent }
  }
  if (measure === 'fourPiece') {
    return { absolute: spec.fourPieceGain, share: spec.fourPiecePercent }
  }
  return { absolute: spec.twoPieceGain, share: spec.twoPiecePercent }
}


/**
 * The season boundary as three columns, because it is three alternatives.
 *
 * The middle one is the state a player is actually in for a while: the first two
 * new pieces have replaced the old four-piece and the third and fourth have not
 * dropped. A negative there means changing over costs damage until the set
 * completes, which is a real and common situation and the reason this view is not
 * a single "the new set is worth X%" number.
 */
function CrossoverTable({
  rows,
}: {
  rows: { spec: BuffSpec; absolute: number | null; share: number | null }[]
}) {
  return (
    <div className="overflow-x-auto px-5 pb-5">
      <table className="w-full text-[12.5px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] uppercase tracking-wide text-ink-tertiary">
            <th className="py-2 pr-3 font-medium">Build</th>
            <th className="py-2 pr-3 text-right font-medium" title="Last season's two and four pieces">
              Old 4pc
            </th>
            <th
              className="py-2 pr-3 text-right font-medium"
              title="Last season's 2-piece plus this season's 2-piece — the state you pass through"
            >
              Split vs old 4pc
            </th>
            <th
              className="py-2 pr-3 text-right font-medium"
              title="This season's two and four pieces, against the split state"
            >
              New 4pc vs split
            </th>
            <th
              className="py-2 text-right font-medium"
              title="This season's four pieces against last season's four"
            >
              New 4pc vs old 4pc
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ spec }) => {
            const cross = spec.crossover
            if (!cross) return null
            return (
              <tr key={spec.id} className="border-b border-hairline/60">
                <td className="py-1.5 pr-3">
                  <BuildIdentity build={{ ...spec, specId: specSlug(spec) }} />
                </td>
                <td className="py-1.5 pr-3 text-right tabular-nums text-ink-tertiary">
                  {fullNumber(cross.previousFourDps)}
                </td>
                <Delta value={cross.splitOverPreviousFour} />
                <Delta value={cross.currentFourOverSplit} />
                <Delta value={cross.currentFourOverPreviousFour} last />
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** One signed share, coloured by direction rather than by class. */
function Delta({ value, last }: { value: number | null; last?: boolean }) {
  if (value === null) {
    return <td className={last ? 'py-1.5 text-right' : 'py-1.5 pr-3 text-right'}>—</td>
  }
  // Gain and loss are the two validated series slots, used here because the mark
  // is a direction rather than a build -- see the palette note in CLAUDE.md.
  const colour = value >= 0 ? 'var(--series-1)' : 'var(--series-2)'
  return (
    <td
      className={last ? 'py-1.5 text-right tabular-nums' : 'py-1.5 pr-3 text-right tabular-nums'}
      style={{ color: colour }}
    >
      {value >= 0 ? '+' : ''}
      {percent(value)}
    </td>
  )
}

export function BuffsView({
  data,
  tierBuilds,
}: {
  data: BuffDataset | null
  /**
   * Builds the tier holds right now, from the manifest.
   *
   * `buffs.json` carries no coverage block at all, so without this the view had
   * nothing to compare against and said nothing -- while 24 of MID2's 52 builds
   * were simply absent from it on 2026-08-26.
   */
  tierBuilds: number | null
}) {
  const [measure, setMeasure] = useState<Measure>('powerInfusion')

  // The sweep's own two numbers describe the sweep; the tier's size comes from the
  // manifest, which is the only thing that can say whether the sweep is behind.
  // Same call `GearView` makes.
  //
  // This passed a hard `null` until 2026-08-26, under a comment claiming
  // `buffs.json` had no coverage block. It has one, and had for as long as
  // `gear.json` has -- what was missing was the field on `BuffDataset`, so the view
  // could not read what the type hid.
  //
  // Reading it was not safe until the sweep itself was fixed, and the order matters:
  // with the old 31-of-31 data over a 52-build tier, `sweepCoverage(31, 31, 52)`
  // returns "The sweep covered every build the tier had when it ran; 21 builds have
  // been added since" -- false, because the sweep never looked at them. #88 made the
  // sweep cover the tier first; this is the second half. `sweepCoverage.test.ts`
  // pins both directions.
  const coverage = useMemo(
    () => sweepCoverage(data?.specs.length ?? 0, data?.coverage?.specsAvailable, tierBuilds),
    [data, tierBuilds],
  )
  const rows = useMemo(() => {
    if (!data) return []
    return data.specs
      .map((spec) => ({ spec, ...gainOf(spec, measure) }))
      .filter((row) => (measure === 'crossover' ? row.share !== null : row.absolute !== null))
      .sort((a, b) => (b.share ?? 0) - (a.share ?? 0))
  }, [data, measure])

  if (!data) {
    return (
      <Panel>
        <PanelHeader title="Buffs and tier sets" />
        <EmptyState>
          No buff sweep has been published for this season yet. It is a
          <code className="mx-1">wowdps buffs</code>
          run — a few profilesets per build, which is minutes rather than the hours a gear
          sweep takes.
        </EmptyState>
      </Panel>
    )
  }

  const chosen = MEASURES.find((entry) => entry.value === measure)!
  const withoutSet = data.specs.filter((spec) => !spec.setName)

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Buffs and tier sets"
          subtitle={
            <>
              {chosen.blurb} {coverage.sentence}
            </>
          }
          actions={
            <Select
              label="Compare"
              value={measure}
              onChange={(value) => setMeasure(value as Measure)}
              options={MEASURES.map((entry) => ({ value: entry.value, label: entry.label }))}
            />
          }
        />

        {rows.length === 0 ? (
          <EmptyState>Nothing was measured for this comparison.</EmptyState>
        ) : measure === 'crossover' ? (
          <CrossoverTable rows={rows} />
        ) : (
          <div className="overflow-x-auto px-5 pb-5">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="border-b border-hairline text-left text-[11.5px] uppercase tracking-wide text-ink-tertiary">
                  <th className="py-2 pr-3 font-medium">Build</th>
                  <th className="py-2 pr-3 text-right font-medium">Gain</th>
                  <th className="py-2 pr-3 text-right font-medium">Share</th>
                  <th className="py-2 pr-3 text-right font-medium">Without it</th>
                  <th className="py-2 font-medium">Set</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ spec, absolute, share }) => (
                  <tr key={spec.id} className="border-b border-hairline/60">
                    <td className="py-1.5 pr-3">
                      <BuildIdentity build={{ ...spec, specId: specSlug(spec) }} />
                    </td>
                    <td
                      className="py-1.5 pr-3 text-right tabular-nums"
                      style={{ color: classColor(spec.class) }}
                    >
                      +{fullNumber(absolute ?? 0)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-ink-secondary">
                      {share === null ? '—' : `+${percent(share)}`}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-ink-tertiary">
                      {fullNumber(spec.baseDps)}
                    </td>
                    <td className="py-1.5 text-ink-tertiary">{spec.setName ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Note>
          Every figure is a difference between two runs of the same build, one with the thing
          and one without — not a level, and not a comparison against another build's profile.
          {measure === 'powerInfusion' && data.specs[0]?.powerInfusionTimes.length ? (
            <>
              {' '}
              Power Infusion lands at{' '}
              {data.specs[0].powerInfusionTimes.map((time) => `${time}s`).join(', ')}, which is on
              cooldown from the pull. The number means nothing without that pattern: one cast at
              the pull would flatter a build whose own cooldowns line up there and nothing else.
            </>
          ) : null}
          {measure === 'fourPiece' ? (
            <> The 4-piece figure is measured over the 2-piece, not over wearing nothing.</>
          ) : null}
          {withoutSet.length ? (
            <>
              {' '}
              {withoutSet.length} build(s) have no tier set in SimulationCraft&rsquo;s data for
              this season, so only their Power Infusion value is shown.
            </>
          ) : null}
        </Note>
      </Panel>
    </div>
  )
}
