/**
 * Spec selection for the comparison views.
 *
 * Grouped by class, with hero-talent builds listed as siblings so the difference
 * between (say) Frostfire and Sunfury Fire Mage is one click apart. Selection is
 * capped at six because that is where a multi-line chart stops being readable.
 */

import { useMemo, useState } from 'react'
import { classColor } from '../lib/palette'
import type { SpecSummary } from '../lib/types'
import { Button, Dot, cx } from './ui'

export function SpecPicker({
  specs,
  selected,
  onToggle,
  onClear,
  colorOf,
  max,
}: {
  specs: SpecSummary[]
  selected: string[]
  onToggle: (id: string) => void
  onClear: () => void
  colorOf: (id: string) => string
  max: number
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const matching = needle
      ? specs.filter((spec) => spec.displayName.toLowerCase().includes(needle))
      : specs

    const byClass = new Map<string, SpecSummary[]>()
    for (const spec of matching) {
      const bucket = byClass.get(spec.class)
      if (bucket) bucket.push(spec)
      else byClass.set(spec.class, [spec])
    }
    return [...byClass.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [specs, query])

  const atCapacity = selected.length >= max

  return (
    <div className="rounded-xl border border-hairline bg-surface">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <span className="text-[13px] font-medium text-ink">Comparing</span>

        {selected.length === 0 ? (
          <span className="text-[13px] text-ink-muted">nothing yet — pick up to {max} builds</span>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {selected.map((id) => {
              const spec = specs.find((entry) => entry.id === id)
              if (!spec) return null
              return (
                <li key={id}>
                  <button
                    type="button"
                    onClick={() => onToggle(id)}
                    title="Remove from comparison"
                    className="flex items-center gap-1.5 rounded-full border border-hairline py-1 pr-2 pl-2.5 text-[12.5px] text-ink hover:bg-elevated"
                  >
                    <Dot color={colorOf(id)} />
                    {spec.displayName}
                    <span aria-hidden className="text-ink-muted">
                      ×
                    </span>
                    <span className="sr-only">Remove {spec.displayName}</span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}

        <div className="ml-auto flex items-center gap-2">
          {selected.length > 0 ? <Button onClick={onClear}>Clear</Button> : null}
          <Button onClick={() => setOpen((value) => !value)} active={open}>
            {open ? 'Done' : 'Add builds'}
          </Button>
        </div>
      </div>

      {open ? (
        <div className="border-t border-hairline px-4 py-3">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by class, spec or hero talent…"
            aria-label="Filter builds"
            className="w-full rounded-lg border border-hairline bg-page px-3 py-2 text-[13px] text-ink placeholder:text-ink-muted"
          />

          {atCapacity ? (
            <p className="mt-2 text-[12.5px] text-ink-muted">
              Six builds is the limit — remove one to add another.
            </p>
          ) : null}

          <div className="mt-3 max-h-80 space-y-4 overflow-y-auto">
            {grouped.length === 0 ? (
              <p className="py-6 text-center text-[13px] text-ink-muted">No builds match.</p>
            ) : (
              grouped.map(([wowClass, entries]) => (
                <div key={wowClass}>
                  <h3 className="flex items-center gap-1.5 text-[12px] font-semibold tracking-wide text-ink-secondary uppercase">
                    <Dot color={classColor(wowClass)} ring />
                    {wowClass}
                  </h3>
                  <ul className="mt-1.5 flex flex-wrap gap-1.5">
                    {entries.map((spec) => {
                      const isSelected = selected.includes(spec.id)
                      return (
                        <li key={spec.id}>
                          <button
                            type="button"
                            onClick={() => onToggle(spec.id)}
                            disabled={!isSelected && atCapacity}
                            aria-pressed={isSelected}
                            className={cx(
                              'rounded-lg border px-2.5 py-1.5 text-left text-[12.5px] transition-colors',
                              'disabled:cursor-not-allowed disabled:opacity-40',
                              isSelected
                                ? 'border-transparent bg-ink text-surface'
                                : 'border-hairline text-ink-secondary hover:bg-elevated hover:text-ink',
                            )}
                          >
                            {spec.spec}
                            <span
                              className={cx(
                                'ml-1.5',
                                isSelected ? 'opacity-70' : 'text-ink-muted',
                              )}
                            >
                              {spec.heroTalent}
                            </span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}
