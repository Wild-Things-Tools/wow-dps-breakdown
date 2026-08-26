/**
 * How much of the tier a sweep actually covers, said honestly.
 *
 * ## The bug this exists for
 *
 * `GearView` printed `Covers {coverage.specs} of {coverage.specsAvailable} builds
 * in the tier`. Both numbers come out of the sweep's own document, and
 * `specsAvailable` is **the size of the tier on the day the sweep ran** -- so on
 * 2026-08-26 the Loot view read *"Covers 28 of 28 builds in the tier"* while the
 * tier held **52**. The sentence is true about the run and false about the tier,
 * and it is the tier it claims to describe. Measured that day against the
 * committed MID2 data: gear covered 28 rings / 26 necks / 26 trinkets and buffs
 * covered 28, against 52 builds in `index.json`.
 *
 * `BuffsView` said nothing at all, which is the same gap without the false
 * sentence: 24 of the tier's builds were simply absent and no one was told.
 *
 * ## Three states, because two would lie
 *
 * A sweep that is behind and a sweep that was always partial are different
 * claims, and so is a sweep that is genuinely complete:
 *
 * - `complete` -- it covers every build the tier has **now**.
 * - `behind` -- it covered everything that existed when it ran, and the tier has
 *   grown since. Nothing is wrong with the sweep; it is simply older than the
 *   tier. This is MID2's state today.
 * - `partial` -- it did not even cover the tier as it stood at the time, which is
 *   what an interrupted shard looks like.
 *
 * ## What `availableAtSweep: null` means, and why it is not zero
 *
 * `buffs.json` carries **no coverage block at all**, so the only number available
 * is how many rows the document holds. That is a statement about the *document*,
 * not about what the sweep set out to do -- an interrupted run and a complete one
 * look identical from a row count, which is exactly why this project's rule says
 * never to read "N of M" off an array length. So when `availableAtSweep` is null
 * the sentence says how many builds the file *holds* and explicitly declines to
 * say whether the sweep meant to do more. `statesIntent` carries that distinction
 * to callers rather than burying it in prose.
 */

export type SweepState = 'complete' | 'behind' | 'partial' | 'unknown'

export interface SweepCoverage {
  /** Builds this sweep has a row for. */
  covered: number
  /** Builds the tier holds right now, from the manifest. Null when unknown. */
  tierNow: number | null
  state: SweepState
  /**
   * False when the document carries no coverage block, so `covered` is a row
   * count and cannot distinguish "the sweep was complete" from "it stopped".
   */
  statesIntent: boolean
  /** The whole claim, ready to render. */
  sentence: string
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

export function sweepCoverage(
  covered: number,
  availableAtSweep: number | null | undefined,
  tierNow: number | null | undefined,
): SweepCoverage {
  const now = typeof tierNow === 'number' && tierNow > 0 ? tierNow : null
  const atSweep =
    typeof availableAtSweep === 'number' && availableAtSweep >= 0 ? availableAtSweep : null
  const statesIntent = atSweep !== null

  // Without the manifest there is nothing to compare against, so the honest
  // answer is the sweep's own two numbers and no claim about the tier today.
  if (now === null) {
    return {
      covered,
      tierNow: null,
      state: 'unknown',
      statesIntent,
      sentence: statesIntent
        ? `Covers ${covered} of ${plural(atSweep as number, 'build', 'builds')} the sweep found.`
        : `Holds ${plural(covered, 'build', 'builds')}.`,
    }
  }

  if (covered >= now) {
    return {
      covered,
      tierNow: now,
      state: 'complete',
      statesIntent,
      sentence: `Covers all ${plural(now, 'build', 'builds')} in the tier.`,
    }
  }

  const missing = now - covered
  // Behind: the sweep was complete for the tier it ran against. Partial: it was
  // not, so the gap is the run's rather than the calendar's.
  // `covered >= atSweep` is the test, NOT `atSweep >= covered`: "behind" means the
  // sweep covered everything IT SAW and the tier grew afterwards. The inverted form
  // called a run that covered 20 of the 28 then present "behind", which blames the
  // calendar for a gap that is the run's -- caught by its own test.
  const behind = statesIntent && covered >= (atSweep as number) && (atSweep as number) < now
  if (behind) {
    return {
      covered,
      tierNow: now,
      state: 'behind',
      statesIntent,
      sentence:
        `Covers ${covered} of ${plural(now, 'build', 'builds')} in the tier. ` +
        `The sweep covered every build the tier had when it ran; ` +
        `${plural(missing, 'build has', 'builds have')} been added since.`,
    }
  }

  if (!statesIntent) {
    return {
      covered,
      tierNow: now,
      state: 'partial',
      statesIntent,
      sentence:
        `Holds ${covered} of ${plural(now, 'build', 'builds')} in the tier. ` +
        `This file states no coverage, so whether the sweep meant to cover more is not recorded.`,
    }
  }

  return {
    covered,
    tierNow: now,
    state: 'partial',
    statesIntent,
    sentence:
      `Covers ${covered} of ${plural(now, 'build', 'builds')} in the tier, ` +
      `and ${plural(missing, 'build is', 'builds are')} missing.`,
  }
}
