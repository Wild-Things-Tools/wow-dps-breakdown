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
import type {
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
  dps: number;
  funnelGain?: number;
  priorityShare?: number;
}

export function OverviewView({
  manifest,
  scenario,
  specIndex,
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
    () => buildRows(manifest.specs, scenario.id, effectiveTargets),
    [manifest.specs, scenario.id, effectiveTargets],
  );

  const best = rows[0]?.dps ?? 0;

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
          {rows.some((row) => row.build.gearComparable === false) ? (
            <>
              {" "}
              <strong className="font-medium text-ink-secondary">
                Faded bars wear gear at a different item level from the tier's.
              </strong>{" "}
              Absolute damage does not survive that difference, so where those
              builds sit against the rest is partly gear rather than spec — each
              one&rsquo;s own page states the gap. They are drawn rather than
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
): Row[] {
  const rows: Row[] = [];
  for (const spec of specs) {
    const entry = spec.scenarios[scenarioId];
    const dps = entry?.dps[String(targets)];
    if (typeof dps !== "number") continue;
    rows.push({
      id: spec.id,
      label: spec.displayName,
      build: spec,
      dps,
      funnelGain: entry?.funnelGain,
      priorityShare: entry?.priorityShare,
    });
  }
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
                      label: "DPS",
                      color: classColor(row.build.class),
                      value: fullNumber(row.dps),
                    },
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
              name on the axis carry which build. */}
          <Bar
            dataKey="dps"
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
                // Faded, not hidden and not recoloured: the bar is a real
                // measurement, and dropping it would recreate exactly the failure
                // the coverage panel exists to prevent. Class colour stays the
                // identity channel; the axis tick carries the words.
                fillOpacity={buildOpacity(row.build)}
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
