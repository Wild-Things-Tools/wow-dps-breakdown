import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AppHeader, type ViewId } from './components/AppHeader'
import { ErrorState, Panel, Spinner } from './components/ui'
import {
  loadFights,
  loadGear,
  loadBuffs,
  loadComputedBuilds,
  loadSpecIndex,
  loadTalentTrees,
  loadLogsVerification,
  loadManifest,
  loadTalents,
  loadSpecs,
  loadTierIndex,
} from './lib/data'
import { describeConvergence, describeGameBuild, samplingError } from './lib/format'
import type {
  BuffDataset,
  ComputedBuildsDataset,
  FightsDataset,
  GearDataset,
  LogsVerification,
  Manifest,
  SpecDetail,
  SpecIndex,
  TalentDataset,
  TierIndex,
  TalentTreeDataset,
} from './lib/types'
import { BuffsView } from './views/BuffsView'
import { BuildsView } from './views/BuildsView'
import { FightsView } from './views/FightsView'
import { FunnelView } from './views/FunnelView'
import { GearView } from './views/GearView'
import { LogsView } from './views/LogsView'
import { OverviewView } from './views/OverviewView'
import { ScalingView } from './views/ScalingView'
import { SpecDetailView } from './views/SpecDetailView'
import { TimingView } from './views/TimingView'

const VIEWS: ViewId[] = [
  'overview',
  'scaling',
  'funnel',
  'builds',
  'gear',
  'fights',
  'logs',
  'timing',
  'spec',
]

/** Views that need the full per-spec files rather than just the manifest. */
const DETAIL_VIEWS: ViewId[] = ['scaling', 'funnel', 'timing', 'builds']

/**
 * URL state, so a configured view is a link somebody can share.
 *
 * There is no `specs` parameter any more: nothing on this site is selected before
 * it will show you anything, so there is no selection to put in a link.
 */
interface UrlState {
  view: ViewId
  /** Which tier's dataset is loaded. Null follows whichever tier is current. */
  tier: string | null
  scenario: string | null
  /** Spec detail: which build is open. Null opens the top build of the scenario. */
  focus: string | null
  /** Fights view: which encounter is open. */
  boss: number | null
}

function readUrl(): UrlState {
  const params = new URLSearchParams(window.location.search)
  const view = params.get('view')
  return {
    view: VIEWS.includes(view as ViewId) ? (view as ViewId) : 'overview',
    tier: params.get('tier'),
    scenario: params.get('scenario'),
    focus: params.get('spec'),
    boss: Number(params.get('boss')) || null,
  }
}

