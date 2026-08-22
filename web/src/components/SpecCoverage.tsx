/**
 * Which specs and hero trees this tier's data covers, and — in several different
 * senses — which it does not.
 *
 * The question a reader arrives with the week a season opens: *is my spec missing,
 * or is it just bad?* A ranking can only draw what it has, so those two look
 * identical on it — and the second is a conclusion somebody might act on.
 *
 * There are **four** states, not two, and collapsing them shipped a wrong claim on
 * the day MID1 was first published:
 *
 * - *simulated* — simc ships a profile and it produced results.
 * - *broken* — simc ships a profile and this run got nothing out of it. A stored
 *   talent hash referencing nodes current spell data does not offer the spec is the
 *   usual cause; 15 of MID1's 41 profiles are in this state.
 * - *unvalidated* — simc wrote a complete profile, talent hash and every gear slot,
 *   and left every line of it commented out in the generator. That is the whole of
 *   MID2's gap. Simulated here and labelled, because "the character we had written
 *   down when we stopped" is not the same claim as "this is the spec this season".
 * - *missing* — simc has no profile for it at all, switched on or off.
 *
 * **Two of those are finer than a spec.** A spec plays two hero-talent trees, and a
 * tier routinely ships a build for one of them: MID2 covers 17 of 26 damage specs
 * and only 35 of 53 (spec × hero tree) pairs. Survival Hunter is simulated and Pack
 * Leader Survival is not, and the spec-level count cannot say so. `specIndex`
 * carries that finer answer — see `specindex.hero_tree_coverage` — and this panel
 * shows it per spec, falling back to the spec-level lists on a dataset built before
 * it existed.
 *
 * And a profile that simc wrote but will not load can now say **why**, with the node
 * id simc itself would name, decided offline against simc's trait table. That is the
 * difference between "no numbers for Arms Warrior" and "simc wrote a profile whose
 * stored talent hash the current tree refuses at node 110203" — only the second tells
 * a reader what would have to change.
 *
 * The reference list is derived rather than written down — see `spec_coverage` in
 * `profiles.py`. A hard-coded table of the game's damage specs would need editing
 * whenever Blizzard adds one, and would go stale in exactly the patch where this
 * matters most. Its cost is stated in the note: a spec no tier has *ever* profiled
 * is invisible to it, which is an under-claim rather than an invented spec list.
 */
import type {
  Manifest,
  SpecIndex,
  UncoveredHeroTree,
} from "../lib/types";
import { HeroTreeBadge, SpecIcon } from "./BuildIdentity";
import { Note, Panel, PanelHeader } from "./ui";
import { classColor } from "../lib/palette";

type Entry = { class: string; spec: string };

/** `Death Knight` + `Frost` -> `death_knight_frost`, which is what the icon map keys on. */
function specSlug(wowClass: string, spec: string): string {
  return `${wowClass}_${spec}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "");
}

/**
 * `MID2` + `Death Knight` -> the generator file simc keeps that class's profiles in.
 *
 * Both halves are verified rather than assumed: simc's default branch is named in
 * the manifest (`simc.gitBranch`), and the file naming — capitalise the first
 * letter, drop the space, so `Deathknight` and `Demonhunter` — matched all thirteen
 * generator files in the checkout on 2026-08-22. No link is drawn when the manifest
 * does not name a branch.
 */
function generatorUrl(
  branch: string | undefined,
  tier: string,
  wowClass: string,
): string | null {
  if (!branch) return null;
  const file = wowClass.replace(/\s+/g, "");
  const cased = file.charAt(0).toUpperCase() + file.slice(1).toLowerCase();
  return (
    `https://github.com/simulationcraft/simc/blob/${branch}` +
    `/profiles/generators/${tier}/${tier}_Generate_${cased}.simc`
  );
}

function profilesUrl(branch: string | undefined, tier: string): string | null {
  return branch
    ? `https://github.com/simulationcraft/simc/tree/${branch}/profiles/${tier}`
    : null;
}

