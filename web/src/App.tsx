import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AppHeader, type ViewId } from './components/AppHeader'
import { SpecPicker } from './components/SpecPicker'
import { ErrorState, Note, Panel, Spinner } from './components/ui'
import { loadGear, loadManifest, loadSpecs, loadTierIndex } from './lib/data'
import { describeConvergence, samplingError } from './lib/format'
import { MAX_SERIES, SeriesPalette } from './lib/palette'
import type { GearDataset, Manifest, SpecDetail, TierIndex } from './lib/types'
import { BuildsView, groupBySpec } from './views/BuildsView'
import { FunnelView } from './views/FunnelView'
import { GearView, gearSpecIds } from './views/GearView'
import { OverviewView } from './views/OverviewView'
import { ScalingView } from './views/ScalingView'
import { SpecDetailView } from './views/SpecDetailView'
import { TimingView } from './views/TimingView'

const VIEWS: ViewId[] = ['overview', 'scaling', 'funnel', 'builds', 'gear', 'timing', 'spec']
type Theme = 'light' | 'dark' | 'system'

/** URL state, so a configured comparison is a link somebody can share. */
interface UrlState {
  view: ViewId
  /** Which tier's dataset is loaded. Null follows whichever tier is current. */
  tier: string | null
  scenario: string | null
  selected: string[]
  focus: string | null
  /** Builds view: which spec's builds are being compared. */
  buildSpec: string | null
}

function readUrl(): UrlState {
  const params = new URLSearchParams(window.location.search)
  const view = params.get('view')
  const selected = params.get('specs')
  return {
    view: VIEWS.includes(view as ViewId) ? (view as ViewId) : 'overview',
    tier: params.get('tier'),
    scenario: params.get('scenario'),
    selected: selected ? selected.split(',').filter(Boolean).slice(0, MAX_SERIES) : [],
    focus: params.get('spec'),
    buildSpec: params.get('buildSpec'),
  }
}

function writeUrl(state: UrlState): void {
  const params = new URLSearchParams()
  if (state.view !== 'overview') params.set('view', state.view)
  if (state.tier) params.set('tier', state.tier)
  if (state.scenario) params.set('scenario', state.scenario)
  if (state.selected.length) params.set('specs', state.selected.join(','))
  if (state.focus) params.set('spec', state.focus)
  if (state.buildSpec) params.set('buildSpec', state.buildSpec)
  const query = params.toString()
  const next = query ? `${window.location.pathname}?${query}` : window.location.pathname
  window.history.replaceState(null, '', next)
}

function describeLoadFailure(error: unknown): string {
  return error instanceof Error
    ? `${error.message}. If this is a fresh checkout, the dataset has not been generated yet — see the README for how to run the simulations.`
    : 'Could not load the dataset.'
}

