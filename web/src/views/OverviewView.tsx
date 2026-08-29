/**
 * Ranking view: every spec at one scenario and target count.
 *
 * Reads only the manifest, so it renders without fetching per-spec files. The
 * offered target counts are derived from the summary's own keys -- whatever the
 * pipeline measured for the scenario is what can be picked, which is what lets
 * a boss scenario measured at 2 targets appear at all. Datasets written before
 * 2026-08-29 carry only the counts 1/3/5/10 and no per-count priority damage;
 * everything below degrades to exactly the view that shipped against them.
 *
 * Nothing is selected here and nothing ever was -- this view is the precedent
 * the rest of the site was rebuilt against. Every build in the tier, sorted,
 * with its class colour, its spec icon and its hero tree written out.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AXIS_LINE,
  AXIS_TICK,
  CURSOR_FILL,
  GRID,
  TooltipCard,
} from "../components/chart";
import {
  BuildIdentity,
  buildOpacity,
  makeBuildTick,
} from "../components/BuildIdentity";
import {
  EmptyState,
  Note,
  Panel,
  PanelHeader,
  SegmentedControl,
  Select,
} from "../components/ui";
import { PatchState } from "../components/PatchState";
import { SpecCoverage } from "../components/SpecCoverage";
import {
  compactNumber,
  describeFunnelGain,
  fullNumber,
  percent,
  samplingError,
} from "../lib/format";
import { classColor, classWash } from "../lib/palette";
import {
  bestBuildFor,
  bestBuildMark,
  computedScope,
  findComputedSpec,
  type BestBuild,
  type ComputedScope,
} from "../lib/bestBuild";
import type {
  ComputedBuildsDataset,
  Manifest,
  ScenarioMeta,
  SpecIndex,
  SpecSummary,
} from "../lib/types";

/** Two lines of tick text plus the icon need more room than a bare name. */
const ROW_HEIGHT = 32;
const TICK_WIDTH = 205;

type Mode = "chart" | "table";

/**
 * Which axis the ranking is sorted by. "total" is overall damage; "boss" is
 * damage on the priority target -- the axis that decides a fight where the boss
 * is what has to die. Only offered where the summary carries a measured split.
 */
type SortMetric = "total" | "boss";

interface Row {
  id: string;
  label: string;
  build: SpecSummary;
  /**
   * The value the row is ranked and drawn by under the total metric: simc's own
   * measurement, unless a computed build beat it outside the tie band. See
   * `lib/bestBuild.ts` -- a marked row carries a projection, never a
   * measurement, and `best.simcDps` keeps the measured figure beside it.
   */
  dps: number;
  /** Which build won, and what that costs. Never null. */
  best: BestBuild;
  /** The part of the bar that is simc's own measurement. */
  simcDps: number;
  /** "computed +2.20%", or null on a row simc still owns. */
  mark: string | null;
  /**
   * Measured damage on the priority target at this count. Undefined at one
   * target (the two axes are one number there), for single-enemy scenarios
   * (simc emits none) and on datasets written before the summary carried it.
   */
  priorityDps?: number;
  /** Per-cell standard error in percent, for the note under the chart. */
  dpsError?: number;
  /**
   * The stacked segments the chart draws. Exactly one decomposition is active
   * per row: a row with a measured split draws boss (solid) + rest (wash), a
   * row without draws solid alone -- never a full wash bar, which would claim
   * "none of this lands on the boss" about a number nobody split.
   */
  bossPart: number;
  restPart: number;
  solidPart: number;
  /** The projected remainder under the total metric. Zero on an unmarked row. */
  uplift: number;
  funnelGain?: number;
  priorityShare?: number;
}

