import { formatDate, relativeAge } from '../lib/format'
import type { Manifest } from '../lib/types'
import { Button, cx } from './ui'

export type ViewId = 'overview' | 'scaling' | 'funnel' | 'builds' | 'timing' | 'spec'

const VIEWS: Array<{ id: ViewId; label: string; blurb: string }> = [
  { id: 'overview', label: 'Overview', blurb: 'Rank every spec at one target count' },
  { id: 'scaling', label: 'Target scaling', blurb: 'How DPS changes from 1 to 10 targets' },
  { id: 'funnel', label: 'Funnel', blurb: 'How much damage lands on the main target' },
  { id: 'builds', label: 'Builds', blurb: 'Which hero-talent build leads, and where that flips' },
  { id: 'timing', label: 'Timing', blurb: 'When during a fight the damage happens' },
  { id: 'spec', label: 'Spec detail', blurb: 'Ability breakdown for one build' },
]

export function AppHeader({
  manifest,
  view,
  onViewChange,
  theme,
  onThemeToggle,
}: {
  manifest: Manifest | null
  view: ViewId
  onViewChange: (view: ViewId) => void
  theme: 'light' | 'dark' | 'system'
  onThemeToggle: () => void
}) {
  return (
    <header className="border-b border-hairline bg-surface">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-4 px-5 pt-5 pb-4">
        <div>
          <h1 className="text-[17px] font-semibold tracking-tight text-ink">
            WoW DPS Breakdown
          </h1>
          <p className="mt-0.5 text-[13px] text-ink-secondary">
            Where damage goes, and when — simulated with SimulationCraft.
          </p>
        </div>

        <div className="flex items-center gap-3">
          {manifest ? <Provenance manifest={manifest} /> : null}
          <Button
            onClick={onThemeToggle}
            title={`Theme: ${theme}. Click to change.`}
          >
            {theme === 'dark' ? 'Dark' : theme === 'light' ? 'Light' : 'Auto'}
          </Button>
        </div>
      </div>

      <nav className="mx-auto max-w-[1400px] px-5" aria-label="Views">
        <ul className="-mb-px flex flex-wrap gap-1">
          {VIEWS.map((entry) => (
            <li key={entry.id}>
              <button
                type="button"
                onClick={() => onViewChange(entry.id)}
                aria-current={view === entry.id ? 'page' : undefined}
                title={entry.blurb}
                className={cx(
                  'border-b-2 px-3 py-2.5 text-[13.5px] font-medium transition-colors',
                  view === entry.id
                    ? 'border-ink text-ink'
                    : 'border-transparent text-ink-secondary hover:text-ink',
                )}
              >
                {entry.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  )
}

function Provenance({ manifest }: { manifest: Manifest }) {
  const { simc, tier, generatedAt } = manifest
  const parts = [
    `Tier ${tier}`,
    simc.simcVersion ? `simc ${simc.simcVersion}` : null,
    simc.ptr ? 'PTR data' : null,
  ].filter(Boolean)

  return (
    <dl
      className="hidden text-right text-[12px] leading-snug text-ink-muted sm:block"
      title={`Generated ${formatDate(generatedAt)}${
        simc.gitRevision ? ` from simc ${simc.gitRevision}` : ''
      }`}
    >
      <div>
        <dt className="sr-only">Data source</dt>
        <dd>{parts.join(' · ')}</dd>
      </div>
      <div>
        <dt className="sr-only">Last updated</dt>
        <dd>Updated {relativeAge(generatedAt)}</dd>
      </div>
    </dl>
  )
}
