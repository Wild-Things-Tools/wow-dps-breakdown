/**
 * Spec detail: what a single build's damage is actually made of.
 *
 * The ability breakdown is where the funnel number stops being abstract -- a
 * build that funnels does it through identifiable abilities, and the shift in
 * this list between one target and ten is the mechanism.
 */

import { useMemo, useState } from 'react'
import { EmptyState, Note, Panel, PanelHeader, Select, StatTile } from '../components/ui'
import { describeBurst, describeFunnel, fullNumber, percent } from '../lib/format'
import { sequentialStep } from '../lib/palette'
import type { ScenarioMeta, SpecDetail } from '../lib/types'

export function SpecDetailView({
  detail,
  scenario,
  allSpecs,
  onSelectSpec,
}: {
  detail: SpecDetail | null
  scenario: ScenarioMeta
  allSpecs: Array<{ id: string; displayName: string }>
  onSelectSpec: (id: string) => void
}) {
  const cells = detail?.scenarios[scenario.id]?.targets ?? []
  const [targets, setTargets] = useState(1)
  const available = cells.map((cell) => cell.targets)
  const effective = available.includes(targets) ? targets : (available[0] ?? 1)
  const cell = cells.find((entry) => entry.targets === effective)

  const picker = (
    <Select
      label="Build"
      value={detail?.id ?? ''}
      onChange={onSelectSpec}
      options={allSpecs.map((spec) => ({ value: spec.id, label: spec.displayName }))}
    />
  )

  if (!detail) {
    return (
      <Panel>
        <PanelHeader title="Spec detail" actions={picker} />
        <EmptyState>Pick a build to see what its damage is made of.</EmptyState>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title={detail.displayName}
          subtitle={`${scenario.label} · ${effective} ${effective === 1 ? 'target' : 'targets'}`}
          actions={
            <>
              {picker}
              {available.length > 1 ? (
                <Select
                  label="Targets"
                  value={effective}
                  onChange={setTargets}
                  options={available.map((count) => ({ value: count, label: String(count) }))}
                />
              ) : null}
            </>
          }
        />

        {cell ? (
          <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="Damage per second"
              value={fullNumber(cell.dps)}
              caption={`±${cell.dpsError.toFixed(2)}% sampling error over ${fullNumber(cell.iterations)} pulls`}
            />
            <StatTile
              label="On main target"
              value={cell.funnelShare !== undefined ? percent(cell.funnelShare) : '—'}
              caption={
                cell.funnelIndex !== undefined
                  ? `${cell.funnelIndex.toFixed(2)}x an even spread — ${describeFunnel(cell.funnelIndex, effective)}`
                  : 'Everything lands on the only target there is.'
              }
            />
            <StatTile
              label="Burst"
              value={cell.burstRatio !== undefined ? `${cell.burstRatio.toFixed(2)}x` : '—'}
              caption={
                cell.burstRatio !== undefined
                  ? describeBurst(cell.burstRatio)
                  : 'No timeline kept at this target count.'
              }
            />
            <StatTile
              label="Fight length"
              value={`${Math.round(cell.fightLength)}s`}
              caption="Averaged across pulls; simulated length varies by ±20%."
            />
          </div>
        ) : null}
      </Panel>

      <Panel>
        <PanelHeader
          title="What the damage is made of"
          subtitle="Share of the build's total damage, by ability. Compare across target counts to see which abilities carry the area damage and which stay on the main target."
        />
        {cell?.abilities.length ? (
          <AbilityBreakdown abilities={cell.abilities} dps={cell.dps} />
        ) : (
          <EmptyState>No ability breakdown recorded for this cell.</EmptyState>
        )}
      </Panel>

      {detail.caveats.length > 0 ? (
        <Panel>
          <PanelHeader
            title="Modelling caveats"
            subtitle="Reported by SimulationCraft itself for this spec — the places where the simulation approximates rather than reproduces the game."
          />
          <ul className="space-y-2 px-5 pb-5">
            {detail.caveats.map((caveat) => (
              <li
                key={caveat}
                className="border-l-2 border-hairline pl-3 text-[12.5px] leading-relaxed text-ink-secondary"
              >
                {caveat}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}

      {detail.errors.length > 0 ? (
        <Panel>
          <PanelHeader
            title="Failed simulations"
            subtitle="Cells missing from this build's data, and why."
          />
          <ul className="space-y-1.5 px-5 pb-5">
            {detail.errors.map((error) => (
              <li key={error} className="text-[12.5px] leading-relaxed text-ink-secondary">
                {error}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  )
}

function AbilityBreakdown({
  abilities,
  dps,
}: {
  abilities: SpecDetail['scenarios'][string]['targets'][number]['abilities']
  dps: number
}) {
  const max = useMemo(
    () => abilities.reduce((best, ability) => Math.max(best, ability.share), 0),
    [abilities],
  )

  return (
    <div className="px-5 pb-5">
      <ul className="space-y-2.5">
        {abilities.map((ability) => (
          <li key={`${ability.id ?? ability.name}`} className="grid grid-cols-[1fr_auto] gap-3">
            <div className="min-w-0">
              <div className="flex items-baseline justify-between gap-3">
                <span className="truncate text-[13px] text-ink">{ability.name}</span>
                <span className="tnum shrink-0 text-[12.5px] text-ink-secondary">
                  {percent(ability.share)}
                </span>
              </div>
              <div
                className="mt-1 h-1.5 overflow-hidden rounded-full"
                style={{ background: 'var(--elevated)' }}
              >
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${max > 0 ? (ability.share / max) * 100 : 0}%`,
                    background: sequentialStep(max > 0 ? ability.share / max : 0),
                  }}
                />
              </div>
            </div>
            <div className="tnum w-24 text-right text-[12.5px] text-ink-muted">
              {fullNumber(ability.share * dps)}
            </div>
          </li>
        ))}
      </ul>
      <Note>
        Right-hand column is that ability's contribution in damage per second. Pet and
        proc damage is attributed to the ability that produced it, so the shares add to 100%.
      </Note>
    </div>
  )
}
