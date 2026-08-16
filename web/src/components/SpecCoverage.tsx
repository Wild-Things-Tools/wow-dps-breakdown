/**
 * Which specs this tier's data covers, and — in two different senses — which it does not.
 *
 * The question a reader arrives with the week a season opens: *is my spec missing,
 * or is it just bad?* A ranking can only draw what it has, so those two look
 * identical on it — and the second is a conclusion somebody might act on.
 *
 * There are **three** states, not two, and collapsing the last two shipped a wrong
 * claim on the day MID1 was first published:
 *
 * - *simulated* — simc ships a profile and it produced results.
 * - *broken* — simc ships a profile and this run got nothing out of it. MID1's
 *   stored talent hashes reference nodes current spell data does not offer the
 *   spec, so 16 of its 41 profiles fail to load and ten whole specs are absent
 *   from the dataset: every Mage, every Hunter, both Warriors, Havoc,
 *   Retribution, Elemental.
 * - *missing* — simc ships no profile for it at all. That is MID2's case, where
 *   eleven specs have not been written yet.
 *
 * `spec_coverage` answers only the first and third, because it reads simc's
 * profiles directory and is called from a shard that simulated one slice. Taken
 * as the coverage of the *published dataset* it read "26 of 26 — complete" over a
 * MID1 ranking with no Mage in it. `dataset.apply_simulated_coverage` settles the
 * split where the whole run is known.
 *
 * The reference list is derived rather than written down — see `spec_coverage` in
 * `profiles.py`. A hard-coded table of the game's damage specs would need editing
 * whenever Blizzard adds one, and would go stale in exactly the patch where this
 * matters most.
 */
import type { Manifest } from '../lib/types'
import { ClassIcon } from './BuildIdentity'
import { Note, Panel, PanelHeader } from './ui'
import { classColor } from '../lib/palette'

type Entry = { class: string; spec: string }

export function SpecCoverage({ manifest }: { manifest: Manifest }) {
  const coverage = manifest.coverage
  // Datasets built before this existed carry no coverage block. Saying nothing is
  // right there: an empty panel would imply full coverage, which is the claim this
  // component exists to stop being made silently.
  if (!coverage || !coverage.damageSpecsKnown) return null

  const { damageSpecs, damageSpecsKnown, missing } = coverage
  const broken = coverage.broken ?? []
  // Undefined on a dataset built before the split existed. Falling back to the
  // shipped count is the old two-state reading, which is what that dataset means.
  const simulated = coverage.simulated ?? damageSpecs
  const complete = missing.length === 0 && broken.length === 0

  return (
    <Panel>
      <PanelHeader
        title="Which specs this covers"
        subtitle={
          complete
            ? 'SimulationCraft ships a profile for every damage spec in this tier, and every one of them ran.'
            : 'A spec absent from this dataset cannot appear in any ranking here — which looks exactly like a spec that ranks badly. There are two ways to be absent and they are not the same thing.'
        }
      />

      <div className="px-5 pb-4">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[22px] font-semibold tabular-nums text-ink-primary">
            {simulated} of {damageSpecsKnown}
          </span>
          <span className="text-[13px] text-ink-tertiary">
            damage specs are in this dataset
          </span>
        </div>

        {/* The bar is redundant with the number beside it, deliberately: it is the
            only thing on the Overview that shows a *fraction* rather than a ranking,
            and the shape is what makes "most of a season is missing" land. The two
            kinds of absence get their own segments, because the whole point of this
            panel is that they are different. */}
        <div
          className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-elevated"
          role="img"
          aria-label={`${simulated} of ${damageSpecsKnown} damage specs simulated, ${broken.length} shipped but not loading, ${missing.length} with no profile`}
        >
          <div
            className="h-full bg-[var(--series-1)]"
            style={{ width: `${(simulated / damageSpecsKnown) * 100}%` }}
          />
          <div
            className="h-full bg-[var(--series-2)]"
            style={{ width: `${(broken.length / damageSpecsKnown) * 100}%` }}
          />
        </div>
      </div>

      <SpecList
        entries={broken}
        label="Profile does not load"
        swatch="var(--series-2)"
      />
      <SpecList entries={missing} label="No profile yet" />

      <Note>
        {complete ? (
          <>Every damage spec SimulationCraft has ever shipped a tier profile for is here.</>
        ) : (
          <>
            {broken.length ? (
              <>
                SimulationCraft <em>ships</em> a profile for the first group and it no longer
                loads — the stored talent hash references nodes current spell data does not
                offer the spec. That is a property of an ageing tier, not of the spec: nothing
                can be said about how they perform until simc&rsquo;s profiles are refreshed.{' '}
              </>
            ) : null}
            {missing.length ? (
              <>
                The second group is absent from SimulationCraft&rsquo;s profiles for this tier,
                not from this site&rsquo;s run — nothing here failed. They appear as soon as
                simc publishes them and the next nightly picks them up.{' '}
              </>
            ) : null}
            The list of what <em>should</em> exist is derived from the tiers simc has shipped
            {coverage.comparedWith.length ? ` (${coverage.comparedWith.join(', ')})` : ''}, so a
            spec Blizzard adds mid-expansion counts the moment it has been profiled once.
          </>
        )}
      </Note>
    </Panel>
  )
}

/** One group of absent specs, folded by class so a whole missing class reads as one row. */
function SpecList({
  entries,
  label,
  swatch,
}: {
  entries: Entry[]
  label: string
  swatch?: string
}) {
  if (entries.length === 0) return null

  const byClass = new Map<string, string[]>()
  for (const entry of entries) {
    const specs = byClass.get(entry.class) ?? []
    specs.push(entry.spec)
    byClass.set(entry.class, specs)
  }

  return (
    <div className="px-5 pb-4">
      <div className="mb-2 flex items-center gap-2 text-[11.5px] uppercase tracking-wide text-ink-tertiary">
        {swatch ? (
          <span
            aria-hidden="true"
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: swatch }}
          />
        ) : null}
        {label} ({entries.length})
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-2">
        {[...byClass.entries()].map(([wowClass, specs]) => (
          <li key={wowClass} className="flex items-center gap-2">
            <ClassIcon wowClass={wowClass} size={18} />
            <span className="text-[12.5px]" style={{ color: classColor(wowClass) }}>
              {wowClass}
            </span>
            <span className="text-[12.5px] text-ink-tertiary">{specs.join(', ')}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
