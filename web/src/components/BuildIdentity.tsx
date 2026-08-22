/**
 * How a build is named on screen: icon, class colour, written-out name.
 *
 * Everything that draws a spec, a class or a hero-talent tree goes through here,
 * so the three rules the site now runs on are enforced in one place rather than
 * re-decided per view:
 *
 * 1. **Colour is the class colour** -- and it is never the only identity, because
 *    thirteen class colours cannot be told apart under colour-blindness (see
 *    lib/palette.ts for the measurements).
 * 2. **Every icon carries an accessible name.** An icon standing alone gets a
 *    real `alt`; an icon sitting beside its own written name gets `alt=""` and a
 *    `title`, so a screen reader hears the name once rather than twice. No
 *    control and no row is ever identified by an image alone.
 * 3. **A hero tree is icon *plus* written name, always.** Class and spec icons
 *    are recognisable enough to stand on their own where space is tight; hero
 *    tree emblems are new and are not, so the name goes with them everywhere.
 *
 * If the icon CDN is blocked or offline, every icon leaves behind the
 * class-coloured tile it was drawn over, with the entity's initials in it. The
 * layout does not move and no meaning is lost.
 */

import { type CSSProperties, type ReactNode } from "react";
import {
  NO_HERO_TREE,
  classIconUrl,
  heroTreeIconUrl,
  iconInitials,
  specIconUrl,
} from "../lib/gameIcons";
import { classColor, classWash } from "../lib/palette";
import { cx } from "./ui";

/**
 * The fields identity needs, and no more.
 *
 * Structural on purpose: `SpecSummary`, `SpecDetail` and `GearSpecResult` all
 * satisfy it, so a view hands over whichever one it already has.
 */
export interface BuildLike {
  id: string;
  class: string;
  spec: string;
  specId: string;
  heroTalent: string;
  displayName: string;
  /**
   * Set when the build came from a profile simc wrote and left commented out. The
   * number is a real simulation of a character simc's authors have not signed off,
   * which is weaker evidence than the rest of the page rather than absent from it
   * -- so it travels with the identity and is drawn wherever the identity is.
   */
  unvalidated?: boolean;
  /**
   * False when the build's gear could not be matched to the tier's. Separate from
   * `unvalidated` because they are different claims: one says simc has not signed the
   * profile off, the other says this particular number cannot be ranked against the
   * ones beside it. A build can be either without being the other.
   */
  gearComparable?: boolean;
}

/** "Frost Death Knight" -- the spec, without the hero tree in brackets. */
export function buildName(build: Pick<BuildLike, "spec" | "class">): string {
  return `${build.spec} ${build.class}`;
}

/**
 * A hero tree's name in running prose.
 *
 * simc writes `Default` where a profile names no hero-talent tree. That is not a
 * tree, so it must not appear in a sentence as if it were the name of one.
 *
 * It used to read "the single build", which is true only some of the time and
 * visibly false the rest: MID2 Frost Death Knight ships `Default` *and* Rider of
 * the Apocalypse, so the Builds view was writing "tie — the single build and Rider
 * of the Apocalypse" about a spec with two builds on screen. What `Default` always
 * means is that this profile names no tree, so that is what it says now.
 */
export function heroLabel(heroTalent: string): string {
  return heroTalent === NO_HERO_TREE
    ? "the build with no hero tree"
    : heroTalent;
}

/** "Frost Death Knight · Rider of the Apocalypse", for titles and alt text. */
export function fullBuildName(
  build: Pick<BuildLike, "spec" | "class" | "heroTalent">,
): string {
  return build.heroTalent === NO_HERO_TREE
    ? buildName(build)
    : `${buildName(build)} · ${build.heroTalent}`;
}

// --------------------------------------------------------------------------------
// Icons
// --------------------------------------------------------------------------------

/**
 * One icon over a coloured, lettered tile.
 *
 * The tile is not a placeholder that gets replaced -- it is drawn first and the
 * image sits on top of it, so a failed load degrades to a coloured, lettered
 * mark instead of a broken-image glyph. That is what lets the site stay usable
 * with the icon CDN blocked, and it is the reason every icon on the site goes
 * through this one component rather than being an `<img>` where it is needed.
 *
 * Exported because not everything with an icon is a build. A boss has a portrait
 * and no class, so it passes its own colour; everything else passes the class
 * colour and gets exactly what it got before.
 */
