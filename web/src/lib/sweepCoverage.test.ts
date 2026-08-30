/**
 * What a sweep is allowed to claim about the tier.
 *
 * The bug these pin: `GearView` read both numbers out of the sweep's own
 * document, where `specsAvailable` is the tier's size ON THE DAY THE SWEEP RAN.
 * On 2026-08-26 that made the Loot view say "Covers 28 of 28 builds in the tier"
 * against a tier of 52.
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { displayedCoverage, sweepCoverage } from './sweepCoverage'

describe('sweepCoverage', () => {
  it('does not call a stale sweep complete', () => {
    // The exact MID2 numbers on 2026-08-26: gear covered 28, the sweep saw 28
    // builds when it ran, and the tier now holds 52.
    const c = sweepCoverage(28, 28, 52)
    expect(c.state).toBe('behind')
    expect(c.sentence).not.toBe('Covers all 52 builds in the tier.')
    expect(c.sentence).toContain('28 of 52')
    // The reason matters: nothing is wrong with the run, it is older than the tier.
    expect(c.sentence).toContain('when it ran')
    expect(c.sentence).toContain('24 builds have been added since')
  })

  it('says complete only when the sweep covers the tier as it stands now', () => {
    const c = sweepCoverage(52, 52, 52)
    expect(c.state).toBe('complete')
    expect(c.sentence).toBe('Covers all 52 builds in the tier.')
  })

  it('separates a sweep that stopped early from one that is merely behind', () => {
    // Covered 20 of the 28 that existed at the time: the gap is the run's.
    const partial = sweepCoverage(20, 28, 52)
    expect(partial.state).toBe('partial')
    expect(partial.sentence).toContain('32 builds are missing')
    expect(partial.sentence).not.toContain('when it ran')
  })

  it('refuses to claim intent for a document with no coverage block', () => {
    // buffs.json carries none, so `covered` is a ROW COUNT -- and an interrupted
    // run and a complete one look identical from a row count.
    const c = sweepCoverage(28, null, 52)
    expect(c.statesIntent).toBe(false)
    expect(c.state).toBe('partial')
    expect(c.sentence).toContain('states no coverage')
    expect(c.sentence).not.toContain('when it ran')
  })

  it('makes no claim about the tier when the manifest is unknown', () => {
    const c = sweepCoverage(28, 28, null)
    expect(c.state).toBe('unknown')
    expect(c.sentence).not.toContain('in the tier')
  })

  it('counts one build in the singular', () => {
    expect(sweepCoverage(1, 1, 1).sentence).toBe('Covers all 1 build in the tier.')
    expect(sweepCoverage(1, 1, 2).sentence).toContain('1 build has been added since')
  })

  it('treats a sweep ahead of the manifest as complete rather than negative', () => {
    // A sweep can hold a build the manifest dropped. That must not render as
    // "-1 builds missing".
    const c = sweepCoverage(30, 30, 28)
    expect(c.state).toBe('complete')
    expect(c.sentence).not.toContain('-')
  })
})

describe('against the committed MID2 documents', () => {
  // The corpus half, same bargain as bestBuild.test.ts: assert PROPERTIES, not
  // frozen counts, so a nightly re-sim does not turn this red for the wrong reason.
  const read = (name: string) =>
    JSON.parse(
      readFileSync(
        join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'public', 'data', 'MID2', name),
        'utf8',
      ),
    )

  const manifest = read('index.json')
  const tierBuilds: number = manifest.specs.length

  it('never tells a reader a sweep is complete when builds are missing from it', () => {
    const gear = read('gear.json')
    const buffs = read('buffs.json')

    const gearC = sweepCoverage(gear.coverage.specs, gear.coverage.specsAvailable, tierBuilds)
    const buffsC = sweepCoverage(buffs.specs.length, buffs.coverage?.specsAvailable ?? null, tierBuilds)

    for (const [name, c, covered] of [
      ['gear', gearC, gear.coverage.specs],
      ['buffs', buffsC, buffs.specs.length],
    ] as const) {
      if (covered < tierBuilds) {
        expect(c.state, `${name} covers ${covered} of ${tierBuilds}`).not.toBe('complete')
        // The gap has to be IN the sentence, not merely in the state.
        expect(c.sentence).toContain(String(tierBuilds))
      } else {
        expect(c.state).toBe('complete')
      }
    }
  })

  it('makes the SAME call the Buffs view makes, and it is complete today', () => {
    // This file used to read `buffs.coverage?.specsAvailable` while BuffsView passed
    // a hard `null` -- so the test asserted a call no view made, which is why the
    // null path never showed up as wrong. Both read the field now.
    const buffs = read('buffs.json')
    const c = sweepCoverage(buffs.specs.length, buffs.coverage?.specsAvailable, tierBuilds)
    expect(buffs.coverage, 'buffs.json states its coverage; the view must read it').toBeDefined()
    expect(c.statesIntent).toBe(true)
    if (buffs.specs.length >= tierBuilds) {
      expect(c.state).toBe('complete')
    }
  })

  it('would have said something false BEFORE the sweep was widened, which is the order', () => {
    // The shape of the data as it stood at 17:30 on 2026-08-26: the sweep found 31
    // builds because `buffs.yml` never materialised the unvalidated and extra
    // profiles, and published `specs == specsAvailable`, which a reader takes as
    // "covered everything there was".
    //
    // Wiring the view to the field WITHOUT fixing the sweep first turns a vague
    // sentence into a confident wrong one. That is why #88 came before #89, and
    // this pins it so the two cannot be reordered by a later refactor.
    const behind = sweepCoverage(31, 31, 52)
    expect(behind.state).toBe('behind')
    expect(behind.sentence).toContain('have been added since')

    // And the honest reading of the same numbers, had the field not lied.
    const honest = sweepCoverage(31, 52, 52)
    expect(honest.state).not.toBe('behind')
    expect(honest.sentence).not.toContain('have been added since')
  })

  it('both views actually pass the field, which no behavioural test here can see', () => {
    // A source-level assertion, deliberately, and it is the ONLY thing guarding the
    // regression this pair of issues was about. Everything else in this file tests
    // `sweepCoverage`, which was never wrong -- the defect was a CALLER passing a
    // hard `null`, and there are no component tests in this project to catch that.
    // Reverting either view to `null` leaves every other test in this file green.
    //
    // Brittle to a refactor, and that is the accepted cost: a rename makes this fail
    // loudly, where the thing it replaces failed silently for as long as nobody
    // opened the file.
    const view = (name: string) =>
      readFileSync(
        join(dirname(fileURLToPath(import.meta.url)), '..', 'views', name),
        'utf8',
      )

    for (const name of ['BuffsView.tsx', 'GearView.tsx']) {
      // `=> sweepCoverage(` and not `sweepCoverage(`: the comment above the call in
      // BuffsView quotes `sweepCoverage(31, 31, 52)` as the sentence it must not
      // produce, and a looser pattern matches the PROSE before the code. Caught by
      // this test failing on its first run against a file that was already correct.
      const call = view(name).match(/=>\s*sweepCoverage\([\s\S]{0,200}?\)/)
      expect(call, `${name} calls sweepCoverage`).not.toBeNull()
      expect(call![0], `${name} must not hard-code the second argument`).toMatch(
        /specsAvailable/,
      )
    }

    // Same guard, one layer up, for the #95 half: GearView must route the pair
    // through `displayedCoverage` so the DISPLAYED SLOT's block wins over the
    // document union. Reverting to the direct document read passes every
    // behavioural test here (the mutation that confirmed it left all green),
    // because the pre-fix call also contains `specsAvailable`.
    expect(
      view('GearView.tsx'),
      'GearView shows the displayed slot through displayedCoverage',
    ).toMatch(/displayedCoverage\(/)
  })

  it('prefers the displayed slot’s own coverage and falls back to the document (#95)', () => {
    const document = { coverage: { specs: 28, specsAvailable: 28 } }
    const slot = { coverage: { specs: 26, specsAvailable: 52 } }
    // A swept slot carries the run that measured IT; the union must not win.
    expect(displayedCoverage(slot, document)).toEqual({ specs: 26, specsAvailable: 52 })
    // A slot from before the per-slot block existed falls back to the document —
    // the old, weaker claim, never an invented one.
    expect(displayedCoverage({}, document)).toEqual({ specs: 28, specsAvailable: 28 })
    expect(displayedCoverage(null, document)).toEqual({ specs: 28, specsAvailable: 28 })
    expect(displayedCoverage(null, null)).toBeNull()
  })

  it('reproduces the sentence the old code got wrong', () => {
    // The shipped bug, stated as a test: reading both numbers out of gear.json
    // yields "N of N", which is what the Loot view printed.
    const gear = read('gear.json')
    const selfReferential = `${gear.coverage.specs} of ${gear.coverage.specsAvailable}`
    const honest = sweepCoverage(gear.coverage.specs, gear.coverage.specsAvailable, tierBuilds)
    if (gear.coverage.specs < tierBuilds) {
      expect(honest.sentence).not.toContain(selfReferential)
    }
  })
})
