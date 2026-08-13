/** Loading the static dataset, with a small in-memory cache. */

import type { LogsVerification, Manifest, SpecDetail } from './types'

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

export function loadManifest(): Promise<Manifest> {
  return fetchJson<Manifest>('index.json')
}

const specCache = new Map<string, Promise<SpecDetail>>()

export function loadSpec(id: string): Promise<SpecDetail> {
  let pending = specCache.get(id)
  if (!pending) {
    pending = fetchJson<SpecDetail>(`specs/${id}.json`)
    // A failed load must not poison the cache: a retry should hit the network.
    pending.catch(() => specCache.delete(id))
    specCache.set(id, pending)
  }
  return pending
}

export function loadSpecs(ids: string[]): Promise<SpecDetail[]> {
  return Promise.all(ids.map(loadSpec))
}

/** Optional: absent until the Warcraft Logs verification job has run. */
export async function loadLogsVerification(): Promise<LogsVerification | null> {
  try {
    return await fetchJson<LogsVerification>('logs-verification.json')
  } catch {
    return null
  }
}