export function EntityIcon({
  url,
  name,
  color,
  wash,
  size,
  labelled,
  round,
}: {
  url: string | null;
  name: string;
  color: string;
  wash: string;
  size: number;
  /** True when the name is written out beside this icon. */
  labelled: boolean;
  round?: boolean;
}) {
  const style: CSSProperties = {
    width: size,
    height: size,
    background: wash,
    borderColor: color,
    color,
    fontSize: Math.max(7, Math.round(size * 0.42)),
  };
  return (
    <span
      className={cx(
        "relative inline-flex shrink-0 items-center justify-center overflow-hidden",
        "border font-semibold",
        round ? "rounded-full" : "rounded-[3px]",
      )}
      style={style}
      // An icon beside its own name must not be announced twice, but it should
      // still answer a hover.
      title={labelled ? name : undefined}
      role={labelled ? undefined : "img"}
      aria-label={labelled ? undefined : name}
    >
      <span aria-hidden className="leading-none select-none">
        {iconInitials(name)}
      </span>
      {url ? (
        <img
          src={url}
          alt=""
          aria-hidden
          loading="lazy"
          decoding="async"
          width={size}
          height={size}
          className={cx(
            "absolute inset-0 size-full object-cover",
            round && "rounded-full",
          )}
          onError={(event) => {
            event.currentTarget.style.display = "none";
          }}
        />
      ) : null}
    </span>
  );
}

/** The tile colours for anything that belongs to a class. */
function classTile(wowClass: string) {
  return { color: classColor(wowClass), wash: classWash(wowClass, 22) };
}

export function ClassIcon({
  wowClass,
  size = 16,
  labelled = false,
}: {
  wowClass: string;
  size?: number;
  labelled?: boolean;
}) {
  return (
    <EntityIcon
      url={classIconUrl(wowClass)}
      name={wowClass}
      {...classTile(wowClass)}
      size={size}
      labelled={labelled}
      round
    />
  );
}

export function SpecIcon({
  build,
  size = 18,
  labelled = false,
}: {
  build: BuildLike;
  size?: number;
  labelled?: boolean;
}) {
  return (
    <EntityIcon
      url={specIconUrl(build.specId) ?? classIconUrl(build.class)}
      name={buildName(build)}
      {...classTile(build.class)}
      size={size}
      labelled={labelled}
    />
  );
}

/**
 * The hero-talent tree: emblem plus name, never emblem alone.
 *
 * `Default` is simc's marker for a spec it ships a single build for. It is not a
 * hero tree, so it gets no emblem and no invented name -- just a muted pill
 * saying what it actually means, which is that there is nothing to choose here.
 */
export function HeroTreeBadge({
  build,
  size = 15,
  className,
}: {
  build: Pick<BuildLike, "class" | "heroTalent">;
  size?: number;
  className?: string;
}) {
  if (build.heroTalent === NO_HERO_TREE) {
    return (
      <span
        className={cx(
          "inline-flex items-center rounded-full border border-hairline px-1.5 py-px",
          "text-[11px] text-ink-muted",
          className,
        )}
        title="SimulationCraft's profile for this build names no hero-talent tree"
      >
        No hero tree
      </span>
    );
  }
  const url = heroTreeIconUrl(build.heroTalent);
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 text-[11.5px] text-ink-secondary",
        className,
      )}
    >
      <EntityIcon
        url={url}
        name={build.heroTalent}
        {...classTile(build.class)}
        size={size}
        labelled
        round
      />
      {build.heroTalent}
    </span>
  );
}

// --------------------------------------------------------------------------------
// Composites
// --------------------------------------------------------------------------------

/**
 * The full identity of one build: spec icon, spec name in the class colour, and
 * the hero tree written out beneath or beside it.
 *
 * This is the component to reach for in a table row, a card header or a legend.
 * `layout="stacked"` puts the hero tree on its own line, which is what a narrow
 * table column wants; `"inline"` keeps it on one line.
 */
