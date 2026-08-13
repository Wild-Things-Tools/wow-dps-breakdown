import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AppHeader, type ViewId } from './components/AppHeader'
import { SpecPicker } from './components/SpecPicker'
import { ErrorState, Panel, Spinner } from './components/ui'
import { loadManifest, loadSpecs } from './lib/data'
import { describeConvergence, samplingError } from './lib/format'
import { MAX_SERIES, SeriesPalette } from './lib/palette'
import type { Manifest, SpecDetail } from './lib/types'
import { FunnelView } from './views/FunnelView'
import { OverviewView } from './views/OverviewView'
import { ScalingView } from './views/ScalingView'
import { SpecDetailView } from './views/SpecDetailView'
import { TimingView } from './views/TimingView'

const VIEWS: ViewId[] = ['overview', 'scaling', 'funnel', 'timing', 'spec']
type Theme = 'light' | 'dark' | 'system'

/** URL state, so a configured comparison is a link somebody can share. */
interface UrlState {
  view: ViewId
  scenario: string | null
  selected: string[]
  focus: string | null
}

function readUrl(): UrlState {
  const params = new URLSearchParams(window.location.search)
  const view = params.get('view')
  const selected = params.get('specs')
  return {
    view: VIEWS.includes(view as ViewId) ? (view as ViewId) : 'overview',
    scenario: params.get('scenario'),
    selected: selected ? selected.split(',').filter(Boolean).slice(0, MAX_SERIES) : [],
    focus: params.get('spec'),
  }
}

function writeUrl(state: UrlState): void {
  const params = new URLSearchParams()
  if (state.view !== 'overview') params.set('view', state.view)
  if (state.scenario) params.set('scenario', state.scenario)
  if (state.selected.length) params.set('specs', state.selected.join(','))
  if (state.focus) params.set('spec', state.focus)
  const query = params.toString()
  const next = query ? `${window.location.pathname}?${query}` : window.location.pathname
  window.history.replaceState(null, '', next)
}

export default function App() {
  const initial = useRef(readUrl()).current

  const [manifest, setManifest] = useState<Manifest | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const [view, setView] = useState<ViewId>(initial.view)
  const [scenarioId, setScenarioId] = useState<string | null>(initial.scenario)
  const [selected, setSelected] = useState<string[]>(initial.selected)
  const [focus, setFocus] = useState<string | null>(initial.focus)

  const [details, setDetails] = useState<SpecDetail[]>([])
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [theme, setTheme] = useState<Theme>(readTheme)

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    loadManifest()
      .then((data) => {
        if (!cancelled) setManifest(data)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setLoadError(
          error instanceof Error
            ? `${error.message}. If this is a fresh checkout, the dataset has not been generated yet — see the README for how to run the simulations.`
            : 'Could not load the dataset.',
        )
      })
    return () => {
      cancelled = true
    }
  }, [reloadToken])

  const scenarios = manifest?.scenarios ?? []
  const scenario = useMemo(
    () => scenarios.find((entry) => entry.id === scenarioId) ?? scenarios[0] ?? null,
    [scenarios, scenarioId],
  )

  // The palette hands out colour slots per spec id and keeps them until the spec
  // is deselected, so filtering never repaints the remaining series.
  const palette = useRef(new SeriesPalette()).current
  palette.sync(selected)
  const colorOf = useCallback((id: string) => palette.colorOf(id), [palette])

  // Views other than the overview need the full per-spec files.
  const needsDetails = view === 'scaling' || view === 'funnel' || view === 'timing'
  useEffect(() => {
    if (!needsDetails || selected.length === 0) {
      setDetails([])
      return
    }
    let cancelled = false
    setDetailsLoading(true)
    loadSpecs(selected)
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
  }, [needsDetails, selected])

  const [focusDetail, setFocusDetail] = useState<SpecDetail | null>(null)
  useEffect(() => {
    if (view !== 'spec' || !focus) {
      setFocusDetail(null)
      return
    }
    let cancelled = false
    loadSpecs([focus])
      .then(([loaded]) => {
        if (!cancelled) setFocusDetail(loaded ?? null)
      })
      .catch(() => {
        if (!cancelled) setFocusDetail(null)
      })
    return () => {
      cancelled = true
    }
  }, [view, focus])

  useEffect(() => {
    writeUrl({ view, scenario: scenario?.id ?? null, selected, focus })
  }, [view, scenario, selected, focus])

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
      <Shell theme={theme} onThemeToggle={() => setTheme(nextTheme)} view={view} onViewChange={setView} manifest={null}>
        <Panel>
          <ErrorState error={loadError} onRetry={() => setReloadToken((token) => token + 1)} />
        </Panel>
      </Shell>
    )
  }

  if (!manifest || !scenario) {
    return (
      <Shell theme={theme} onThemeToggle={() => setTheme(nextTheme)} view={view} onViewChange={setView} manifest={manifest}>
        <Panel>
          <Spinner label="Loading simulation data…" />
        </Panel>
      </Shell>
    )
  }

  const comparisonView = needsDetails

  return (
    <Shell
      theme={theme}
      onThemeToggle={() => setTheme(nextTheme)}
      view={view}
      onViewChange={setView}
      manifest={manifest}
    >
      {comparisonView ? (
        <SpecPicker
          specs={manifest.specs}
          selected={selected}
          onToggle={toggleSpec}
          onClear={() => setSelected([])}
          colorOf={colorOf}
          max={MAX_SERIES}
        />
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
  view,
  onViewChange,
  theme,
  onThemeToggle,
}: {
  children: React.ReactNode
  manifest: Manifest | null
  view: ViewId
  onViewChange: (view: ViewId) => void
  theme: Theme
  onThemeToggle: () => void
}) {
  return (
    <div className="min-h-dvh bg-page">
      <AppHeader
        manifest={manifest}
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
