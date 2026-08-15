/**
 * Spec detail: what a single build's damage is actually made of.
 *
 * The ability breakdown is where the funnel number stops being abstract -- a
 * build that funnels does it through identifiable abilities, and the shift in
 * this list between one target and ten is the mechanism.
 *
 * This is the one view that is inherently about one build at a time, so it opens
 * on one rather than asking for it: the app hands it the top build of the current
 * scenario, or whatever was clicked in the ranking. The strip along the top is
 * navigation between builds, not a gate in front of them -- there is always
 * something on screen behind it.
 */

import { useMemo, useState } from 'react'
import {
  HeroTreeBadge,
  SpecIcon,
  buildName,
  fullBuildName,
  type BuildLike,
} from '../components/BuildIdentity'
import { TalentList, TalentTree } from '../components/TalentTree'
import { EmptyState, Note, Panel, PanelHeader, Select, StatTile, cx } from '../components/ui'
import { describeBurst, describeFunnelGain, fullNumber, percent } from '../lib/format'
import { classColor, classWash, sequentialStep } from '../lib/palette'
import type { ScenarioMeta, SpecDetail, TalentTreeDataset } from '../lib/types'

export function SpecDetailView({
  detail,
  scenario,
  allSpecs,
  onSelectSpec,
  talentTrees,
}: {
  detail: SpecDetail | null
  scenario: ScenarioMeta
  allSpecs: BuildLike[]
  onSelectSpec: (id: string) => void
  /** Null until the tier's decoded trees have loaded, or when it carries none. */
  talentTrees: TalentTreeDataset | null
}) {
  const cells = detail?.scenarios[scenario.id]?.targets ?? []
  const [targets, setTargets] = useState(1)
  const available = cells.map((cell) => cell.targets)
  const effective = available.includes(targets) ? targets : (available[0] ?? 1)
  const cell = cells.find((entry) => entry.targets === effective)

  return (
    <div className="space-y-4">
      <BuildStrip builds={allSpecs} current={detail?.id ?? null} onSelect={onSelectSpec} />

      {!detail ? (
        <Panel>
          <PanelHeader title="Spec detail" />
          <EmptyState>
            No build is loaded. Pick one from the strip above, or open one from the ranking.
          </EmptyState>
        </Panel>
      ) : (
        <>
          <Panel>
            <PanelHeader
              title={
                <span className="inline-flex items-center gap-2.5">
                  <SpecIcon build={detail} size={24} labelled />
                  <span style={{ color: classColor(detail.class) }}>{buildName(detail)}</span>
                  <HeroTreeBadge build={detail} size={16} />
                </span>
              }
              subtitle={`${scenario.label} · ${effective} ${effective === 1 ? 'target' : 'targets'}`}
              actions={
                available.length > 1 ? (
                  <Select
                    label="Targets"
                    value={effective}
                    onChange={setTargets}
                    options={available.map((count) => ({ value: count, label: String(count) }))}
                  />
                ) : null
              }
            />

            {cell ? (
              <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 lg:grid-cols-4">
                <StatTile
                  label="Damage per second"
                  value={fullNumber(cell.dps)}
                  caption={`±${cell.dpsError.toFixed(2)}% sampling error over ${fullNumber(cell.iterations)} pulls`}
                  accent={classColor(detail.class)}
                />
                <StatTile
                  label="Funnel gain"
                  value={cell.funnelGain !== undefined ? `${cell.funnelGain.toFixed(2)}x` : '—'}
                  caption={
                    cell.funnelGain !== undefined
                      ? `${describeFunnelGain(cell.funnelGain)}. Main target takes ${percent(cell.priorityShare ?? 0)} of total damage, ${(cell.concentration ?? 0).toFixed(2)}x an even spread.`
                      : 'Only one target, so there is nothing to funnel from.'
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

          <TalentsPanel detail={detail} trees={talentTrees} />

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
        </>
      )}
    </div>
  )
}

/**
 * Every build in the tier as a row of chips, the current one filled.
 *
 * Icon plus written name, not icon alone: forty spec icons are not universally
 * recognisable and a hero-tree emblem certainly is not, so each chip says what it
 * is in words as well.
 */
function BuildStrip({
  builds,
  current,
  onSelect,
}: {
  builds: BuildLike[]
  current: string | null
  onSelect: (id: string) => void
}) {
  const grouped = useMemo(() => {
    const byClass = new Map<string, BuildLike[]>()
    for (const build of builds) {
      const bucket = byClass.get(build.class)
      if (bucket) bucket.push(build)
      else byClass.set(build.class, [build])
    }
    return [...byClass.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [builds])

  if (builds.length === 0) return null

  return (
    <Panel>
      <div className="flex flex-wrap gap-x-5 gap-y-3 px-5 py-4">
        {grouped.map(([wowClass, entries]) => (
          <div key={wowClass} className="min-w-0">
            <h3
              className="text-[11px] font-semibold tracking-wide uppercase"
              style={{ color: classColor(wowClass) }}
            >
              {wowClass}
            </h3>
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {entries.map((build) => {
                const active = build.id === current
                return (
                  <li key={build.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(build.id)}
                      aria-current={active ? 'true' : undefined}
                      title={fullBuildName(build)}
                      className={cx(
                        'flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[12px] transition-colors',
                        active
                          ? 'border-transparent text-ink'
                          : 'border-hairline text-ink-secondary hover:text-ink',
                      )}
                      style={
                        active
                          ? { background: classWash(build.class, 26), borderColor: classColor(build.class) }
                          : undefined
                      }
                    >
                      <SpecIcon build={build} size={15} labelled />
                      <span>{build.spec}</span>
                      <HeroTreeBadge build={build} size={12} />
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </div>
      <Note>
        Every build SimulationCraft ships for this tier. The one on screen is filled in
        its class colour; picking another changes what is below, nothing is hidden until
        you do.
      </Note>
    </Panel>
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
        Magnitude here is a one-hue ramp, not a class colour — it encodes how much, not whose.
      </Note>
    </div>
  )
}


// --------------------------------------------------------------------------------
// Talent loadout
// --------------------------------------------------------------------------------

/**
 * The build's talents, as far as a static site can carry them without a decoder.
 *
 * Every spec plays a hero tree and this build's is named and iconised here; the
 * full node-by-node tree needs the tree layout and a loadout decoder, which live in
 * the wtt backend, not in a byte-reproducible dataset. Until that is wired in, the
 * loadout string is exposed so it can be pasted into the in-game talent UI or a
 * talent calculator to see the whole tree -- the string simc simulated is the same
 * one the game imports.
 */
function TalentsPanel({ detail, trees }: { detail: SpecDetail; trees: TalentTreeDataset | null }) {
  const [copied, setCopied] = useState(false)
  const hash = detail.talentHash
  const build = trees?.builds.find((b) => b.specId === detail.id) ?? null
  const layout = build ? (trees?.trees[build.tree] ?? null) : null
  const color = classColor(detail.class)

  return (
    <Panel>
      <PanelHeader
        title="Talents"
        subtitle={
          layout
            ? 'What this build takes, decoded from the loadout string simc simulated. Taken talents are lit; the rest of the tree is shown so what the build passed over is visible too.'
            : 'The hero tree this build plays, and the loadout string simc simulated.'
        }
      />
      <div className="flex flex-wrap items-center gap-4 px-5 pb-5">
        <span className="inline-flex items-center gap-2">
          <HeroTreeBadge build={detail} size={22} />
        </span>
        {hash ? (
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-lg border border-hairline bg-elevated px-3 py-2 font-mono text-[12px] text-ink-secondary">
              {hash}
            </code>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard?.writeText(hash).then(() => {
                  setCopied(true)
                  window.setTimeout(() => setCopied(false), 1500)
                })
              }}
              className="shrink-0 rounded-lg border border-hairline bg-surface px-3 py-2 text-[13px] font-medium text-ink-secondary hover:bg-elevated hover:text-ink"
            >
              {copied ? 'Copied' : 'Copy loadout'}
            </button>
          </div>
        ) : (
          <span className="text-[13px] text-ink-muted">No loadout string in this profile.</span>
        )}
      </div>

      {layout && build ? (
        <>
          <TalentTree layout={layout} build={build} color={color} />
          {build.caveat ? <Note>{build.caveat}</Note> : null}
          <details className="border-t border-hairline">
            <summary className="cursor-pointer px-5 py-3 text-[12.5px] text-ink-tertiary hover:text-ink-secondary">
              The same thing as a list ({build.selected.length} talents)
            </summary>
            <TalentList layout={layout} build={build} />
          </details>
          <Note>
            Decoded from simc&rsquo;s own loadout string against simc&rsquo;s own trait
            table, so it needs no external service and moves only when simc&rsquo;s data
            does. Hovering a talent asks Wowhead for its spell card. Connector lines are
            not drawn: simc ships no edge data for the tree, and a guessed edge would be a
            claim about how the tree unlocks rather than a reading of it.
          </Note>
        </>
      ) : (
        <Note>
          Paste the loadout into the in-game talent UI, or a talent calculator, to see the
          full tree. The tree is not drawn here because this tier carries no decoded trees
          yet — run <code>wowdps talent-trees</code> against a simc checkout to add them.
        </Note>
      )}
    </Panel>
  )
}