export function BuildIdentity({
  build,
  size = 20,
  layout = "stacked",
  hero = true,
  trailing,
  className,
}: {
  build: BuildLike;
  size?: number;
  layout?: "stacked" | "inline";
  /** Drop the hero tree where the surrounding context already names it. */
  hero?: boolean;
  trailing?: ReactNode;
  className?: string;
}) {
  const showHero = hero;
  return (
    <span className={cx("inline-flex min-w-0 items-center gap-2", className)}>
      <SpecIcon build={build} size={size} labelled />
      <span
        className={cx(
          "min-w-0",
          layout === "stacked"
            ? "flex flex-col leading-tight"
            : "inline-flex items-baseline gap-2",
        )}
      >
        <span
          className="truncate font-medium"
          style={{ color: classColor(build.class) }}
        >
          {buildName(build)}
        </span>
        {showHero ? <HeroTreeBadge build={build} /> : null}
        <UnvalidatedMark build={build} />
        {trailing}
      </span>
    </span>
  );
}

/**
 * The mark on a build simc wrote and did not switch on.
 *
 * Deliberately a word rather than a colour: class colour is already the primary
 * encoding here and the palette has no slot left that thirteen class hues do not
 * collide with. It also has to survive being read out loud, which is why the title
 * carries the whole sentence rather than the abbreviation.
 */
export function UnvalidatedMark({
  build,
}: {
  build: Pick<BuildLike, "unvalidated" | "gearComparable">;
}) {
  return (
    <>
      {build.unvalidated ? (
        <Mark title="SimulationCraft wrote this profile and left it commented out for this tier — a real simulation of a character its authors have not signed off">
          unvalidated
        </Mark>
      ) : null}
      {build.gearComparable === false ? (
        // The mark that changes how a number should be *read*, so it is drawn on any
        // build carrying it, shipped or not. Measured on MID2: the disabled profiles
        // wear item level 289 against a shipped 334-344, which put all eight of their
        // builds below all twenty-eight shipped ones — a gear gap that reads as a
        // balance result unless it is said out loud.
        <Mark title="This profile's gear is not at the tier's item level, so its place in a ranking of absolute damage is partly gear rather than spec. The build's own page states the gap.">
          gear differs
        </Mark>
      ) : null}
    </>
  );
}

function Mark({ title, children }: { title: string; children: ReactNode }) {
  return (
    <span
      className="shrink-0 rounded-sm border border-subtle px-1 py-px text-[10px] uppercase tracking-wide text-ink-tertiary"
      title={title}
    >
      {children}
    </span>
  );
}

/**
 * One line, for legends and tight cells: spec icon, spec name in class colour,
 * hero tree after it. Same rules, less vertical space.
 */
export function BuildChip({
  build,
  dash,
  className,
}: {
  build: BuildLike;
  /** Stroke dash of this build's line, drawn as a key. */
  dash?: string;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex min-w-0 items-center gap-1.5 text-[12.5px]",
        className,
      )}
    >
      {dash !== undefined ? (
        <svg width="16" height="8" aria-hidden className="shrink-0">
          <line
            x1="0"
            y1="4"
            x2="16"
            y2="4"
            stroke={classColor(build.class)}
            strokeWidth="2"
            strokeDasharray={dash}
          />
        </svg>
      ) : null}
      <SpecIcon build={build} size={15} labelled />
      <span className="truncate" style={{ color: classColor(build.class) }}>
        {buildName(build)}
      </span>
      <HeroTreeBadge build={build} size={13} />
      <UnvalidatedMark build={build} />
    </span>
  );
}

// --------------------------------------------------------------------------------
// Recharts axis ticks
// --------------------------------------------------------------------------------

/**
 * A category-axis tick that draws the spec icon and the build's name.
 *
 * Wowhead's tooltip script can never reach a chart axis -- it only enriches HTML
 * anchors -- but an SVG `<image>` with a URL of our own is a different matter, so
 * the icon that is impossible for an item name is straightforward here.
 *
 * The name is still drawn as text beside it. The icon is a second channel, not
 * the identity: an axis tick cannot be focused or hovered for an accessible
 * name, so the text is what has to carry it, and the table twin below every
 * chart carries it again.
 */
/**
 * The hero tree on a category-axis tick: emblem, then name.
 *
 * CLAUDE.md's rule is that a hero tree is icon *plus* written name everywhere --
 * class and spec icons are recognisable enough to carry a label on their own,
 * hero-tree emblems are not. The tick used to write only the name, which made it
 * the one place on the site that broke that rule. An SVG `<image>` is fine here:
 * the URL is ours, so unlike a Wowhead lookup it needs nothing from a script that
 * cannot see inside an SVG.
 */
