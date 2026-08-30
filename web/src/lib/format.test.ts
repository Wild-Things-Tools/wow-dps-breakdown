/**
 * What the footer is allowed to claim about the run's precision.
 *
 * The bug these pin: `settings.medianDpsError` is ONE median over ALL scenarios,
 * and the population is lopsided — 510 of MID2's 864 cells are Patchwerk. The
 * footer printed that pooled figure on every view as "measures DPS to about X
 * standard error", understating Hectic Add Cleave and Dungeon Slice by a factor of
 * two (issue #103). The chart caption solves it by taking the median of the rows on
 * screen; the footer has no such context and names the range instead.
 */

import { describe, expect, it } from 'vitest'

import { describeSamplingError, samplingErrorRange } from './format'
import type { Manifest } from './types'

/** A manifest carrying only what these functions read. */
function manifest(cells: Record<string, number[]>, medianDpsError: number | null = 0.0555) {
  const scenarios: Record<string, unknown> = {}
  for (const [name, errors] of Object.entries(cells)) {
    scenarios[name] = {
      dpsError: Object.fromEntries(errors.map((value, index) => [String(index + 1), value])),
    }
  }
  return {
    settings: { targetError: 0, maxIterations: 3000, deterministic: true, medianDpsError },
    specs: [{ scenarios }],
  } as unknown as Manifest
}

describe('samplingErrorRange', () => {
  it('spans the scenarios rather than pooling them', () => {
    // MID2's real medians on 2026-08-30, rounded: the pooled figure is 0.0555 and
    // the noisiest scenario is more than twice that.
    const range = samplingErrorRange(
      manifest({
        patchwerk: [0.0527, 0.0527, 0.0527],
        hecticaddcleave: [0.1177],
        boss_53445: [0.0457],
      }),
    )
    expect(range).toEqual({ low: 0.0457, high: 0.1177 })
  })

  it('takes each scenario s median, not its worst cell', () => {
    // One unlucky cell must not become the headline. Patchwerk's median here is
    // 0.05 and its maximum is 0.4.
    const range = samplingErrorRange(manifest({ patchwerk: [0.04, 0.05, 0.4] }))
    expect(range).toEqual({ low: 0.05, high: 0.05 })
  })

  it('ignores zeroes, because a zero is not a measurement', () => {
    // The "converged to 0% standard error" footer bug, one field over: in
    // deterministic mode nothing is requested, so a zero means unmeasured.
    const range = samplingErrorRange(manifest({ patchwerk: [0, 0, 0.06] }))
    expect(range).toEqual({ low: 0.06, high: 0.06 })
  })

  it('is null when there is nothing to measure, so the caller can fall back', () => {
    expect(samplingErrorRange(manifest({}))).toBeNull()
    expect(samplingErrorRange(manifest({ patchwerk: [0] }))).toBeNull()
  })
})

describe('describeSamplingError', () => {
  it('names the range when the scenarios disagree', () => {
    const text = describeSamplingError(
      manifest({ patchwerk: [0.0527], hecticaddcleave: [0.1177] }),
    )
    expect(text).toBe('0.05% to 0.12% depending on the scenario')
  })

  it('states one figure when the ends round to the same one', () => {
    // "0.05% to 0.05% depending on the scenario" is worse than the point value it
    // came from -- it advertises a spread the reader cannot see.
    expect(describeSamplingError(manifest({ a: [0.0527], b: [0.0533] }))).toBe('0.05%')
  })

  it('falls back to the pooled figure on a dataset with no per-cell errors', () => {
    // An older manifest publishes no dpsError maps. The pooled median is then the
    // only thing anyone measured, and it is still true of the run as a whole.
    expect(describeSamplingError(manifest({}, 0.0591))).toBe('0.06%')
  })
})
