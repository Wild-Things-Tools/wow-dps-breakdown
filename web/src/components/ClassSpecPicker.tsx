/**
 * Every class and every spec in the game, as one picker.
 *
 * The shape is not decoration. Per class, the specs sit on the vertices of a
 * polygon — a triangle for three, a square for Druid's four — and each hero tree
 * sits on the **edge between the two specs that share it**. That is not a layout
 * choice imposed on the data; it is what the data is. simc's trait table says each
 * hero tree is available to exactly two specs of its class, and each spec to
 * exactly two trees, so the specs and trees of a class form a cycle. Drawing them
 * any other way would hide the one structural fact a reader most needs when
 * choosing between builds.
 *
 * Four states, and every one of them is derived rather than asserted:
 *
 * - **selectable** — simc ships a profile for this tier and the dataset has builds.
 * - **no profile this season** — simc has shipped one before and has not yet for
 *   this tier. Lightly dimmed, because it is expected to come back.
 * - **not simulated** — no profile in any tier. Tanks land here, and so do healers:
 *   role comes from a profile's `role=` line and simc ships no healing profiles at
 *   all, so "healer" is something this project cannot prove and does not claim. The
 *   tooltip says which of the two it is.
 * - **tank** — the one non-damage role simc does profile, so it is named.
 *
 * Class colour is the identity channel here as everywhere, and it is redundant as
 * always: the class name is written, the spec name is written, and each node
 * carries its own icon.
 */
import { useMemo } from 'react'
import type { SpecIndex, SpecIndexClass, SpecIndexSpec } from '../lib/types'
import { classColor, classWash } from '../lib/palette'
import { classIconUrl, heroTreeIconUrl, specIconUrl } from '../lib/gameIcons'
import { EntityIcon } from './BuildIdentity'
import { cx } from './ui'

/** Where a class's shape is drawn, in its own square viewport. */
const BOX = 210
const CENTRE = BOX / 2
const SPEC_RADIUS = 66
const NODE = 44
const TREE_NODE = 26

/** `Death Knight` + `Frost` -> `death_knight_frost`, the pipeline's own spec id. */
function pipelineSpecId(wowClass: string, spec: string): string {
  return `${wowClass} ${spec}`.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')
}

export type PickerSelection =
  | { kind: 'spec'; specId: number; buildIds: string[] }
  | { kind: 'hero'; subTree: number; name: string | null; buildIds: string[] }

/**
 * Vertices of a regular polygon, first one at the top.
 *
 * Three specs give a triangle and four a square, which is the whole of what the
 * game needs today; the formula covers any count so a class gaining a spec needs
 * no edit here.
 */
function vertices(count: number, radius: number): { x: number; y: number }[] {
  return Array.from({ length: count }, (_, index) => {
    const angle = (index / count) * Math.PI * 2 - Math.PI / 2
    return { x: CENTRE + radius * Math.cos(angle), y: CENTRE + radius * Math.sin(angle) }
  })
}

function specState(spec: SpecIndexSpec): {
  state: 'selectable' | 'absent' | 'never'
  reason: string
} {
  if (spec.role === 'tank') {
    return { state: 'never', reason: 'Tank specialisation — this site compares damage only.' }
  }
  if (!spec.profiledEver) {
    return {
      state: 'never',
      reason:
        'SimulationCraft ships no profile for this spec in any season. Healing specs are ' +
        'not simulated at all, which is why they appear here without a role.',
    }
  }
  if (!spec.profiled || spec.builds.length === 0) {
    return {
      state: 'absent',
      reason:
        'SimulationCraft has shipped a profile for this spec before but not yet for this ' +
        'season, so there is nothing to show — not a poor result.',
    }
  }
  return { state: 'selectable', reason: '' }
}

