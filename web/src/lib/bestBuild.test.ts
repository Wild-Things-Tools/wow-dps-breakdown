/**
 * What the Overview is allowed to claim.
 *
 * `bestBuild.ts` decides which build each row of the published ranking presents
 * and what number it is drawn at. Until now nothing in CI executed a line of it
 * -- `tsc` checked that it compiled and that was the whole of the coverage on a
 * page that is live (issue #54).
 *
 * Two halves. The unit half pins the rules against hand-built contenders. The
 * corpus half runs the real committed MID2 documents through it and asserts
 * PROPERTIES rather than counts: a frozen "12 rows are marked" would go red on
 * the next nightly simulation and the failure would say nothing about this
 * module. What must hold on any dataset is that an unmarked row carries exactly
 * the published number, and that a marked one cleared its own tie band.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  bestBuildFor,
  bestBuildMark,
  combinedNoise,
  computedScope,
  findComputedSpec,
  isReadable,
} from './bestBuild'
import type { ComputedBuildsDataset, ComputedContender, ComputedSpec } from './types'

const HERE = dirname(fileURLToPath(import.meta.url))
const DATA = join(HERE, '..', '..', 'public', 'data', 'MID2')

function contender(dps: number, dpsError: number): ComputedContender {
  return { origin: 'simc', label: 'x', talentHash: 'h', heroTalent: 't', dps, dpsError } as ComputedContender
}

function entry(simc: ComputedContender | null, best: ComputedContender | null): ComputedSpec {
  return { id: 'b', scenario: 'patchwerk', targets: 1, searched: true, simc, best } as ComputedSpec
}

describe('the tie rule', () => {
  it('adds the two errors in quadrature rather than taking either', () => {
    // 0.06% and 0.08% -> 0.1%, the 3-4-5 triangle, so a wrong operator is visible.
    expect(combinedNoise(0.06, 0.08)).toBeCloseTo(0.001, 12)
  })

  it('treats a margin equal to the band as a tie, not a lead', () => {
    // Exactly equal, not approximately: the numbers are chosen so both sides
    // land on the same double. 1026/1024 is 1.001953125 exactly (the divisor is
    // a power of two), and 0.1953125% is 1/512 exactly, so margin === band with
    // no rounding for the assertion to hide behind. A `<` where the code has
    // `<=` then fails here and nowhere else.
    const simc = contender(1024, 0.1953125)
    const best = contender(1026, 0)
    expect(best.dps / simc.dps - 1).toBe(combinedNoise(simc.dpsError, best.dpsError))

    const verdict = bestBuildFor(200_000, entry(simc, best))

    expect(verdict.projected).toBe(false)
    expect(verdict.rankDps).toBe(200_000)
    expect(bestBuildMark(verdict)).toBeNull()
  })

  it('carries the published number forward by the measured ratio once the band is cleared', () => {
    const simc = contender(100_000, 0.05)
    const best = contender(102_000, 0.05)

    // The published figure is deliberately far from the anchored one: the two
    // documents measure different characters, and the whole point of the module
    // is that only the RATIO travels between them.
    const verdict = bestBuildFor(232_961.2, entry(simc, best))

    expect(verdict.projected).toBe(true)
    expect(verdict.simcDps).toBe(232_961.2)
    expect(verdict.rankDps).toBeCloseTo(232_961.2 * 1.02, 6)
    expect(verdict.gain).toBeCloseTo(0.02, 12)
    expect(bestBuildMark(verdict)).toBe('computed +2.00%')
  })

  it('never ranks a row at the anchored DPS', () => {
    // The trap this module exists for: substituting best.dps would push a
    // WINNING build down, because the anchor sits below the shipped kit.
    const verdict = bestBuildFor(232_961.2, entry(contender(100_000, 0.05), contender(102_000, 0.05)))

    expect(verdict.rankDps).not.toBe(102_000)
    expect(verdict.rankDps).toBeGreaterThan(verdict.simcDps)
  })
})

describe('half a side is not a side', () => {
  it.each([
    ['no computed contender', entry(contender(100_000, 0.05), null)],
    ['no simc contender', entry(null, contender(102_000, 0.05))],
    ['no entry at all', null],
  ])('%s leaves the published number untouched', (_name, e) => {
    const verdict = bestBuildFor(232_961.2, e)
    expect(verdict.projected).toBe(false)
    expect(verdict.rankDps).toBe(232_961.2)
  })

  it('rejects a contender whose error is missing, however good its dps looks', () => {
    const half = { origin: 'simc', label: 'x', talentHash: 'h', heroTalent: 't', dps: 102_000 } as ComputedContender
    expect(bestBuildFor(232_961.2, entry(contender(100_000, 0.05), half)).projected).toBe(false)
  })
})

describe('the join and the scope', () => {
  const dataset = {
    schemaVersion: 1,
    specs: [
      { ...entry(contender(1, 0.1), contender(2, 0.1)), id: 'a', targets: 1 },
      { ...entry(contender(1, 0.1), contender(2, 0.1)), id: 'a', targets: 5 },
    ],
  } as ComputedBuildsDataset

  it('joins on id, scenario and targets together', () => {
    expect(findComputedSpec(dataset, 'a', 'patchwerk', 1)?.targets).toBe(1)
    expect(findComputedSpec(dataset, 'a', 'patchwerk', 5)?.targets).toBe(5)
    // A verdict is about one scenario at one target count, so neither a wrong
    // scenario nor a wrong count may fall back to the other's answer.
    expect(findComputedSpec(dataset, 'a', 'patchwerk', 10)).toBeNull()
    expect(findComputedSpec(dataset, 'a', 'addwaves', 1)).toBeNull()
  })

  it('separates "a search ran" from "nobody looked" from "no such document"', () => {
    expect(computedScope(dataset, 'patchwerk', 1)).toBe('searched')
    expect(computedScope(dataset, 'patchwerk', 10)).toBe('not-searched')
    expect(computedScope(null, 'patchwerk', 1)).toBe('absent')
  })

  it('refuses a schema version it does not know', () => {
    expect(isReadable(dataset)).toBe(true)
    expect(isReadable({ ...dataset, schemaVersion: 2 })).toBe(false)
    expect(isReadable({ ...dataset, schemaVersion: 0 })).toBe(false)
    expect(isReadable({ schemaVersion: 1 } as ComputedBuildsDataset)).toBe(false)
  })
})

describe('against the committed MID2 documents', () => {
  const manifest = JSON.parse(readFileSync(join(DATA, 'index.json'), 'utf8'))
  const computed: ComputedBuildsDataset = JSON.parse(
    readFileSync(join(DATA, 'computed-builds.json'), 'utf8'),
  )

  it('is a dataset this code will read at all', () => {
    expect(isReadable(computed)).toBe(true)
    expect(manifest.specs.length).toBeGreaterThan(0)
  })

  // Properties, not counts. The numbers move every night; these do not.
  it.each([1, 3, 5, 10])('at %i target(s) every row is either untouched or earned its mark', (targets) => {
    let marked = 0
    for (const row of manifest.specs) {
      const published = row.scenarios?.patchwerk?.dps?.[String(targets)]
      if (!Number.isFinite(published)) continue

      const verdict = bestBuildFor(published, findComputedSpec(computed, row.id, 'patchwerk', targets))

      // Never invented and never lost: the manifest's own measurement stays on
      // the row whatever else happens.
      expect(verdict.simcDps).toBe(published)

      if (!verdict.projected) {
        expect(verdict.rankDps).toBe(published)
        expect(bestBuildMark(verdict)).toBeNull()
        continue
      }
      marked += 1
      expect(verdict.gain!).toBeGreaterThan(verdict.noise!)
      expect(verdict.rankDps).toBeGreaterThan(published)
      expect(bestBuildMark(verdict)).toMatch(/^computed \+\d+\.\d\d%$/)
    }
    // A mark may only appear where the document actually covers that cell.
    if (computedScope(computed, 'patchwerk', targets) !== 'searched') {
      expect(marked).toBe(0)
    }
  })

  it('leaves a build the search never covered exactly as published', () => {
    const covered = new Set(computed.specs.map((s) => s.id))
    const absent = manifest.specs.find((r: { id: string }) => !covered.has(r.id))
    // Only meaningful while the search covers less than the whole tier; if it
    // ever covers everything this is vacuous rather than wrong.
    if (!absent) return
    const published = absent.scenarios.patchwerk.dps['1']
    expect(bestBuildFor(published, findComputedSpec(computed, absent.id, 'patchwerk', 1)).rankDps).toBe(published)
  })
})

describe('which margin the ranking uses', () => {
  // Devastation Evoker (Scalecommander), measured 2026-08-26: +2.53% on the gear
  // anchor and +0.02% on simc's own kit. This is the build the projection is wrong
  // about, and it is the case every test below is shaped around.
  const anchored = entry(contender(100, 0.05), contender(102.53, 0.05))

  function withShipped(margin: number, tieBand = 0.001): ComputedSpec {
    return { ...anchored, shipped: { simcDps: 200, bestDps: 200 * (1 + margin), margin, tieBand } } as ComputedSpec
  }

  it('prefers the margin measured on simc own gear over the anchored one', () => {
    const best = bestBuildFor(1000, withShipped(0.005))
    expect(best.marginBasis).toBe('shipped-gear')
    expect(best.gain).toBeCloseTo(0.005, 10)
    // 1000 * 1.005, NOT 1000 * 1.0253 -- the anchored margin must not reach rankDps.
    expect(best.rankDps).toBeCloseTo(1005, 6)
  })

  it('drops the mark when the measured margin does not clear its own band', () => {
    // The real Devastation case: +0.02% on shipped gear is inside any band, so the
    // row must fall back to the published number and carry no mark at all.
    const best = bestBuildFor(1000, withShipped(0.0002, 0.001))
    expect(best.projected).toBe(false)
    expect(best.marginBasis).toBeNull()
    expect(best.rankDps).toBe(1000)
    expect(bestBuildMark(best)).toBeNull()
  })

  it('marks a build the anchored margin would have missed', () => {
    // Devastation Flameshaper runs the other way: +0.21% anchored, +0.90% shipped.
    // A single correction factor cannot serve both, which is why there is none.
    const under = entry(contender(100, 0.05), contender(100.21, 0.05))
    const best = bestBuildFor(1000, { ...under, shipped: { simcDps: 200, bestDps: 201.8, margin: 0.009, tieBand: 0.001 } } as ComputedSpec)
    expect(best.marginBasis).toBe('shipped-gear')
    expect(best.rankDps).toBeCloseTo(1009, 6)
  })

  it('falls back to the anchored margin when no shipped block was measured', () => {
    // Documents written before the pipeline measured it have none. Absent is not a
    // margin of zero, and blinding those rows would discard a real result on the
    // eight builds in nine where the projection is fine.
    const best = bestBuildFor(1000, anchored)
    expect(best.marginBasis).toBe('anchor')
    expect(best.gain).toBeCloseTo(0.0253, 6)
  })

  it('refuses a shipped block with no usable band and falls back rather than guessing', () => {
    // A margin ranked against no bar is exactly what the tie rule exists to prevent,
    // so a half-written block must not be preferred to a whole anchored one.
    for (const broken of [{ margin: 0.02 }, { margin: 0.02, tieBand: -1 }, { margin: Number.NaN, tieBand: 0.001 }]) {
      const best = bestBuildFor(1000, { ...anchored, shipped: { simcDps: 1, bestDps: 1, ...broken } } as ComputedSpec)
      expect(best.marginBasis).toBe('anchor')
    }
  })

  it('never mixes a shipped margin with an anchored band', () => {
    // The band has to come from the same run as the margin it judges. Here the
    // anchored band (0.0014) would clear a 0.2% margin and the shipped one (0.5%)
    // would not, so a mixed pair is visible in the verdict.
    const best = bestBuildFor(1000, withShipped(0.002, 0.005))
    expect(best.projected).toBe(false)
    expect(best.rankDps).toBe(1000)
  })
})