export function SpecCoverage({
  manifest,
  specIndex,
}: {
  manifest: Manifest;
  specIndex?: SpecIndex | null;
}) {
  const coverage = manifest.coverage;
  // Datasets built before this existed carry no coverage block. Saying nothing is
  // right there: an empty panel would imply full coverage, which is the claim this
  // component exists to stop being made silently.
  if (!coverage || !coverage.damageSpecsKnown) return null;

  const { damageSpecs, damageSpecsKnown, missing } = coverage;
  const broken = coverage.broken ?? [];
  const unvalidated = coverage.unvalidated ?? [];
  // Undefined on a dataset built before the split existed. Falling back to the
  // shipped count is the old two-state reading, which is what that dataset means.
  const simulated = coverage.simulated ?? damageSpecs;
  // Counted separately from `simulated` on purpose. Adding them would make one
  // number out of two different claims, and the weaker one would disappear into
  // it -- which is the failure this whole panel exists to prevent.
  const unvalidatedRan = coverage.unvalidatedSimulated ?? 0;

  const branch = manifest.simc?.gitBranch;
  const tier = manifest.tier;
  const heroCoverage =
    specIndex && specIndex.tier === tier ? specIndex.heroTreeCoverage : null;
  // Uncovered (spec x hero tree) pairs, keyed so a spec's row can list its own.
  const bySpec = new Map<string, UncoveredHeroTree[]>();
  for (const cell of heroCoverage?.uncovered ?? []) {
    const key = `${cell.class}|${cell.spec}`;
    bySpec.set(key, [...(bySpec.get(key) ?? []), cell]);
  }
  // A spec whose hero trees are all covered but which is not in any state list is
  // simply fine; a spec in `shipped` with uncovered trees is the new group.
  const shippedGaps = [...bySpec.entries()]
    .filter(([, cells]) => cells.every((cell) => cell.state === "shipped"))
    .map(([key]) => {
      const [wowClass, spec] = key.split("|");
      return { class: wowClass ?? "", spec: spec ?? "" };
    });

  const complete =
    missing.length === 0 &&
    broken.length === 0 &&
    unvalidated.length === 0 &&
    shippedGaps.length === 0;

  return (
    <Panel>
      <PanelHeader
        title="Which specs and hero trees this covers"
        subtitle={
          complete
            ? "SimulationCraft ships a profile for every damage spec in this tier and for both hero trees of each, and every one of them ran."
            : "A build absent from this dataset cannot appear in any ranking here — which looks exactly like one that ranks badly. There are several ways to be absent and they are not the same thing."
        }
      />

      <div className="px-5 pb-4">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[22px] font-semibold tabular-nums text-ink-primary">
            {simulated} of {damageSpecsKnown}
          </span>
          <span className="text-[13px] text-ink-tertiary">
            damage specs are in this dataset from a profile SimulationCraft
            ships
          </span>
        </div>

        {unvalidatedRan ? (
          <div className="mt-1 text-[13px] text-ink-tertiary">
            <span className="tabular-nums text-ink-secondary">
              +{unvalidatedRan}
              {unvalidated.length > unvalidatedRan
                ? ` of ${unvalidated.length}`
                : ""}
            </span>{" "}
            more ran from a profile simc wrote and left switched off. Those are
            shown throughout with an{" "}
            <span className="text-ink-secondary">unvalidated</span> mark.
          </div>
        ) : null}

        {/* The bar is redundant with the number beside it, deliberately: it is the
            only thing on the Overview that shows a *fraction* rather than a ranking,
            and the shape is what makes "most of a season is missing" land. The two
            kinds of absence get their own segments, because the whole point of this
            panel is that they are different. */}
        <div
          className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-elevated"
          role="img"
          aria-label={`${simulated} of ${damageSpecsKnown} damage specs simulated from a shipped profile, ${unvalidatedRan} from an unvalidated one, ${broken.length} shipped but not loading, ${missing.length} with no profile`}
        >
          <div
            className="h-full bg-[var(--series-1)]"
            style={{ width: `${(simulated / damageSpecsKnown) * 100}%` }}
          />
          <div
            className="h-full bg-[var(--series-3)]"
            style={{ width: `${(unvalidatedRan / damageSpecsKnown) * 100}%` }}
          />
          <div
            className="h-full bg-[var(--series-2)]"
            style={{ width: `${(broken.length / damageSpecsKnown) * 100}%` }}
          />
        </div>

        {heroCoverage ? (
          <div className="mt-3 text-[13px] text-ink-tertiary">
            One level finer:{" "}
            <span className="tabular-nums text-ink-secondary">
              {heroCoverage.covered} of {heroCoverage.cells}
            </span>{" "}
            spec-and-hero-tree pairs have a build. A spec plays two hero trees
            and a tier can ship a build for one of them, which the count above
            cannot say.
          </div>
        ) : null}
      </div>

      <SpecList
        entries={broken}
        label="Profile ships and does not load"
        swatch="var(--series-2)"
        gaps={bySpec}
        branch={branch}
        tier={tier}
      />
      <SpecList
        entries={unvalidated}
        label="Profile written, not switched on"
        swatch="var(--series-3)"
        gaps={bySpec}
        branch={branch}
        tier={tier}
      />
      <SpecList
        entries={missing}
        label="No profile at all"
        gaps={bySpec}
        branch={branch}
        tier={tier}
      />
      <SpecList
        entries={shippedGaps}
        label="Shipped, but one hero tree has no build"
        gaps={bySpec}
        branch={branch}
        tier={tier}
        href={profilesUrl(branch, tier)}
      />

      <Note>
        {complete ? (
          <>
            Every damage spec SimulationCraft has ever shipped a tier profile
            for is here, on both of its hero trees.
          </>
        ) : (
          <>
            {broken.length ? (
              <>
                SimulationCraft <em>ships</em> a profile for the first group and
                it no longer loads. That is a property of an ageing tier, not of
                the spec: nothing can be said about how they perform until
                simc&rsquo;s profiles are refreshed.{" "}
              </>
            ) : null}
            {unvalidated.length ? (
              <>
                SimulationCraft <em>wrote</em> a profile for the next group —
                talent hash, every gear slot — and left every line of it
                commented out, which is what its authors do while a profile or a
                rotation is not validated for the tier. The rotation is not what
                is missing: simc maintains a current action list for these specs
                in the same checkout. What is missing is a signed-off{" "}
                <em>character</em>. Those that load are simulated here and marked{" "}
                <em>unvalidated</em> wherever they appear, and they wear last
                season&rsquo;s item level and none of this season&rsquo;s tier
                set, so their numbers sit well below the rest for two reasons
                that have nothing to do with the spec.{" "}
              </>
            ) : null}
            {missing.length ? (
              <>
                The next group is absent from SimulationCraft&rsquo;s profiles
                for this tier, switched on or off — nothing here failed. A
                profile is contributed to simc as one of the generator blocks
                linked above.{" "}
              </>
            ) : null}
            {shippedGaps.length ? (
              <>
                The last group is simulated: simc ships a build for one of the
                spec&rsquo;s two hero trees and not the other, so the ranking
                shows the spec and not that build.{" "}
              </>
            ) : null}
            The list of what <em>should</em> exist is derived from the tiers simc
            has shipped
            {coverage.comparedWith.length
              ? ` (${coverage.comparedWith.join(", ")})`
              : ""}
            , and the hero trees from simc&rsquo;s own trait table, so a spec or
            a tree Blizzard adds counts as soon as it is in either. The cost of
            deriving it rather than writing it down: a spec no tier has ever
            profiled cannot be reported missing, because nothing here knows it
            exists. This list under-claims rather than inventing a spec list.
          </>
        )}
      </Note>
    </Panel>
  );
}