export function ClassSpecPicker({
  index,
  selectedSpecId,
  selectedSubTree,
  onSelect,
}: {
  index: SpecIndex
  selectedSpecId?: number
  selectedSubTree?: number
  onSelect: (selection: PickerSelection) => void
}) {
  const treesById = useMemo(
    () => new Map(index.heroTrees.map((tree) => [tree.subTree, tree])),
    [index.heroTrees],
  )
  const buildsBySpec = useMemo(() => {
    const map = new Map<number, string[]>()
    for (const entry of index.classes) {
      for (const spec of entry.specs) map.set(spec.specId, spec.builds)
    }
    return map
  }, [index.classes])

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {index.classes.map((entry) => (
        <ClassShape
          key={entry.class}
          entry={entry}
          treesById={treesById}
          buildsBySpec={buildsBySpec}
          selectedSpecId={selectedSpecId}
          selectedSubTree={selectedSubTree}
          onSelect={onSelect}
        />
      ))}
    </div>
  )
}

function ClassShape({
  entry,
  treesById,
  buildsBySpec,
  selectedSpecId,
  selectedSubTree,
  onSelect,
}: {
  entry: SpecIndexClass
  treesById: Map<number, { subTree: number; specIds: number[]; name: string | null }>
  buildsBySpec: Map<number, string[]>
  selectedSpecId?: number
  selectedSubTree?: number
  onSelect: (selection: PickerSelection) => void
}) {
  const color = classColor(entry.class)
  const points = vertices(entry.specs.length, SPEC_RADIUS)
  const positionOf = new Map(entry.specs.map((spec, index) => [spec.specId, points[index]]))

  // A tree belongs between the two specs that can play it. One whose partner spec
  // is not in this class's list (which the data has never shown, but which would
  // otherwise place a node at NaN) is skipped rather than drawn somewhere wrong.
  const trees = entry.specs
    .flatMap((spec) => spec.subTrees)
    .filter((subTree, index, all) => all.indexOf(subTree) === index)
    .map((subTree) => treesById.get(subTree))
    .filter((tree): tree is NonNullable<typeof tree> => Boolean(tree))
    .map((tree) => {
      const ends = tree.specIds.map((id) => positionOf.get(id)).filter(Boolean) as {
        x: number
        y: number
      }[]
      if (ends.length !== 2 || !ends[0] || !ends[1]) return null
      const owners = tree.specIds
        .map((id) => entry.specs.find((spec) => spec.specId === id))
        .filter((spec): spec is SpecIndexSpec => Boolean(spec))
      const buildIds = owners.flatMap((spec) => buildsBySpec.get(spec.specId) ?? [])
      return {
        tree,
        buildIds,
        playable: owners.some((spec) => specState(spec).state === 'selectable'),
        x: (ends[0]!.x + ends[1]!.x) / 2,
        y: (ends[0]!.y + ends[1]!.y) / 2,
      }
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item))

  const anySelectable = entry.specs.some((spec) => specState(spec).state === 'selectable')

  return (
    <section
      className={cx(
        'rounded-lg border border-hairline bg-surface p-3',
        !anySelectable && 'opacity-70',
      )}
    >
      <h3
        className="mb-1 flex items-center gap-2 text-[12.5px] font-semibold"
        style={{ color }}
      >
        <EntityIcon
          url={classIconUrl(entry.class)}
          name={entry.class}
          color={color}
          wash={classWash(entry.class)}
          size={16}
          labelled
        />
        {entry.class}
      </h3>

      <div className="relative mx-auto" style={{ width: BOX, height: BOX }}>
        {/* The edges, drawn behind everything: they are what says "these two specs
            share this tree", and without them the tree nodes read as floating. */}
        <svg
          className="absolute inset-0"
          width={BOX}
          height={BOX}
          aria-hidden="true"
          focusable="false"
        >
          {points.map((from, index) => {
            const to = points[(index + 1) % points.length]!
            return (
              <line
                key={index}
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                stroke={color}
                strokeOpacity={0.25}
                strokeWidth={1.5}
              />
            )
          })}
          {entry.specs.length === 4 ? (
            // A square's diagonals carry the two trees whose specs sit opposite.
            <>
              <line
                x1={points[0]!.x}
                y1={points[0]!.y}
                x2={points[2]!.x}
                y2={points[2]!.y}
                stroke={color}
                strokeOpacity={0.15}
                strokeWidth={1.5}
              />
              <line
                x1={points[1]!.x}
                y1={points[1]!.y}
                x2={points[3]!.x}
                y2={points[3]!.y}
                stroke={color}
                strokeOpacity={0.15}
                strokeWidth={1.5}
              />
            </>
          ) : null}
        </svg>

        {entry.specs.map((spec, index) => (
          <SpecNode
            key={spec.specId}
            spec={spec}
            color={color}
            at={points[index]!}
            selected={spec.specId === selectedSpecId}
            onSelect={onSelect}
          />
        ))}

        {trees.map((item) => (
          <TreeNode
            key={item.tree.subTree}
            subTree={item.tree.subTree}
            name={item.tree.name}
            wowClass={entry.class}
            color={color}
            at={item}
            playable={item.playable}
            buildIds={item.buildIds}
            selected={item.tree.subTree === selectedSubTree}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  )
}

function SpecNode({
  spec,
  color,
  at,
  selected,
  onSelect,
}: {
  spec: SpecIndexSpec
  color: string
  at: { x: number; y: number }
  selected: boolean
  onSelect: (selection: PickerSelection) => void
}) {
  const { state, reason } = specState(spec)
  const disabled = state !== 'selectable'
  const title = disabled ? `${spec.name} ${spec.class} — ${reason}` : `${spec.name} ${spec.class}`

  return (
    <button
      type="button"
      disabled={disabled}
      title={title}
      aria-label={title}
      aria-pressed={selected}
      onClick={() => onSelect({ kind: 'spec', specId: spec.specId, buildIds: spec.builds })}
      className={cx(
        'absolute flex flex-col items-center gap-0.5 rounded-md p-0.5 transition',
        disabled ? 'cursor-not-allowed' : 'cursor-pointer hover:bg-elevated',
        selected && 'ring-2',
      )}
      style={{
        left: at.x - NODE / 2,
        top: at.y - NODE / 2 - 6,
        // Two dimming levels, and the difference is the whole point: "not this
        // season" is expected back, "never simulated" is not.
        opacity: state === 'never' ? 0.28 : state === 'absent' ? 0.55 : 1,
        ...(selected ? { boxShadow: `0 0 0 2px ${color}` } : null),
      }}
    >
      <EntityIcon
        url={specIconUrl(pipelineSpecId(spec.class, spec.name)) ?? classIconUrl(spec.class)}
        name={spec.name}
        color={color}
        wash={classWash(spec.class)}
        size={NODE - 16}
        labelled
      />
      <span className="max-w-[74px] text-center text-[9.5px] leading-tight text-ink-secondary">
        {spec.name}
      </span>
    </button>
  )
}

function TreeNode({
  subTree,
  name,
  wowClass,
  color,
  at,
  playable,
  buildIds,
  selected,
  onSelect,
}: {
  subTree: number
  name: string | null
  wowClass: string
  color: string
  at: { x: number; y: number }
  playable: boolean
  buildIds: string[]
  selected: boolean
  onSelect: (selection: PickerSelection) => void
}) {
  // A tree simc never named is one no build plays, which is also one that cannot
  // be selected — the two coincide, so an unnamed node is always an inert marker.
  const label = name ?? 'Hero tree (unnamed in simc data)'
  const disabled = !playable || !name

  return (
    <button
      type="button"
      disabled={disabled}
      title={label}
      aria-label={label}
      aria-pressed={selected}
      onClick={() => onSelect({ kind: 'hero', subTree, name, buildIds })}
      className={cx(
        'absolute flex items-center justify-center rounded-full border transition',
        disabled ? 'cursor-not-allowed' : 'cursor-pointer hover:brightness-125',
      )}
      style={{
        left: at.x - TREE_NODE / 2,
        top: at.y - TREE_NODE / 2,
        width: TREE_NODE,
        height: TREE_NODE,
        borderColor: selected ? color : 'var(--hairline)',
        borderWidth: selected ? 2 : 1,
        background: 'var(--surface-1)',
        opacity: disabled ? 0.3 : 1,
      }}
    >
      <EntityIcon
        url={name ? heroTreeIconUrl(name) : null}
        name={name ?? '?'}
        color={color}
        wash={classWash(wowClass)}
        size={TREE_NODE - 6}
        labelled
        round
      />
    </button>
  )
}
