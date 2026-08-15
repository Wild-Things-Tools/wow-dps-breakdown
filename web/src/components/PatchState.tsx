/**
 * What patch these numbers are, stated plainly.
 *
 * The header already carries the game build and the simc version as a byline, and
 * the footer carries a sentence. Neither answers the question a reader actually
 * arrives with the morning after a tuning pass: *is yesterday's change in this?*
 * That question has a precise answer and every part of it is already in the
 * manifest -- what was missing was somewhere that says it out loud.
 *
 * The load-bearing figure is the **data cutoff**, not the publish date. simc's
 * numbers come from a game-data snapshot with its own hotfix date, and a dataset
 * regenerated today from a snapshot taken three days ago models the game as it was
 * three days ago. Those are routinely different dates, and reporting only the second
 * would be the more flattering of the two and the wrong one.
 *
 * Everything here is read from the manifest. Nothing checks Blizzard's patch notes,
 * so this says *which build is modelled*, never "tuning change X is included" -- a
 * claim of that shape would need a source this project does not have, and being
 * confidently wrong about it is worse than being silent.
 */
import type { Manifest } from '../lib/types'
import { Panel, PanelHeader } from './ui'

function daysBetween(from: string | undefined, to: Date): number | null {
  if (!from) return null
  const start = new Date(from)
  if (Number.isNaN(start.getTime())) return null
  return Math.floor((to.getTime() - start.getTime()) / 86_400_000)
}

function age(days: number | null): string {
  if (days === null) return 'not recorded'
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

function Row({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail?: string
}) {
  return (
    <div className="flex flex-col gap-0.5 border-l-2 border-hairline pl-3">
      <dt className="text-[11.5px] uppercase tracking-wide text-ink-tertiary">{label}</dt>
      <dd className="text-[13.5px] font-medium text-ink-primary">{value}</dd>
      {detail ? <dd className="text-[12px] leading-relaxed text-ink-tertiary">{detail}</dd> : null}
    </div>
  )
}

export function PatchState({ manifest }: { manifest: Manifest }) {
  const simc = manifest.simc
  const now = new Date()
  const cutoffAge = daysBetween(simc.hotfixDate, now)
  const publishedAge = daysBetween(manifest.generatedAt, now)

  // The two dates a reader has to be able to tell apart. A dataset can be published
  // today off a snapshot taken days earlier, and the older one is what bounds what
  // the numbers can possibly know.
  const lag =
    simc.hotfixDate && manifest.generatedAt
      ? daysBetween(simc.hotfixDate, new Date(manifest.generatedAt))
      : null

  return (
    <Panel>
      <PanelHeader
        title="Which patch this is"
        subtitle="Every figure on this site comes from one snapshot of the game's data. This is that snapshot, and the date after which nothing here knows what changed."
      />
      <dl className="grid gap-4 px-5 pb-4 sm:grid-cols-2 lg:grid-cols-4">
        <Row
          label="Game build"
          value={simc.wowVersion ?? 'not recorded'}
          detail={
            simc.ptr
              ? 'Read from the PTR data set, which is what a tier simc ships before release is built from.'
              : 'Read from the live data set.'
          }
        />
        <Row
          label="Data cutoff"
          value={simc.hotfixDate ?? 'not recorded'}
          detail={
            simc.hotfixDate
              ? `The game-data hotfix these numbers were built from — ${age(cutoffAge)}. Anything Blizzard changed after it, class tuning included, is not in the figures below.`
              : 'This dataset does not record which data snapshot it came from.'
          }
        />
        <Row
          label="SimulationCraft"
          value={simc.simcVersion ?? 'not recorded'}
          detail={[
            simc.gitRevision ? `revision ${simc.gitRevision}` : null,
            simc.gitBranch ? `branch ${simc.gitBranch}` : null,
            simc.buildDate ? `built ${simc.buildDate}` : null,
          ]
            .filter(Boolean)
            .join(', ')}
        />
        <Row
          label="Numbers last changed"
          value={
            manifest.generatedAt ? new Date(manifest.generatedAt).toISOString().slice(0, 10) : '—'
          }
          detail={`${age(publishedAge)}. The sims are deterministic, so this date only moves when a number moved — a quiet night leaves it alone rather than restamping it.`}
        />
      </dl>
      <p className="border-t border-hairline px-5 py-3 text-[12.5px] leading-relaxed text-ink-tertiary">
        {lag !== null && lag > 0 ? (
          <>
            The data snapshot is <strong className="text-ink-secondary">{lag} days older</strong>{' '}
            than the run that produced these numbers, so the publish date above is not the cutoff —{' '}
            <strong className="text-ink-secondary">{simc.hotfixDate}</strong> is.{' '}
          </>
        ) : null}
        This page states which build is modelled; it does not check Blizzard&rsquo;s patch notes,
        so it will never claim a specific tuning change is or is not included. To find out, compare
        the change&rsquo;s date against the cutoff — and if it is newer, the run that picks it up is
        the one after SimulationCraft regenerates its data.
      </p>
    </Panel>
  )
}