/**
 * One group of absent builds, one row per spec.
 *
 * Grouped by spec rather than by class, because the hero tree is the thing being
 * reported and a class row could not carry two specs' trees. Each row is the
 * three redundant identity channels the site uses everywhere: the spec icon, the
 * spec name in the class colour, and the hero tree as emblem *plus* written name.
 */
function SpecList({
  entries,
  label,
  swatch,
  gaps,
  branch,
  tier,
  href,
}: {
  entries: Entry[];
  label: string;
  swatch?: string;
  gaps: Map<string, UncoveredHeroTree[]>;
  branch: string | undefined;
  tier: string;
  href?: string | null;
}) {
  if (entries.length === 0) return null;

  return (
    <div className="px-5 pb-4">
      <div className="mb-2 flex items-center gap-2 text-[11.5px] tracking-wide text-ink-tertiary uppercase">
        {swatch ? (
          <span
            aria-hidden="true"
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: swatch }}
          />
        ) : null}
        {label} ({entries.length})
      </div>
      <ul className="flex flex-col gap-2">
        {entries.map((entry) => (
          <SpecRow
            key={`${entry.class}|${entry.spec}`}
            entry={entry}
            cells={gaps.get(`${entry.class}|${entry.spec}`) ?? null}
            href={href ?? generatorUrl(branch, tier, entry.class)}
            label={href ? "simc's profiles" : "simc's generator"}
          />
        ))}
      </ul>
    </div>
  );
}