export default function App() {
  const initial = useRef(readUrl()).current

  const [tierIndex, setTierIndex] = useState<TierIndex | null>(null)
  const [tier, setTier] = useState<string | null>(initial.tier)
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const [view, setView] = useState<ViewId>(initial.view)
  const [scenarioId, setScenarioId] = useState<string | null>(initial.scenario)
  const [selected, setSelected] = useState<string[]>(initial.selected)
  const [focus, setFocus] = useState<string | null>(initial.focus)
  const [buildSpec, setBuildSpec] = useState<string | null>(initial.buildSpec)

  const [gear, setGear] = useState<GearDataset | null>(null)
  const [details, setDetails] = useState<SpecDetail[]>([])
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [theme, setTheme] = useState<Theme>(readTheme)

  // The tier index comes first: it decides which tier's files to fetch, and an
  // unknown tier in the URL falls back to the current one rather than 404ing.
  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    loadTierIndex()
      .then((index) => {
        if (cancelled) return
        setTierIndex(index)
        setTier((current) =>
          current && index.tiers.some((entry) => entry.id === current) ? current : index.current,
        )
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setLoadError(describeLoadFailure(error))
      })
    return () => {
      cancelled = true
    }
  }, [reloadToken])

  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setLoadError(null)
    setManifest(null)
    loadManifest(tier)
      .then((data) => {
        if (!cancelled) setManifest(data)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setLoadError(describeLoadFailure(error))
      })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // Optional per tier: a tier can have a spec dataset without a gear sweep, and
  // the view says so rather than the app failing to load.
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setGear(null)
    loadGear(tier).then((data) => {
      if (!cancelled) setGear(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  const scenarios = manifest?.scenarios ?? []
  const scenario = useMemo(
    () => scenarios.find((entry) => entry.id === scenarioId) ?? scenarios[0] ?? null,
    [scenarios, scenarioId],
  )

  // The palette hands out colour slots per spec id and keeps them until the spec
  // is deselected, so filtering never repaints the remaining series.
  // The gear sweep covers part of the tier, so its picker is narrowed to what was
  // swept and opens on everything it has -- the chart's whole point is the
  // cross-spec comparison, and an empty default would hide it behind six clicks.
  const gearIds = useMemo(() => gearSpecIds(gear), [gear])
  const gearVisible = useMemo(() => {
    const covered = selected.filter((id) => gearIds.includes(id))
    return covered.length ? covered : gearIds.slice(0, MAX_SERIES)
  }, [selected, gearIds])

  const palette = useRef(new SeriesPalette()).current
  // Colour follows the entity, so the ids handed to the palette are the ones
  // actually being drawn -- otherwise the gear view's default set has no slots.
  palette.sync(view === 'gear' ? gearVisible : selected)
  const colorOf = useCallback((id: string) => palette.colorOf(id), [palette])

  // Views other than the overview need the full per-spec files.
  const needsDetails = view === 'scaling' || view === 'funnel' || view === 'timing'
  useEffect(() => {
    if (!tier || !needsDetails || selected.length === 0) {
      setDetails([])
      return
    }
    let cancelled = false
    setDetailsLoading(true)
    loadSpecs(tier, selected)
      .then((loaded) => {
        if (!cancelled) setDetails(loaded)
      })
      .catch(() => {
        if (!cancelled) setDetails([])
      })
      .finally(() => {
        if (!cancelled) setDetailsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [needsDetails, selected, tier])

  const [focusDetail, setFocusDetail] = useState<SpecDetail | null>(null)
  useEffect(() => {
    if (!tier || view !== 'spec' || !focus) {
      setFocusDetail(null)
      return
    }
    let cancelled = false
    loadSpecs(tier, [focus])
      .then(([loaded]) => {
        if (!cancelled) setFocusDetail(loaded ?? null)
      })
      .catch(() => {
        if (!cancelled) setFocusDetail(null)
      })
    return () => {
      cancelled = true
    }
  }, [view, focus, tier])

  // The builds view compares one spec's own hero-talent builds, so it is addressed
  // by a spec id rather than through the shared multi-spec picker.
  const buildGroups = useMemo(() => groupBySpec(manifest?.specs ?? []), [manifest])
  const activeBuildSpec = buildSpec ?? buildGroups[0]?.specId ?? null
  const buildIds = useMemo(
    () =>
      buildGroups.find((group) => group.specId === activeBuildSpec)?.builds.map((b) => b.id) ??
      [],
    [buildGroups, activeBuildSpec],
  )
  const buildKey = buildIds.join(',')
  const [buildDetails, setBuildDetails] = useState<SpecDetail[]>([])
  const [buildsLoading, setBuildsLoading] = useState(false)
  useEffect(() => {
    if (!tier || view !== 'builds' || !buildKey) {
      setBuildDetails([])
      return
    }
    let cancelled = false
    setBuildsLoading(true)
    loadSpecs(tier, buildKey.split(','))
      .then((loaded) => {
        if (!cancelled) setBuildDetails(loaded)
      })
      .catch(() => {
        if (!cancelled) setBuildDetails([])
      })
      .finally(() => {
        if (!cancelled) setBuildsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [view, buildKey, tier])

  // Switching tier changes the spec list: season 1 has builds season 2 does not,
  // and vice versa. Selections the new tier does not contain have to go, or the
  // views fail their fetches -- but dropping them silently would leave a comparison
  // quietly missing a line, so the count is reported until the next change.
  const [droppedBuilds, setDroppedBuilds] = useState(0)
  useEffect(() => {
    if (!manifest) return
    const available = new Set(manifest.specs.map((spec) => spec.id))
    setSelected((current) => {
      const kept = current.filter((id) => available.has(id))
      setDroppedBuilds(current.length - kept.length)
      return kept.length === current.length ? current : kept
    })
    setFocus((current) => (current && !available.has(current) ? null : current))
  }, [manifest])

  useEffect(() => {
    writeUrl({ view, tier, scenario: scenario?.id ?? null, selected, focus, buildSpec })
  }, [view, tier, scenario, selected, focus, buildSpec])

  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = theme
    try {
      if (theme === 'system') localStorage.removeItem('wowdps-theme')
      else localStorage.setItem('wowdps-theme', theme)
    } catch {
      /* private mode: the toggle still works for this session */
    }
  }, [theme])

  const toggleSpec = useCallback((id: string) => {
    setDroppedBuilds(0)
    setSelected((current) =>
      current.includes(id)
        ? current.filter((entry) => entry !== id)
        : current.length >= MAX_SERIES
          ? current
          : [...current, id],
    )
  }, [])

  const openSpec = useCallback((id: string) => {
    setFocus(id)
    setView('spec')
  }, [])

  if (loadError) {
    return (
      <Shell
        theme={theme}
        onThemeToggle={() => setTheme(nextTheme)}
        view={view}
        onViewChange={setView}
        manifest={null}
        tierIndex={tierIndex}
        tier={tier}
        onTierChange={setTier}
      >
        <Panel>
          <ErrorState error={loadError} onRetry={() => setReloadToken((token) => token + 1)} />
        </Panel>
      </Shell>
    )
  }

  if (!manifest || !scenario) {
    return (
      <Shell
        theme={theme}
        onThemeToggle={() => setTheme(nextTheme)}
        view={view}
        onViewChange={setView}
        manifest={manifest}
        tierIndex={tierIndex}
        tier={tier}
        onTierChange={setTier}
      >
        <Panel>
          <Spinner label="Loading simulation data…" />
        </Panel>
      </Shell>
    )
  }

  const comparisonView = needsDetails || view === 'gear'
  const tierLabel =
    tierIndex?.tiers.find((entry) => entry.id === tier)?.label ?? `tier ${manifest.tier}`

  return (
    <Shell
      theme={theme}
      onThemeToggle={() => setTheme(nextTheme)}
      view={view}
      onViewChange={setView}
      manifest={manifest}
      tierIndex={tierIndex}
      tier={tier}
      onTierChange={setTier}
    >
      {comparisonView ? (
        <SpecPicker
          specs={
            view === 'gear'
              ? manifest.specs.filter((spec) => gearIds.includes(spec.id))
              : manifest.specs
          }
          selected={selected}
          onToggle={toggleSpec}
          onClear={() => {
            setDroppedBuilds(0)
            setSelected([])
          }}
          colorOf={colorOf}
          max={MAX_SERIES}
        />
      ) : null}

      {droppedBuilds > 0 ? (
        <Panel>
          <Note>
            {droppedBuilds === 1 ? 'One build was' : `${droppedBuilds} builds were`} dropped
            from the comparison: SimulationCraft ships no profile for{' '}
            {droppedBuilds === 1 ? 'it' : 'them'} in {tierLabel}.
          </Note>
        </Panel>
      ) : null}

      {view === 'overview' ? (
        <OverviewView
          manifest={manifest}
          scenario={scenario}
          onScenarioChange={setScenarioId}
          onOpenSpec={openSpec}
        />
      ) : null}

      {comparisonView && detailsLoading ? (
        <Panel>
          <Spinner label="Loading builds…" />
        </Panel>
      ) : null}

      {view === 'scaling' && !detailsLoading ? (
        <ScalingView details={details} scenario={scenario} colorOf={colorOf} />
      ) : null}

      {view === 'funnel' && !detailsLoading ? (
        <FunnelView details={details} scenario={scenario} colorOf={colorOf} />
      ) : null}

      {view === 'builds' ? (
        buildsLoading ? (
          <Panel>
            <Spinner label="Loading builds…" />
          </Panel>
        ) : (
          <BuildsView
            specs={manifest.specs}
            specId={activeBuildSpec}
            onSpecChange={setBuildSpec}
            details={buildDetails}
            scenario={scenario}
          />
        )
      ) : null}

      {view === 'gear' ? (
        <GearView gear={gear} visible={gearVisible} colorOf={colorOf} />
      ) : null}

      {view === 'timing' && !detailsLoading ? (
        <TimingView details={details} scenario={scenario} colorOf={colorOf} />
      ) : null}

      {view === 'spec' ? (
        <SpecDetailView
          detail={focusDetail}
          scenario={scenario}
          allSpecs={manifest.specs}
          onSelectSpec={setFocus}
        />
      ) : null}

      <Footer manifest={manifest} />
    </Shell>
  )
}

function Shell({
  children,
  manifest,
  tierIndex,
  tier,
  onTierChange,
  view,
  onViewChange,
  theme,
  onThemeToggle,
}: {
  children: React.ReactNode
  manifest: Manifest | null
  tierIndex: TierIndex | null
  tier: string | null
  onTierChange: (tier: string) => void
  view: ViewId
  onViewChange: (view: ViewId) => void
  theme: Theme
  onThemeToggle: () => void
}) {
  return (
    <div className="min-h-dvh bg-page">
      <AppHeader
        manifest={manifest}
        tierIndex={tierIndex}
        tier={tier}
        onTierChange={onTierChange}
        view={view}
        onViewChange={onViewChange}
        theme={theme}
        onThemeToggle={onThemeToggle}
      />
      <main className="mx-auto max-w-[1400px] space-y-4 px-5 py-6">{children}</main>
    </div>
  )
}

function Footer({ manifest }: { manifest: Manifest }) {
  const { simc, tier, settings } = manifest
  return (
    <footer className="border-t border-hairline pt-5 text-[12.5px] leading-relaxed text-ink-muted">
      <p className="max-w-3xl">
        Every number here comes from SimulationCraft {simc.simcVersion ?? ''} running its own{' '}
        {tier} tier profiles: {describeConvergence(settings)}, which measures DPS to about{' '}
        {samplingError(settings)} standard error. Sims model a stationary target and perfect
        play; real fights add movement, mechanics and mistakes. Use this to understand the
        shape of a spec, not to predict your own parse.
      </p>
      <p className="mt-2">
        Not affiliated with Blizzard Entertainment. World of Warcraft is a trademark of
        Blizzard Entertainment, Inc.
      </p>
    </footer>
  )
}

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem('wowdps-theme')
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    /* private mode */
  }
  return 'system'
}

function nextTheme(current: Theme): Theme {
  return current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system'
}
