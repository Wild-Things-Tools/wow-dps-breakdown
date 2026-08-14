/** Loading the static dataset, with a small in-memory cache. */

import type {
  FightsDataset,
  GearDataset,
  LogsVerification,
  Manifest,
  SpecDetail,
  TalentDataset,
  TierIndex,
} from './types'

const BASE = import.meta.env.BASE_URL

function dataUrl(path: string): string {
  return `${BASE}data/${path}`.replace(/([^:])\/\/+/g, '$1/')
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(dataUrl(path))
  if (!response.ok) {
    throw new Error(`Could not load ${path} (HTTP ${response.status})`)
  }
  return (await response.json()) as T
}

/** Which tiers exist. Loaded once, before anything else can be fetched. */
export function loadTierIndex(): Promise<TierIndex> {
  return fetchJson<TierIndex>('tiers.json')
}

export function loadManifest(tier: string): Promise<Manifest> {
  return fetchJson<Manifest>(`${tier}/index.json`)
}

// Keyed by tier as well as spec: the same spec id exists in several tiers and
// means something different in each.
const specCache = new Map<string, Promise<SpecDetail>>()

export function loadSpec(tier: string, id: string): Promise<SpecDetail> {
  const key = `${tier}/${id}`
  let pending = specCache.get(key)
  if (!pending) {
    pending = fetchJson<SpecDetail>(`${tier}/specs/${id}.json`)
    // A failed load must not poison the cache: a retry should hit the network.
    pending.catch(() => specCache.delete(key))
    specCache.set(key, pending)
  }
  return pending
}

export function loadSpecs(tier: string, ids: string[]): Promise<SpecDetail[]> {
  return Promise.all(ids.map((id) => loadSpec(tier, id)))
}

/** Optional: absent until the Warcraft Logs verification job has run. */
export async function loadLogsVerification(tier: string): Promise<LogsVerification | null> {
  try {
    return await fetchJson<LogsVerification>(`${tier}/logs-verification.json`)
  } catch {
    return null
  }
}

/**
 * Gear comparison for one tier. Optional: a tier can have a spec dataset without a
 * gear sweep having been run for it, and the view says so rather than erroring.
 */
export async function loadGear(tier: string): Promise<GearDataset | null> {
  try {
    return await fetchJson<GearDataset>(`${tier}/gear.json`)
  } catch {
    return null
  }
}

/**
 * Per-boss fight shapes for one tier. Optional in the same way the gear sweep is:
 * the file only exists once `wowdps fights` has been run for the tier, and the view
 * says so rather than the app failing to load.
 */
export async function loadFights(tier: string): Promise<FightsDataset | null> {
  try {
    return await fetchJson<FightsDataset>(`${tier}/fights.json`)
  } catch {
    return null
  }
}

/**
 * Build comparison with the gear held still, for one tier. Optional like the gear
 * sweep and the fight shapes: the file exists once `wowdps talents` has been run,
 * and the view explains its own absence rather than the app failing to load.
 */
export async function loadTalents(tier: string): Promise<TalentDataset | null> {
  try {
    return await fetchJson<TalentDataset>(`${tier}/talents.json`)
  } catch {
    return null
  }
}