export function OverviewView({
  manifest,
  scenario,
  specIndex,
  computedBuilds,
  computedSettled,
  targets,
  onTargetsChange,
  onScenarioChange,
  onOpenSpec,
}: {
  manifest: Manifest;
  scenario: ScenarioMeta;
  /**
   * Already fetched at app level for the Spec detail picker, so the coverage panel
   * costs no extra request. Null until it arrives, and on a tier built before
   * `wowdps spec-index` existed -- the panel then falls back to spec-level coverage.
   */
  specIndex: SpecIndex | null;
  /**
   * Builds this project computed, beside simc's own. Optional by contract:
   * absent is the state of every tier that has never been through
   * `wowdps build-search`, and the ranking then draws simc's numbers exactly
   * as it did before this document was read.
   */
  computedBuilds: ComputedBuildsDataset | null;
  /** True once the question "does that document exist?" has been answered. */
  computedSettled: boolean;
  /** From the URL, so a configured ranking is a link. Null means "first offered". */
  targets: number | null;
  onTargetsChange: (targets: number) => void;
  onScenarioChange: (id: string) => void;
  onOpenSpec: (id: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("chart");
  const [sortBy, setSortBy] = useState<SortMetric>("total");

  // The default axis follows the scenario (owner decision, 2026-08-29): base
  // sweeps open on Overall, a boss scenario opens on Boss DPS -- there the
  // boss is the question. The toggle still overrides for the current view;
  // switching scenarios re-applies the default rather than carrying a choice
  // made about a different fight. Keyed on the id string, so an unrelated
  // re-render cannot eat a reader's toggle.
  useEffect(() => {
    setSortBy(scenario.id.startsWith("boss_") ? "boss" : "total");
  }, [scenario.id]);

  // The counts actually measured for this scenario, read off the summary's own
  // keys rather than a fixed list. A boss scenario has exactly one; patchwerk
  // in an old dataset has 1/3/5/10 and in a new one all ten.
  const available = useMemo(() => {
    const counts = new Set<number>();
    for (const spec of manifest.specs) {
      const entry = spec.scenarios[scenario.id];
      if (!entry) continue;
      for (const key of Object.keys(entry.dps)) {
        const count = Number(key);
        if (Number.isFinite(count)) counts.add(count);
      }
    }
    return [...counts].sort((a, b) => a - b);
  }, [manifest.specs, scenario.id]);
  const effectiveTargets =
    targets !== null && available.includes(targets)
      ? targets
      : (available[0] ?? 1);

  // A chosen count the current scenario does not offer is rewritten to what is
  // actually shown, so the URL never states a count the page is not drawing --
  // e.g. targets=10 surviving a switch to a two-target boss. Null is left
  // alone: "follow the first offered" keeps links clean.
  useEffect(() => {
    if (targets !== null && targets !== effectiveTargets) {
      onTargetsChange(effectiveTargets);
    }
  }, [targets, effectiveTargets, onTargetsChange]);

  // Boss scenarios are ordinary scenarios with a `boss_` id -- one measured
  // composition, one target count. They get their own select so the base
  // sweeps stay a short list, but both selects write the same `scenario=`
  // state: one axis, two projections of it, never a second source of truth.
  const baseScenarios = useMemo(
    () => manifest.scenarios.filter((entry) => !entry.id.startsWith("boss_")),
    [manifest.scenarios],
  );
  const bossScenarios = useMemo(
    () => manifest.scenarios.filter((entry) => entry.id.startsWith("boss_")),
    [manifest.scenarios],
  );
  const isBossScenario = scenario.id.startsWith("boss_");

  // Whether this (scenario, targets) carries a measured boss/overall split at
  // all. At one target the two axes are the same number, and a dataset from
  // before the summary carried priorityDps has no split anywhere -- in both
  // cases the toggle disappears and the chart draws exactly as it always did.
  // Derived from the manifest, not from the built rows, because the sort state
  // can outlive the data that justified it: a reader who picks "Boss DPS" at
  // five targets and then switches to one must land back on the total sort --
  // including its projection segments -- not on a boss sort of rows that have
  // no boss axis.
  const hasSplit = useMemo(
    () =>
      manifest.specs.some(
        (spec) =>
          spec.scenarios[scenario.id]?.priorityDps?.[String(effectiveTargets)] !==
          undefined,
      ),
    [manifest.specs, scenario.id, effectiveTargets],
  );
  const effectiveSort: SortMetric = hasSplit ? sortBy : "total";

  const rows = useMemo(
    () =>
      buildRows(
        manifest.specs,
        scenario.id,
        effectiveTargets,
        computedBuilds,
        effectiveSort,
      ),
    [manifest.specs, scenario.id, effectiveTargets, computedBuilds, effectiveSort],
  );

  const scope: ComputedScope = computedScope(
    computedBuilds,
    scenario.id,
    effectiveTargets,
  );
  const computedRows = rows.filter((row) => row.best.projected).length;

  // Independent of the sort order, deliberately: under the boss sort rows[0]
  // is the best *boss* row, whose total need not be the maximum, and a
  // percent column divided by it reads ">100% of the top build" on an
  // unsplit row. Both denominators are maxima over all rows.
  const best = rows.reduce((max, row) => Math.max(max, row.dps), 0);
  const bestBoss = Math.max(
    0,
    ...rows.map((row) => row.priorityDps ?? 0),
  );

  // The precision of *these* cells, not of the whole run: the manifest-level
  // median pools every scenario, and pooling understates the noisier ones by
  // a factor of two (issue #103's finding, Dungeon Slice measured). Per-count
  // errors arrived in the summary alongside priorityDps; older datasets fall
  // back to the pooled figure via samplingError().
  const scenarioError = useMemo(() => {
    const errors = rows
      .map((row) => row.dpsError)
      .filter((value): value is number => typeof value === "number")
      .sort((a, b) => a - b);
    if (errors.length === 0) return null;
    const mid = Math.floor(errors.length / 2);
    const upper = errors[mid] ?? 0;
    const lower = errors[mid - 1] ?? upper;
    const median = errors.length % 2 === 1 ? upper : (lower + upper) / 2;
    // A zero is not a measurement -- the "converged to 0% standard error"
    // footer bug, one field over. Fall back to the pooled figure instead.
    if (!(median > 0)) return null;
    return `${median < 0.1 ? median.toFixed(2) : median.toFixed(1)}%`;
  }, [rows]);

  // Counted over the rows actually on screen rather than over the tier, so a
  // scenario that happens to contain no flagged build says nothing at all. The two
  // reasons overlap by design -- simc's disabled profiles are behind on item level
  // *and* wear no set -- so these are counts of the faded builds carrying each
  // property, not a partition of them, and the caption is worded that way.
  const ilevelGap = rows.filter(
    (row) => row.build.gearComparable === false,
  ).length;
  const setGap = rows.filter(
    (row) => row.build.tierSetComparable === false,
  ).length;
  const faded = rows.filter((row) => buildOpacity(row.build) < 1).length;
  const unsplit = hasSplit
    ? rows.filter((row) => row.priorityDps === undefined).length
    : 0;

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title={`${scenario.label} — ${effectiveTargets} ${
            effectiveTargets === 1 ? "target" : "targets"
          }`}
          subtitle={scenario.description}
          actions={
            <>
              <Select
                label="Scenario"
                value={isBossScenario ? "" : scenario.id}
                onChange={(id) => {
                  if (id) onScenarioChange(id);
                }}
                options={[
                  // A boss being open leaves the base select on a placeholder
                  // rather than silently highlighting a sweep nobody is
                  // looking at. Picking any sweep leaves boss mode; picking
                  // the placeholder itself is guarded to a no-op below, same
                  // as the Boss select's "—".
                  ...(isBossScenario
                    ? [{ value: "", label: "— boss fight —" }]
                    : []),
                  ...baseScenarios.map((entry) => ({
                    value: entry.id,
                    label: entry.label,
                  })),
                ]}
              />
              {bossScenarios.length > 0 ? (
                <Select
                  label="Boss"
                  value={isBossScenario ? scenario.id : ""}
                  onChange={(id) => {
                    if (id) onScenarioChange(id);
                  }}
                  options={[
                    { value: "", label: "—" },
                    ...bossScenarios.map((entry) => ({
                      value: entry.id,
                      label: entry.label,
                    })),
                  ]}
                />
              ) : null}
              {available.length > 1 ? (
                <Select
                  label="Targets"
                  value={effectiveTargets}
                  onChange={onTargetsChange}
                  options={available.map((count) => ({
                    value: count,
                    label: String(count),
                  }))}
                />
              ) : null}
              {hasSplit ? (
                <SegmentedControl
                  label="Sort by"
                  value={effectiveSort}
                  onChange={setSortBy}
                  options={[
                    { value: "total", label: "Overall" },
                    { value: "boss", label: "Boss DPS" },
                  ]}
                />
              ) : null}
              <SegmentedControl
                label="Display"
                value={mode}
                onChange={setMode}
                options={[
                  { value: "chart", label: "Chart" },
                  { value: "table", label: "Table" },
                ]}
              />
            </>
          }
        />

        {rows.length === 0 ? (
          <EmptyState>
            No results for this scenario yet. The nightly simulation run fills
            these in as SimulationCraft adds profiles for the current tier.
          </EmptyState>
        ) : mode === "chart" ? (
          <RankingChart
            rows={rows}
            best={best}
            bestBoss={bestBoss}
            sortBy={effectiveSort}
            hasSplit={hasSplit}
            onOpenSpec={onOpenSpec}
          />
        ) : (
          <RankingTable
            rows={rows}
            best={best}
            bestBoss={bestBoss}
            sortBy={effectiveSort}
            hasSplit={hasSplit}
            onOpenSpec={onOpenSpec}
          />
        )}

        <Note>
          Simulated damage per second against{" "}
          {isBossScenario
            ? // "Where first kills established one": two of MID2's four promoted
              // bosses carry a measured target count, the other two run at the
              // default composition with only the kill length measured. Claiming
              // "measured composition" for all four would label a defaulted fact
              // as a measurement.
              "this boss's logged kill length, and its measured composition where first kills established one"
            : "a stationary target with no external buffs"}
          , using SimulationCraft&rsquo;s own tier profiles. Treat gaps under a
          few percent as a tie — the sampling error alone is around{" "}
          {scenarioError ?? samplingError(manifest.settings)}
          {scenarioError ? " on these cells" : ""}. Bars carry each build&rsquo;s
          class colour; the icon and the name beside it are what identify it.
          {hasSplit ? (
            <>
              {" "}
              <strong className="font-medium text-ink-secondary">
                Each bar is split where the split was measured: the solid part
                is damage on the priority target, the washed-out remainder is
                everything else.
              </strong>{" "}
              {effectiveSort === "boss"
                ? "Sorted by damage on the boss — the solid segments — so a build that tops the meter can sit below one that kills the boss faster."
                : "Sorted by overall damage."}
              {unsplit > 0
                ? ` ${unsplit} ${unsplit === 1 ? "row draws" : "rows draw"} solid because no split was measured there — that says "not measured", never "all of it lands on the boss".`
                : ""}
            </>
          ) : null}
          {/* Three sentences, not one. "No talent search has run at this target
              count" and "a search ran and simc's builds all held" are different
              claims, and `computed-builds.json` covers Patchwerk at one target
              only today. Collapsing them would publish a finding nobody made. */}
          {scope === "searched" && computedRows > 0 ? (
            <>
              {" "}
              <strong className="font-medium text-ink-secondary">
                Ranked by the best build known for each row
                {effectiveSort === "boss"
                  ? " on the overall axis; under this sort, marked rows sit by simc's measured boss damage"
                  : ""}
                .
              </strong>{" "}
              {computedRows} {computedRows === 1 ? "build is" : "builds are"}{" "}
              ranked by talents this project computed rather than the ones
              SimulationCraft ships, because they beat simc&rsquo;s by more than
              the two runs&rsquo; combined sampling error.{" "}
              {effectiveSort === "boss"
                ? "Under this sort their bars carry no projection segment — the gain is a total-damage claim, so it lives in the table and the tooltip here. "
                : "Those bars carry a hatched segment at the end — that segment is the gain, and it is a projection rather than a measurement: it was carried forward onto simc's own published figure, which the table beside this chart prints next to it. "}
              Where the same run measured boss damage,
              the mark also says what the computed build does <em>there</em>{" "}
              &mdash; a build can gain overall and lose on the boss, which is
              the trade to see before picking one. Every other row is
              SimulationCraft&rsquo;s build, unchanged.
            </>
          ) : scope === "searched" ? (
            <>
              {" "}
              A talent search ran for this scenario at this target count and beat
              none of SimulationCraft&rsquo;s own builds by more than the
              combined sampling error, so every row here is simc&rsquo;s build.
            </>
          ) : scope === "not-searched" ? (
            <>
              {" "}
              No talent search has run for this scenario at this target count, so
              every row is SimulationCraft&rsquo;s own build. That is not a
              finding about these builds &mdash; nobody has looked here yet.
            </>
          ) : computedSettled ? (
            <>
              {" "}
              This season carries no computed-build document, so every row is
              SimulationCraft&rsquo;s own build.
            </>
          ) : null}
          {faded > 0 ? (
            <>
              {" "}
              <strong className="font-medium text-ink-secondary">
                Faded bars carry gear the rest of the tier does not, so where
                they sit against the others is partly gear rather than spec.
              </strong>{" "}
              {/*
                The reasons are named, never counted. They overlap -- simc's disabled
                profiles are behind on item level *and* wear no set, so on MID2 the
                two counts would be 8 and 10 over 10 faded bars and a reader who added
                them would get 18. Naming them costs no arithmetic and degrades
                correctly: a tier with one kind of gap prints one clause.

                Each clause is drawn only when a row on screen carries that flag,
                because a single sentence covering every fade would attribute the
                Arcane deficit to item level -- exactly what the two-flag split in the
                dataset exists to prevent.

                Deliberately unquantified, too. The measured four-piece gain for MID2's
                Arcane builds is +13.13% and +14.42%, but this caption renders for any
                tier and this project's own buff sweep puts a full tier set anywhere
                between 5.96% and 23.19% depending on the build. One number here would
                look more precise than it is, which this project counts as a bug, so
                the size stays where it can carry its measurement: the build's page.
              */}
              {ilevelGap > 0 && setGap > 0
                ? "Two things put a bar here — gear at a different item level from the tier's, and a different amount of this season's tier set."
                : ilevelGap > 0
                  ? "What puts a bar here is gear at a different item level from the tier's."
                  : "What puts a bar here is a different amount of this season's tier set."}{" "}
              The table beside this chart names which, on each build, and the
              build&rsquo;s own page states the gap. They are drawn rather than
              dropped because a spec that is absent reads as a spec that ranks
              badly, which is the thing this page most has to avoid.
            </>
          ) : null}
        </Note>
      </Panel>

      <SpecCoverage manifest={manifest} specIndex={specIndex} />

      <PatchState manifest={manifest} />
    </div>
  );
}

function buildRows(
  specs: SpecSummary[],
  scenarioId: string,
  targets: number,
  computedBuilds: ComputedBuildsDataset | null,
  sortBy: SortMetric,
): Row[] {
  const rows: Row[] = [];
  for (const spec of specs) {
    const entry = spec.scenarios[scenarioId];
    const dps = entry?.dps[String(targets)];
    if (typeof dps !== "number") continue;
    // Joined on (id, scenario, targets), never on the id alone: a verdict is a
    // statement about one scenario at one target count.
    const best = bestBuildFor(
      dps,
      findComputedSpec(computedBuilds, spec.id, scenarioId, targets),
    );
    const priorityDps = entry?.priorityDps?.[String(targets)];
    const hasSplit = typeof priorityDps === "number";
    rows.push({
      id: spec.id,
      label: spec.displayName,
      build: spec,
      dps: best.rankDps,
      best,
      simcDps: best.simcDps,
      mark: bestBuildMark(best),
      priorityDps: hasSplit ? priorityDps : undefined,
      dpsError: entry?.dpsError?.[String(targets)],
      // One decomposition per row. The projection segment is a total-axis
      // claim, so it is drawn only under the total sort -- under the boss sort
      // it would stack a total gain onto a bar being read for boss damage.
      bossPart: hasSplit ? priorityDps : 0,
      restPart: hasSplit ? Math.max(0, best.simcDps - priorityDps) : 0,
      solidPart: hasSplit ? 0 : best.simcDps,
      uplift: sortBy === "total" ? best.rankDps - best.simcDps : 0,
      funnelGain: entry?.funnelGain,
      priorityShare: entry?.priorityShare,
    });
  }
  // Ranked by the best build known for each row under the total metric -- the
  // whole reason this view carries the computed document. Under the boss
  // metric the sort key is simc's *measured* priority damage: the computed
  // margin is a total-axis measurement and is never projected onto an axis
  // nobody measured it on. Rows without a measured split sort after the ones
  // with one, by total, rather than being dropped.
  if (sortBy === "boss") {
    rows.sort((a, b) => {
      const aBoss = a.priorityDps ?? -1;
      const bBoss = b.priorityDps ?? -1;
      if (aBoss !== bBoss) return bBoss - aBoss;
      return b.dps - a.dps;
    });
  } else {
    rows.sort((a, b) => b.dps - a.dps);
  }
  return rows;
}

function RankingChart({
  rows,
  best,
  bestBoss,
  sortBy,
  hasSplit,
  onOpenSpec,
}: {
  rows: Row[];
  best: number;
  bestBoss: number;
  sortBy: SortMetric;
  hasSplit: boolean;
  onOpenSpec: (id: string) => void;
}) {
  // Horizontal bars: the labels are long spec names, which read far better along
  // the y-axis than rotated under a vertical chart.
  const height = Math.max(280, rows.length * ROW_HEIGHT + 40);
  const byLabel = useMemo(
    () => new Map(rows.map((row) => [row.label, row.build])),
    [rows],
  );
  const tick = useMemo(
    () => makeBuildTick(byLabel, { width: TICK_WIDTH }),
    [byLabel],
  );

  const openRow = (entry: unknown) => {
    const row = (entry as { payload?: Row } | undefined)?.payload;
    if (row) onOpenSpec(row.id);
  };

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={height}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 56, bottom: 4, left: 8 }}
        >
          {/* One diagonal hatch per class present, since an SVG pattern cannot
              inherit the shape's fill. Built from the rows rather than from the
              whole class list, so a chart of six builds carries six defs. */}
          <defs>
            {Array.from(new Set(rows.map((row) => row.build.class))).map((wowClass) => (
              <pattern
                key={wowClass}
                id={hatchId(wowClass)}
                patternUnits="userSpaceOnUse"
                width={6}
                height={6}
                patternTransform="rotate(135)"
              >
                <rect width={3} height={6} fill={classColor(wowClass)} />
              </pattern>
            ))}
          </defs>
          <CartesianGrid {...GRID} vertical horizontal={false} />
          <XAxis
            type="number"
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            tickFormatter={compactNumber}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={TICK_WIDTH}
            tick={tick}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const row = payload[0]?.payload as Row | undefined;
              if (!row) return null;
              return (
                <TooltipCard
                  title={row.label}
                  subtitle={
                    sortBy === "boss" && row.priorityDps !== undefined
                      ? `${percent(bestBoss > 0 ? row.priorityDps / bestBoss : 0, 0)} of the top build's boss damage`
                      : `${percent(best > 0 ? row.dps / best : 0, 0)} of the top build`
                  }
                  rows={[
                    {
                      id: "dps",
                      label: row.best.projected
                        ? "Overall DPS, best build"
                        : "Overall DPS",
                      color: classColor(row.build.class),
                      value: fullNumber(row.dps),
                      ...(row.best.projected
                        ? {
                            hint: "simc's own figure carried forward by the measured talent gain — not a run of its own",
                          }
                        : {}),
                    },
                    ...(row.priorityDps !== undefined
                      ? [
                          {
                            id: "boss",
                            label: "On the priority target",
                            value: fullNumber(row.priorityDps),
                            hint: "measured — the solid part of the bar",
                          },
                        ]
                      : []),
                    ...(row.best.projected
                      ? [
                          {
                            id: "simc",
                            label: "SimulationCraft's own build",
                            // The margin's kit differs per row (marginBasis),
                            // and the boss ratio is always the anchored run's;
                            // one "measured on..." preamble over both numbers
                            // was naming the wrong kit for the 148 rows whose
                            // margin is shipped-gear.
                            hint: `${row.mark}, margin measured on ${
                              row.best.marginBasis === "shipped-gear"
                                ? "simc's own gear"
                                : "one normalised kit"
                            }${
                              hasSplit && row.best.priorityGain !== null
                                ? ` — boss damage, measured on the normalised kit: ${row.best.priorityGain >= 0 ? "+" : ""}${(row.best.priorityGain * 100).toFixed(2)}% there`
                                : ""
                            }`,
                            value: fullNumber(row.simcDps),
                          },
                        ]
                      : []),
                    ...(row.funnelGain !== undefined
                      ? [
                          {
                            id: "funnel",
                            label: "Funnel gain at 5T",
                            value: `${row.funnelGain.toFixed(2)}x`,
                            hint: describeFunnelGain(row.funnelGain),
                          },
                        ]
                      : []),
                  ]}
                />
              );
            }}
          />
          {/* Class colour, per the site's identity rule: the bar length carries the
              magnitude, the hue carries which class it belongs to, and the icon and
              name on the axis carry which build.

              Up to three stacked segments, because the parts are not the same kind
              of number, and each claim gets its own channel:

              - solid class colour: measured damage on the priority target (or the
                whole measurement, on a row with no measured split);
              - the same hue washed out (classWash): the measured remainder --
                damage that lands elsewhere. A flat wash, not a texture, so it
                cannot be confused with the hatch;
              - the 135-degree HATCH: the projected computed-build gain, a
                total-axis claim, drawn only under the total sort.

              HATCHING, NOT A SECOND OPACITY, and the reason is measured. The
              projected segment used to be drawn at `buildOpacity(row) * 0.42`,
              which MULTIPLIES two independent claims: `buildOpacity` says whether
              a row can be ranked against its neighbours, and the 0.42 says
              measured-versus-projected. On a row that is both incomparable and
              improved the product is 0.45 x 0.42 = 0.189, which is invisible on
              this surface (#61). Texture is orthogonal to opacity, so the two
              claims stop multiplying -- and the wash is orthogonal to both: it is
              a lighter mix of the same hue with no texture, while the hatch is
              full-strength stripes. The tooltip, the table twin and the caption
              say all three in words. */}
          <Bar
            dataKey="bossPart"
            stackId="dps"
            barSize={16}
            isAnimationActive={false}
            onClick={openRow}
          >
            {rows.map((row) => (
              <Cell
                key={row.id}
                cursor="pointer"
                fill={classColor(row.build.class)}
                // Faded, not hidden and not recoloured: the bar is a real
                // measurement, and dropping it would recreate exactly the failure
                // the coverage panel exists to prevent. Class colour stays the
                // identity channel; the axis tick carries the words.
                fillOpacity={buildOpacity(row.build)}
              />
            ))}
          </Bar>
          <Bar
            dataKey="restPart"
            stackId="dps"
            barSize={16}
            isAnimationActive={false}
            onClick={openRow}
          >
            {/* The wash must not multiply with the comparability fade: 0.32
                alpha x buildOpacity 0.45 is 0.144 effective, below the 0.189
                this file's own #61 measurement records as invisible on this
                surface -- the flagged rows' remainder (and with it the bar's
                total length) would vanish. So the fade is carried by stepping
                the wash's own mix down one legible notch instead, and the
                cell's fillOpacity stays 1. The wash is a data mark here (its
                edge delimits simc's measured total), which classWash's other
                call sites are not; the solid|wash boundary and the table twin
                carry the reading, per the caption. */}
            {rows.map((row) => (
              <Cell
                key={row.id}
                cursor="pointer"
                fill={classWash(row.build.class, buildOpacity(row.build) < 1 ? 20 : 32)}
              />
            ))}
          </Bar>
          <Bar
            dataKey="solidPart"
            stackId="dps"
            barSize={16}
            isAnimationActive={false}
            onClick={openRow}
          >
            {rows.map((row) => (
              <Cell
                key={row.id}
                cursor="pointer"
                fill={classColor(row.build.class)}
                fillOpacity={buildOpacity(row.build)}
              />
            ))}
          </Bar>
          <Bar
            dataKey="uplift"
            stackId="dps"
            radius={[0, 4, 4, 0]}
            barSize={16}
            isAnimationActive={false}
            onClick={openRow}
          >
            {rows.map((row) => (
              <Cell
                key={row.id}
                cursor="pointer"
                fill={`url(#${hatchId(row.build.class)})`}
                fillOpacity={buildOpacity(row.build)}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      {hasSplit ? (
        <p className="mt-1 px-2 text-[11.5px] text-ink-muted">
          Solid&nbsp;= on the priority target (measured) · washed&nbsp;= the
          rest of the damage (measured)
          {sortBy === "total"
            ? " · hatched = projected computed-build gain"
            : ""}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The svg id of one class's hatch pattern.
 *
 * Slugged because a class name carries a space ("Death Knight") and an id with a
 * space in it is not addressable by `url(#...)` -- the fill silently resolves to
 * nothing and the segment disappears, which is the same invisible failure this
 * whole change is repairing.
 */
function hatchId(wowClass: string): string {
  return `dps-hatch-${wowClass.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function RankingTable({
  rows,
  best,
  bestBoss,
  sortBy,
  hasSplit,
  onOpenSpec,
}: {
  rows: Row[];
  best: number;
  bestBoss: number;
  sortBy: SortMetric;
  hasSplit: boolean;
  onOpenSpec: (id: string) => void;
}) {
  return (
    <div className="overflow-x-auto px-1 pb-2">
      <table className="w-full min-w-[640px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="px-4 py-2.5 font-medium">
              #
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Build
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              DPS
            </th>
            {hasSplit ? (
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                On the boss
              </th>
            ) : null}
            <th scope="col" className="px-4 py-2.5 font-medium">
              Build shown
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              vs top
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              Funnel gain at 5T
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={row.id}
              className="border-b border-hairline/60 last:border-0"
            >
              <td className="tnum px-4 py-2 text-ink-muted">{index + 1}</td>
              <td className="px-4 py-2">
                <button
                  type="button"
                  onClick={() => onOpenSpec(row.id)}
                  className="text-left hover:underline"
                >
                  <BuildIdentity build={row.build} />
                </button>
              </td>
              <td className="tnum px-4 py-2 text-right font-medium text-ink">
                {fullNumber(row.dps)}
              </td>
              {hasSplit ? (
                <td className="tnum px-4 py-2 text-right text-ink-secondary">
                  {row.priorityDps !== undefined ? (
                    fullNumber(row.priorityDps)
                  ) : (
                    <span className="text-ink-muted">—</span>
                  )}
                </td>
              ) : null}
              {/* simc's own figure stays on screen wherever a computed build
                  replaced it, so the substitution can always be undone by a
                  reader. That is the difference between showing the better
                  build and showing it silently. */}
              <td className="px-4 py-2 text-ink-secondary">
                {row.best.projected ? (
                  <>
                    <span className="inline-flex items-center rounded-full border border-hairline px-1.5 py-px text-[11px] whitespace-nowrap text-ink-secondary">
                      {row.mark}
                    </span>
                    <span className="mt-0.5 block text-[11.5px] text-ink-muted">
                      simc&rsquo;s own build:{" "}
                      <span className="tnum">{fullNumber(row.simcDps)}</span>{" "}
                      &middot; margin on{" "}
                      {row.best.marginBasis === "shipped-gear"
                        ? "simc's own gear"
                        : "one normalised kit"}
                    </span>
                    {/* The other axis of the trade (#99): what the marked build
                        does on the boss, measured in the anchored run. Only
                        drawn where this (scenario, targets) has a real split --
                        on a single-enemy view the document's priorityDps equals
                        dps and the "ratio" would be the anchor artefact wearing
                        a boss label. Disclosure without a verdict: the run
                        publishes no error for the priority figures, so no tie
                        band is claimed. */}
                    {hasSplit && row.best.priorityGain !== null ? (
                      <span className="mt-0.5 block text-[11.5px] text-ink-muted">
                        on the boss:{" "}
                        <span className="tnum">
                          {row.best.priorityGain >= 0 ? "+" : ""}
                          {(row.best.priorityGain * 100).toFixed(2)}%
                        </span>{" "}
                        (measured on the normalised kit)
                      </span>
                    ) : null}
                  </>
                ) : (
                  <span className="text-[11.5px] text-ink-muted">
                    SimulationCraft&rsquo;s build
                  </span>
                )}
              </td>
              {/* One metric per column: under the boss sort a row without a
                  measured split gets a dash rather than a total-based percent
                  that would sit unlabelled among boss-based ones. */}
              <td className="tnum px-4 py-2 text-right text-ink-secondary">
                {sortBy === "boss"
                  ? row.priorityDps !== undefined
                    ? percent(bestBoss > 0 ? row.priorityDps / bestBoss : 0, 0)
                    : "—"
                  : percent(best > 0 ? row.dps / best : 0, 0)}
              </td>
              <td className="px-4 py-2 text-right text-ink-secondary">
                {row.funnelGain !== undefined ? (
                  <span className="tnum">
                    {row.funnelGain.toFixed(2)}x
                    <span className="ml-1.5 text-ink-muted">
                      {describeFunnelGain(row.funnelGain)}
                    </span>
                  </span>
                ) : (
                  <span className="text-ink-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
