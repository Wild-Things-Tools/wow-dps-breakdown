/**
 * Ranking view: every spec at one scenario and target count.
 *
 * Reads only the manifest, so it renders without fetching per-spec files. That
 * is why the target count is restricted to the four the manifest summarises.
 *
 * Nothing is selected here and nothing ever was -- this view is the precedent
 * the rest of the site was rebuilt against. Every build in the tier, sorted,
 * with its class colour, its spec icon and its hero tree written out.
 */

import { useMemo, useState } from "react";
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
import { classColor } from "../lib/palette";
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

const SUMMARY_TARGETS = [1, 3, 5, 10];

/** Two lines of tick text plus the icon need more room than a bare name. */
const ROW_HEIGHT = 32;
const TICK_WIDTH = 205;

type Mode = "chart" | "table";

interface Row {
  id: string;
  label: string;
  build: SpecSummary;
  /**
   * The value the row is ranked and drawn by: simc's own measurement, unless a
   * computed build beat it outside the tie band. See `lib/bestBuild.ts` -- a
   * marked row carries a projection, never a measurement, and `best.simcDps`
   * keeps the measured figure beside it.
   */
  dps: number;
  /** Which build won, and what that costs. Never null. */
  best: BestBuild;
  /** The part of the bar that is simc's own measurement. */
  simcDps: number;
  /** The projected remainder, stacked on top of it. Zero on an unmarked row. */
  uplift: number;
  /** "computed +2.20%", or null on a row simc still owns. */
  mark: string | null;
  funnelGain?: number;
  priorityShare?: number;
}