function SpecRow({
  entry,
  cells,
  href,
  label,
}: {
  entry: Entry;
  cells: UncoveredHeroTree[] | null;
  href: string | null;
  label: string;
}) {
  const build = {
    id: "",
    class: entry.class,
    spec: entry.spec,
    specId: specSlug(entry.class, entry.spec),
    heroTalent: "",
    displayName: `${entry.spec} ${entry.class}`,
  };
  // Only the trees with *no* build are named. A spec listed here because its
  // profile is switched off may still have a build for both of its trees -- both of
  // Devastation Evoker's ran -- so an unqualified "both hero trees" would be a
  // claim the coverage data contradicts. No cells means nothing to add, and on a
  // dataset built before hero-tree coverage existed that is every row.
  const trees = cells ?? [];
  const reasons = [...new Set(trees.map((cell) => cell.reason).filter(Boolean))];

  return (
    <li>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex items-center gap-2">
          <SpecIcon build={build} size={18} labelled />
          <span
            className="text-[12.5px]"
            style={{ color: classColor(entry.class) }}
          >
            {entry.spec} {entry.class}
          </span>
        </span>
        {trees.length ? (
          <span className="text-[11.5px] text-ink-muted">
            no build for
          </span>
        ) : null}
        {trees.map((cell) =>
          cell.tree ? (
            <HeroTreeBadge
              key={cell.subTree}
              build={{ class: entry.class, heroTalent: cell.tree }}
            />
          ) : (
            <span key={cell.subTree} className="text-[11.5px] text-ink-muted">
              hero tree {cell.subTree}, which simc&rsquo;s data does not name
            </span>
          ),
        )}
        {href ? (
          <a
            className="text-[11.5px] text-ink-muted underline decoration-hairline underline-offset-2 hover:text-ink-secondary"
            href={href}
            target="_blank"
            rel="noreferrer noopener"
          >
            {label}
          </a>
        ) : null}
      </div>
      {/* One line per distinct refusal: Havoc's two builds are refused at two
          different nodes, and printing one of them against both would name the
          wrong node for one of the trees. */}
      {reasons.map((reason) => (
        <div key={reason} className="mt-0.5 pl-7 text-[11.5px] text-ink-muted">
          simc will not load it: {reason}
        </div>
      ))}
    </li>
  );
}
