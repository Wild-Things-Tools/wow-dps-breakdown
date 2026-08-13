/**
 * Small presentational primitives.
 *
 * Deliberately hand-written rather than pulled from a component library: there
 * are only a handful of shapes here, and keeping them local means the whole
 * surface is readable and editable in place. `components.json` is configured so
 * `npx shadcn@latest add <component>` can drop richer primitives alongside these
 * when a real dialog or combobox is needed.
 */

import type { ReactNode } from 'react'

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

export function Panel({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={cx(
        'rounded-xl border border-hairline bg-surface',
        'shadow-[0_1px_2px_rgb(0_0_0/0.04)]',
        className,
      )}
    >
      {children}
    </section>
  )
}

export function PanelHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline px-5 py-4">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h2>
        {subtitle ? (
          <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-ink-secondary">
            {subtitle}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  )
}

export function Button({
  children,
  onClick,
  active,
  title,
  disabled,
}: {
  children: ReactNode
  onClick?: () => void
  active?: boolean
  title?: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      disabled={disabled}
      aria-pressed={active}
      className={cx(
        'rounded-lg border px-3 py-1.5 text-[13px] font-medium transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-40',
        active
          ? 'border-transparent bg-ink text-surface'
          : 'border-hairline bg-surface text-ink-secondary hover:bg-elevated hover:text-ink',
      )}
    >
      {children}
    </button>
  )
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: Array<{ value: T; label: string; title?: string }>
  value: T
  onChange: (value: T) => void
  label: string
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex rounded-lg border border-hairline bg-surface p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          title={option.title}
          onClick={() => onChange(option.value)}
          className={cx(
            'rounded-[6px] px-3 py-1.5 text-[13px] font-medium transition-colors',
            value === option.value
              ? 'bg-ink text-surface'
              : 'text-ink-secondary hover:text-ink',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Select<T extends string | number>({
  value,
  onChange,
  options,
  label,
}: {
  value: T
  onChange: (value: T) => void
  options: Array<{ value: T; label: string }>
  label: string
}) {
  return (
    <label className="inline-flex items-center gap-2 text-[13px] text-ink-secondary">
      <span>{label}</span>
      <select
        value={String(value)}
        onChange={(event) => {
          const raw = event.target.value
          const match = options.find((option) => String(option.value) === raw)
          if (match) onChange(match.value)
        }}
        className="rounded-lg border border-hairline bg-surface px-2.5 py-1.5 text-[13px] font-medium text-ink"
      >
        {options.map((option) => (
          <option key={String(option.value)} value={String(option.value)}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

export function Dot({ color, ring }: { color: string; ring?: boolean }) {
  return (
    <span
      aria-hidden
      className={cx('inline-block size-2.5 shrink-0 rounded-full', ring && 'ring-1 ring-hairline')}
      style={{ background: color }}
    />
  )
}

/**
 * Legend for a multi-series chart. Always rendered when two or more series are
 * present, so identity never depends on colour alone.
 */
export function Legend({
  items,
}: {
  items: Array<{ id: string; label: string; color: string }>
}) {
  if (items.length < 2) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1.5 px-5 pb-4">
      {items.map((item) => (
        <li key={item.id} className="flex items-center gap-1.5 text-[12.5px] text-ink-secondary">
          <Dot color={item.color} />
          <span>{item.label}</span>
        </li>
      ))}
    </ul>
  )
}

export function Note({ children }: { children: ReactNode }) {
  return (
    <p className="px-5 pb-4 text-[12.5px] leading-relaxed text-ink-muted">{children}</p>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-52 items-center justify-center px-6 py-12 text-center text-[13.5px] text-ink-muted">
      <p className="max-w-md leading-relaxed">{children}</p>
    </div>
  )
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="flex min-h-52 items-center justify-center gap-3 text-[13.5px] text-ink-muted">
      <span
        aria-hidden
        className="size-4 animate-spin rounded-full border-2 border-hairline border-t-ink-secondary"
      />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex min-h-52 flex-col items-center justify-center gap-4 px-6 py-12 text-center">
      <p className="max-w-md text-[13.5px] leading-relaxed text-ink-secondary">{error}</p>
      {onRetry ? <Button onClick={onRetry}>Try again</Button> : null}
    </div>
  )
}

export function StatTile({
  label,
  value,
  caption,
  accent,
}: {
  label: string
  value: ReactNode
  caption?: ReactNode
  accent?: string
}) {
  return (
    <div className="rounded-xl border border-hairline bg-surface px-4 py-3.5">
      <div className="flex items-center gap-1.5">
        {accent ? <Dot color={accent} /> : null}
        <span className="text-[11.5px] font-medium tracking-wide text-ink-muted uppercase">
          {label}
        </span>
      </div>
      <div className="mt-1.5 text-2xl leading-none font-semibold text-ink">{value}</div>
      {caption ? (
        <div className="mt-1.5 text-[12.5px] leading-snug text-ink-secondary">{caption}</div>
      ) : null}
    </div>
  )
}
