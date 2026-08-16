/**
 * Which specs this tier's data covers, and which are simply not there yet.
 *
 * The question a reader arrives with the week a season opens: *is my spec missing,
 * or is it just bad?* A ranking can only draw what it has, so those two look
 * identical on it — and the second is a conclusion somebody might act on.
 *
 * simc ships its tier profiles as they are written, so early in a season the set is
 * incomplete: Midnight Season 2 has 15 of the 26 damage specs, where Season 1 ended
 * with all 26. Six whole classes are absent. Saying so is not a caveat about this
 * site's data, it is the most useful thing on the page for anyone playing one of
 * those eleven specs.
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

export function SpecCoverage({ manifest }: { manifest: Manifest }) {
  const coverage = manifest.coverage
  // Datasets built before this existed carry no coverage block. Saying nothing is
  // right there: an empty panel would imply full coverage, which is the claim this
  // component exists to stop being made silently.
  if (!coverage || !coverage.damageSpecsKnown) return null

  const { damageSpecs, damageSpecsKnown, missing } = coverage
  const complete = missing.length === 0

  const byClass = new Map<string, string[]>()
  for (const entry of missing) {
    const specs = byClass.get(entry.class) ?? []
    specs.push(entry.spec)
    byClass.set(entry.class, specs)
  }

  return (
    <Panel>
      <PanelHeader
        title="Which specs this covers"
        subtitle={
          complete
            ? 'SimulationCraft ships a profile for every damage spec in this tier.'
            : 'SimulationCraft ships its tier profiles as they are written, so early in a season the set is incomplete. A spec with no profile cannot appear in any ranking here — which looks exactly like a spec that ranks badly.'
        }
      />

      <div className="px-5 pb-4">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[22px] font-semibold tabular-nums text-ink-primary">
            {damageSpecs} of {damageSpecsKnown}
          </span>
          <span className="text-[13px] text-ink-tertiary">
            damage specs have a profile for this tier
          </span>
        </div>

        {/* The bar is redundant with the number beside it, deliberately: it is the
            only thing on the Overview that shows a *fraction* rather than a ranking,
            and the shape is what makes "most of a season is missing" land. */}
        <div
          className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-elevated"
          role="img"
          aria-label={`${damageSpecs} of ${damageSpecsKnown} damage specs covered`}
        >
          <div
            className="h-full rounded-full bg-[var(--series-1)]"
            style={{ width: `${(damageSpecs / damageSpecsKnown) * 100}%` }}
          />
        </div>
      </div>

      {complete ? null : (
        <div className="px-5 pb-4">
          <div className="mb-2 text-[11.5px] uppercase tracking-wide text-ink-tertiary">
            No profile yet ({missing.length})
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
      )}

      <Note>
        {complete ? (
          <>Every damage spec SimulationCraft has ever shipped a tier profile for is here.</>
        ) : (
          <>
            These are absent from SimulationCraft&rsquo;s profiles for this tier, not from this
            site&rsquo;s run — nothing here failed. They appear as soon as simc publishes them
            and the next nightly picks them up. The list of what <em>should</em> exist is
            derived from the tiers simc has shipped
            {coverage.comparedWith.length ? ` (${coverage.comparedWith.join(', ')})` : ''}, so a
            spec Blizzard adds mid-expansion counts the moment it has been profiled once.
          </>
        )}
      </Note>
    </Panel>
  )
}