function writeUrl(state: UrlState): void {
  const params = new URLSearchParams()
  if (state.view !== 'overview') params.set('view', state.view)
  if (state.tier) params.set('tier', state.tier)
  if (state.scenario) params.set('scenario', state.scenario)
  if (state.focus) params.set('spec', state.focus)
  if (state.boss) params.set('boss', String(state.boss))
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
  // How many BUILDS the tier holds now. The sweeps' own `specsAvailable` is the
  // tier's size on the day each ran, so only the manifest can say whether a sweep
  // has fallen behind -- see `lib/sweepCoverage.ts`.
  //
  // Deliberately `specs.length` and NOT `manifest.coverage`: that block counts
  // damage SPECS (18 of 26 on MID2), while gear and buffs are swept per BUILD and
  // MID2 publishes 52 of those. Joining the two would be the same wrong-column
  // mistake this whole change exists to fix, one level up.
  const tierBuilds = manifest?.specs.length ?? null
  const [reloadToken, setReloadToken] = useState(0)

  const [view, setView] = useState<ViewId>(initial.view)
  const [scenarioId, setScenarioId] = useState<string | null>(initial.scenario)
  const [focus, setFocus] = useState<string | null>(initial.focus)
  const [boss, setBoss] = useState<number | null>(initial.boss)

  const [gear, setGear] = useState<GearDataset | null>(null)
  const [logs, setLogs] = useState<LogsVerification | null>(null)
  const [talents, setTalents] = useState<TalentDataset | null>(null)
  const [details, setDetails] = useState<SpecDetail[]>([])
  const [detailsLoading, setDetailsLoading] = useState(false)

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

  // Same optional-dataset treatment: fights.json is only there once the probe has
  // published, and the view explains its own absence.
  const [fights, setFights] = useState<FightsDataset | null>(null)
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setFights(null)
    loadFights(tier).then((data) => {
      if (!cancelled) setFights(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // And again for the decoded talent trees, which only the Spec detail view draws
  // and which are ~400 KB for a tier -- so they are fetched on their own rather than
  // riding along with the manifest every view pays for.
  const [talentTrees, setTalentTrees] = useState<TalentTreeDataset | null>(null)
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setTalentTrees(null)
    loadTalentTrees(tier).then((data) => {
      if (!cancelled) setTalentTrees(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // Every class and spec in the game, which the Spec detail picker draws so that a
  // spec's absence reads as absence. ~10 KB, and only that view needs it.
  // Tier set and Power Infusion values. Small, and only one view draws them.
  const [buffs, setBuffs] = useState<BuffDataset | null>(null)
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setBuffs(null)
    loadBuffs(tier).then((data) => {
      if (!cancelled) setBuffs(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  const [specIndex, setSpecIndex] = useState<SpecIndex | null>(null)
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setSpecIndex(null)
    loadSpecIndex(tier).then((data) => {
      if (!cancelled) setSpecIndex(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // Builds this project computed, beside simc's own. Its own fetch, because it
  // is optional -- a tier that has never been through `wowdps build-search` has
  // no such file, and `null` then means "no computed build", never an error.
  const [computedBuilds, setComputedBuilds] =
    useState<ComputedBuildsDataset | null>(null)
  const [computedSettled, setComputedSettled] = useState(false)
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setComputedBuilds(null)
    setComputedSettled(false)
    loadComputedBuilds(tier).then((data) => {
      if (cancelled) return
      setComputedBuilds(data)
      // Separate from the value: "still asking" and "asked, there is none" are
      // different states and the footnote says a different sentence for each.
      setComputedSettled(true)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // And again for the logs cross-check, which needs Warcraft Logs credentials and
  // so only exists for a tier somebody has run `wowdps verify` against.
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setLogs(null)
    loadLogsVerification(tier).then((data) => {
      if (!cancelled) setLogs(data)
    })
    return () => {
      cancelled = true
    }
  }, [tier, reloadToken])

  // The build comparison with the gear held still. Same optional treatment again:
  // the file exists once `wowdps talents` has been run for the tier.
  useEffect(() => {
    if (!tier) return
    let cancelled = false
    setTalents(null)
    loadTalents(tier).then((data) => {
      if (!cancelled) setTalents(data)
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

  // Every comparison view now covers the whole tier, so they all want the same
  // thing: every per-spec file. One list, fetched once, cached by `loadSpec`.
  const allSpecIds = useMemo(() => (manifest?.specs ?? []).map((spec) => spec.id), [manifest])
  const specKey = allSpecIds.join(',')
  const needsDetails = DETAIL_VIEWS.includes(view)
  useEffect(() => {
    if (!tier || !needsDetails || !specKey) {
      return
    }
    let cancelled = false
    setDetailsLoading(true)
    loadSpecs(tier, specKey.split(','))
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
  }, [needsDetails, specKey, tier])

  // Spec detail is inherently one build at a time, so it opens on the scenario's
  // top build rather than on an empty picker.
  const defaultFocus = useMemo(() => {
    if (!manifest || !scenario) return null
    const key = String(scenario.targetCounts[0] ?? 1)
    let best: { id: string; dps: number } | null = null
    for (const spec of manifest.specs) {
      const dps = spec.scenarios[scenario.id]?.dps[key]
      if (typeof dps !== 'number') continue
      if (!best || dps > best.dps) best = { id: spec.id, dps }
    }
    return best?.id ?? manifest.specs[0]?.id ?? null
  }, [manifest, scenario])
  const activeFocus = focus ?? defaultFocus

  const [focusDetail, setFocusDetail] = useState<SpecDetail | null>(null)
  useEffect(() => {
    if (!tier || view !== 'spec' || !activeFocus) {
      setFocusDetail(null)
      return
    }
    let cancelled = false
    loadSpecs(tier, [activeFocus])
      .then(([loaded]) => {
        if (!cancelled) setFocusDetail(loaded ?? null)
      })
      .catch(() => {
        if (!cancelled) setFocusDetail(null)
      })
    return () => {
      cancelled = true
    }
  }, [view, activeFocus, tier])

  // Switching tier changes the spec list: season 1 has builds season 2 does not,
  // and vice versa. A build the new tier has no profile for cannot stay open.
  useEffect(() => {
    if (!manifest) return
    const available = new Set(manifest.specs.map((spec) => spec.id))
    setFocus((current) => (current && !available.has(current) ? null : current))
    setDetails((current) => current.filter((detail) => available.has(detail.id)))
  }, [manifest])

  // Same for the open boss: seasons have different raids, so an encounter id from
  // one season means nothing in the next. Dropped explicitly rather than left in
  // the URL pointing at a boss the view quietly fell back from.
  useEffect(() => {
    if (!fights) return
    const available = new Set(fights.encounters.map((entry) => entry.encounterId))
    setBoss((current) => (current && !available.has(current) ? null : current))
  }, [fights])

  useEffect(() => {
    writeUrl({ view, tier, scenario: scenario?.id ?? null, focus, boss })
  }, [view, tier, scenario, focus, boss])

  const openSpec = useCallback((id: string) => {
    setFocus(id)
    setView('spec')
  }, [])

  if (loadError) {
    return (
      <Shell
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

  const waitingForDetails = needsDetails && (detailsLoading || details.length === 0)

  return (
    <Shell
      view={view}
      onViewChange={setView}
      manifest={manifest}
      tierIndex={tierIndex}
      tier={tier}
      onTierChange={setTier}
    >
      {view === 'overview' ? (
        <OverviewView
          manifest={manifest}
          scenario={scenario}
          specIndex={specIndex}
          computedBuilds={computedBuilds}
          computedSettled={computedSettled}
          onScenarioChange={setScenarioId}
          onOpenSpec={openSpec}
        />
      ) : null}

      {waitingForDetails ? (
        <Panel>
          <Spinner label="Loading every build in the tier…" />
        </Panel>
      ) : null}

      {view === 'scaling' && !waitingForDetails ? (
        <ScalingView details={details} scenario={scenario} />
      ) : null}

      {view === 'funnel' && !waitingForDetails ? (
        <FunnelView details={details} scenario={scenario} />
      ) : null}

      {view === 'builds' && !waitingForDetails ? (
        <BuildsView details={details} scenario={scenario} talents={talents} />
      ) : null}

      {view === 'gear' ? <GearView gear={gear} tierBuilds={tierBuilds} /> : null}

      {view === 'buffs' ? <BuffsView data={buffs} tierBuilds={tierBuilds} /> : null}

      {view === 'fights' ? (
        <FightsView
          fights={fights}
          encounterId={boss}
          onEncounterChange={setBoss}
          // The season control on this view *is* the tier control. One piece of
          // state, one `tier=` in the URL, one answer everywhere -- see the
          // reasoning on `Season` in FightsView.
          tierIndex={tierIndex}
          tier={tier}
          onTierChange={setTier}
        />
      ) : null}

      {view === 'logs' ? <LogsView logs={logs} specs={manifest.specs} /> : null}

      {view === 'timing' && !waitingForDetails ? (
        <TimingView details={details} scenario={scenario} />
      ) : null}

      {view === 'spec' ? (
        <SpecDetailView
          detail={focusDetail}
          scenario={scenario}
          allSpecs={manifest.specs}
          onSelectSpec={setFocus}
          specIndex={specIndex}
          talentTrees={talentTrees}
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
}: {
  children: React.ReactNode
  manifest: Manifest | null
  tierIndex: TierIndex | null
  tier: string | null
  onTierChange: (tier: string) => void
  view: ViewId
  onViewChange: (view: ViewId) => void
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
      <p className="mt-2 text-ink-secondary">{describeGameBuild(simc)}</p>
      <p className="mt-2">
        Class, specialisation and hero-talent icons are Blizzard artwork served by Wowhead;
        this site is not affiliated with either. World of Warcraft is a trademark of
        Blizzard Entertainment, Inc.
      </p>
    </footer>
  )
}