function HeroTreeTick({
  x,
  y,
  heroTalent,
  size,
}: {
  x: number;
  y: number;
  heroTalent: string;
  size: number;
}) {
  const url = heroTreeIconUrl(heroTalent);
  const textX = url ? x + size + 4 : x;
  return (
    <g>
      {url ? (
        <image
          href={url}
          x={x}
          y={y + 11 - size + 1}
          width={size}
          height={size}
          // Decorative: the name is written out immediately beside it, so a
          // screen reader must hear it once rather than twice.
          aria-hidden="true"
          preserveAspectRatio="xMidYMid slice"
        />
      ) : null}
      <text x={textX} y={y} dy={11} fill="var(--text-muted)" fontSize={10.5}>
        {heroTalent}
      </text>
    </g>
  );
}

export function makeBuildTick(
  builds: Map<string, BuildLike>,
  { width, iconSize = 15 }: { width: number; iconSize?: number },
) {
  // Recharts types a tick's coordinates as `string | number`, so they are
  // narrowed here rather than asserted.
  return function BuildTick(props: {
    x?: string | number;
    y?: string | number;
    payload?: { value?: string | number };
  }) {
    const { x, y, payload } = props;
    if (typeof x !== "number" || typeof y !== "number") return null;
    const label = String(payload?.value ?? "");
    const build = builds.get(label);
    const url = build
      ? (specIconUrl(build.specId) ?? classIconUrl(build.class))
      : null;
    const color = build ? classColor(build.class) : "var(--text-muted)";
    // Ticks arrive right-aligned against the plot: lay the row out from the left
    // edge of the reserved width so icons line up in a column.
    const left = x - width;
    const gap = iconSize + 6;
    return (
      <g>
        <rect
          x={left}
          y={y - iconSize / 2}
          width={iconSize}
          height={iconSize}
          rx={3}
          fill={color}
          fillOpacity={0.22}
          stroke={color}
          strokeWidth={1}
        />
        {/* Initials under the image, exactly as the HTML icon does it: an opaque
            icon covers them, a blocked CDN leaves a lettered tile. */}
        <text
          x={left + iconSize / 2}
          y={y}
          dy={3}
          textAnchor="middle"
          fill={color}
          fontSize={7.5}
          fontWeight={600}
        >
          {iconInitials(build ? buildName(build) : label)}
        </text>
        {url ? (
          <image
            href={url}
            x={left}
            y={y - iconSize / 2}
            width={iconSize}
            height={iconSize}
            clipPath="inset(0 round 3px)"
            preserveAspectRatio="xMidYMid slice"
          />
        ) : null}
        <text
          x={left + gap}
          y={y}
          dy={build && build.heroTalent !== NO_HERO_TREE ? -1 : 4}
          fill={color}
          fontSize={11.5}
          fontWeight={500}
        >
          {build ? buildName(build) : label}
        </text>
        {build && build.heroTalent !== NO_HERO_TREE ? (
          <HeroTreeTick
            x={left + gap}
            y={y}
            heroTalent={build.heroTalent}
            size={iconSize - 4}
          />
        ) : null}
      </g>
    );
  };
}

/**
 * How opaque a build's mark should be drawn: full for a comparable one, faded for a
 * build whose number cannot be ranked against the others.
 *
 * Fading rather than hiding, and never recolouring: the bar is still a real
 * measurement of a real profile, and dropping it would recreate the failure the
 * coverage panel exists to prevent -- a spec that is absent reads as a spec that
 * ranks badly. Class colour stays the identity channel it is everywhere else.
 *
 * **The words go in the caption, not in the axis tick.** A third line under the
 * spec name was tried and collided with the row below it: the tick's height is the
 * chart's row spacing, which already carries two lines. Opacity is a weak channel
 * on its own, so what makes this legal is that three other channels say it in
 * words -- the caption under the chart, the badge in the table twin, and the
 * coverage panel.
 */
export function buildOpacity(build: Pick<BuildLike, "gearComparable">): number {
  return build.gearComparable === false ? 0.45 : 1;
}