export function OverviewView({
  manifest,
  scenario,
  specIndex,
  computedBuilds,
  computedSettled,
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
  onScenarioChange: (id: string) => void;
  onOpenSpec: (id: string) => void;
}) {
  const [targets, setTargets] = useState(1);
  const [mode, setMode] = useState<Mode>("chart");

  const available = useMemo(
    () =>
      SUMMARY_TARGETS.filter((count) => scenario.targetCounts.includes(count)),
    [scenario],
  );
  const effectiveTargets = available.includes(targets)
    ? targets
    : (available[0] ?? 1);

  const rows = useMemo(
    () =>
      buildRows(manifest.specs, scenario.id, effectiveTargets, computedBuilds),
    [manifest.specs, scenario.id, effectiveTargets, computedBuilds],
  );

  const scope: ComputedScope = computedScope(
    computedBuilds,
    scenario.id,
    effectiveTargets,
  );
  const computedRows = rows.filter((row) => row.best.projected).length;

  const best = rows[0]?.dps ?? 0;

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
                value={scenario.id}
                onChange={onScenarioChange}
                options={manifest.scenarios.map((entry) => ({
                  value: entry.id,
                  label: entry.label,
                }))}
              />
              {available.length > 1 ? (
                <Select
                  label="Targets"
                  value={effectiveTargets}
                  onChange={setTargets}
                  options={available.map((count) => ({
                    value: count,
                    label: String(count),
                  }))}
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
          <RankingChart rows={rows} best={best} onOpenSpec={onOpenSpec} />
        ) : (
          <RankingTable rows={rows} best={best} onOpenSpec={onOpenSpec} />
        )}

        <Note>
          Simulated damage per second against a stationary target with no
          external buffs, using SimulationCraft's own tier profiles. Treat gaps
          under a few percent as a tie — the sampling error alone is around{" "}
          {samplingError(manifest.settings)}. Bars carry each build's class
          colour; the icon and the name beside it are what identify it.
          {/* Three sentences, not one. "No talent search has run at this target
              count" and "a search ran and simc's builds all held" are different
              claims, and `computed-builds.json` covers Patchwerk at one target
              only today. Collapsing them would publish a finding nobody made. */}
          {scope === "searched" && computedRows > 0 ? (
            <>
              {" "}
              <strong className="font-medium text-ink-secondary">
                Ranked by the best build known for each row.
              </strong>{" "}
              {computedRows} {computedRows === 1 ? "build is" : "builds are"}{" "}
              ranked by talents this project computed rather than the ones
              SimulationCraft ships, because they beat simc&rsquo;s by more than
              the two runs&rsquo; combined sampling error. Those bars carry a
              paler segment at the end &mdash; that segment is the gain, and it
              is a projection rather than a measurement: the gain was measured
              with both builds on one normalised kit, then carried forward onto
              simc&rsquo;s own published figure, which the table beside this
              chart prints next to it. Every other row is
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
    rows.push({
      id: spec.id,
      label: spec.displayName,
      build: spec,
      dps: best.rankDps,
      best,
      simcDps: best.simcDps,
      uplift: best.rankDps - best.simcDps,
      mark: bestBuildMark(best),
      funnelGain: entry?.funnelGain,
      priorityShare: entry?.priorityShare,
    });
  }
  // Ranked by the best build known for each row -- the whole reason this view
  // carries the computed document. Rows nobody computed a build for are ranked
  // by simc's number, unchanged.
  rows.sort((a, b) => b.dps - a.dps);
  return rows;
}

function RankingChart({
  rows,
  best,
  onOpenSpec,
}: {
  rows: Row[];
  best: number;
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

  return (
    <div className="px-2 py-4">
      <ResponsiveContainer width="100%" height={height}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 56, bottom: 4, left: 8 }}
        >
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
                  subtitle={`${percent(row.dps / best, 0)} of the top build`}
                  rows={[
                    {
                      id: "dps",
                      label: row.best.projected ? "DPS, best build" : "DPS",
                      color: classColor(row.build.class),
                      value: fullNumber(row.dps),
                      ...(row.best.projected
                        ? {
                            hint: "simc's own figure carried forward by the measured talent gain — not a run of its own",
                          }
                        : {}),
                    },
                    ...(row.best.projected
                      ? [
                          {
                            id: "simc",
                            label: "SimulationCraft's own build",
                            value: fullNumber(row.simcDps),
                            hint: `${row.mark}, measured with both builds on one normalised kit`,
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

              Two stacked segments rather than one, because the two halves are not
              the same kind of number. The solid part is simc's own measurement;
              the paler part is the projected talent gain, which nobody has
              simulated on this gear. A single solid bar would present the sum as
              one measured figure -- the whole failure `lib/bestBuild.ts` exists to
              prevent, restated in pixels. It is never the only channel: the row's
              tooltip prints both numbers, the table twin repeats them, and the
              note under the chart says it in words. */}
          <Bar
            dataKey="simcDps"
            stackId="dps"
            barSize={16}
            isAnimationActive={false}
            onClick={(entry: unknown) => {
              const row = (entry as { payload?: Row } | undefined)?.payload;
              if (row) onOpenSpec(row.id);
            }}
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
            dataKey="uplift"
            stackId="dps"
            radius={[0, 4, 4, 0]}
            barSize={16}
            isAnimationActive={false}
            onClick={(entry: unknown) => {
              const row = (entry as { payload?: Row } | undefined)?.payload;
              if (row) onOpenSpec(row.id);
            }}
          >
            {rows.map((row) => (
              <Cell
                key={row.id}
                cursor="pointer"
                fill={classColor(row.build.class)}
                fillOpacity={buildOpacity(row.build) * 0.42}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function RankingTable({
  rows,
  best,
  onOpenSpec,
}: {
  rows: Row[];
  best: number;
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
                      <span className="tnum">{fullNumber(row.simcDps)}</span>
                    </span>
                  </>
                ) : (
                  <span className="text-[11.5px] text-ink-muted">
                    SimulationCraft&rsquo;s build
                  </span>
                )}
              </td>
              <td className="tnum px-4 py-2 text-right text-ink-secondary">
                {percent(row.dps / best, 0)}
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
