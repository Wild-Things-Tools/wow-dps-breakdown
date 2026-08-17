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
import { BuildIdentity } from '../components/BuildIdentity'
import { EmptyState, Note, Panel, PanelHeader, Select } from '../components/ui'
import { classColor } from '../lib/palette'
import { fullNumber, percent } from '../lib/format'

/** The pipeline's spec id (`mage_fire`), which the icon map is keyed on. */
function specSlug(spec: BuffSpec): string {
  return `${spec.class} ${spec.spec}`.toLowerCase().replace(/[^a-z0-9]+/g, '_')
}

type Measure = 'powerInfusion' | 'fourPiece' | 'twoPiece'

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
]

function gainOf(spec: BuffSpec, measure: Measure): { absolute: number | null; share: number | null } {
  if (measure === 'powerInfusion') {
    return { absolute: spec.powerInfusionGain, share: spec.powerInfusionPercent }
  }
  if (measure === 'fourPiece') {
    return { absolute: spec.fourPieceGain, share: spec.fourPiecePercent }
  }
  return { absolute: spec.twoPieceGain, share: spec.twoPiecePercent }
}

export function BuffsView({ data }: { data: BuffDataset | null }) {
  const [measure, setMeasure] = useState<Measure>('powerInfusion')

  const rows = useMemo(() => {
    if (!data) return []
    return data.specs
      .map((spec) => ({ spec, ...gainOf(spec, measure) }))
      .filter((row) => row.absolute !== null)
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
          subtitle={chosen.blurb}
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
