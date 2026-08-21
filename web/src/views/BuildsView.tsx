/**
 * Build comparison: within each spec, which hero-talent build is ahead, and where
 * that changes.
 *
 * Two rankings over the same runs, kept as separate modes rather than two axes on
 * one chart:
 *  - total damage, which is the question "what tops the meter"
 *  - damage on the main target, which is the question "what kills the boss fastest
 *    while the rest of the pack is up" -- the funnel reading of a build choice
 *
 * They disagree often, and that disagreement is the point of the view. So is the
 * crossover: a build that wins on one target and loses on eight is two different
 * recommendations, and a single headline number hides exactly that.
 *
 * This view used to ask for a spec before it would show anything. It now shows
 * every spec that has more than one build, as a small multiple each -- the
 * comparison is only ever two or three lines wide, so the honest form is a grid
 * of them rather than one plot behind a picker. The table at the bottom is the
 * whole tier's answer in one place.
 *
 * Two builds of one spec share a class colour *and* a spec icon, because they are
 * the same class and the same spec. What separates them is the hero-talent tree:
 * its emblem, its name written out, and a stroke dash on the chart.
 *
 * Honesty constraint specific to this view: simc's shipped builds for one spec
 * differ in gear as well as talents (verified -- MID2 Arcane's two builds carry
 * different rings and noticeably different secondaries). So a gap here means "this
 * build the way simc plays it", not "these talents are worth this much". The note
 * says so, and a margin inside the two runs' combined sampling error is reported as
 * a tie rather than as a winner.
 */

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AXIS_LINE,
  AXIS_TICK,
  CURSOR_LINE,
  GRID,
  TooltipCard,
} from "../components/chart";
import {
  HeroTreeBadge,
  SpecIcon,
  UnvalidatedMark,
  buildName,
  heroLabel,
} from "../components/BuildIdentity";
import {
  EmptyState,
  Note,
  Panel,
  PanelHeader,
  SegmentedControl,
  StatTile,
  cx,
} from "../components/ui";
import { compactNumber, fullNumber, percent } from "../lib/format";
import { buildDash, classColor } from "../lib/palette";
import type {
  Cell,
  ScenarioMeta,
  SpecDetail,
  SpecSummary,
  TalentDataset,
  TalentSpec,
} from "../lib/types";

type Metric = "dps" | "priority";

export function BuildsView({
  details,
  scenario,
  talents,
}: {
  details: SpecDetail[];
  scenario: ScenarioMeta;
  /** Optional: exists once `wowdps talents` has been run for the tier. */
  talents?: TalentDataset | null;
}) {
  const [metric, setMetric] = useState<Metric>("dps");
  const sweeps = scenario.sweepsTargets ?? false;

  // Only specs simc ships more than one build for have anything to compare.
  const groups = useMemo(() => {
    const byId = new Map<string, SpecDetail[]>();
    for (const detail of details) {
      const bucket = byId.get(detail.specId);
      if (bucket) bucket.push(detail);
      else byId.set(detail.specId, [detail]);
    }
    return [...byId.values()]
      .filter((builds) => builds.length > 1)
      .sort((a, b) => buildName(a[0]!).localeCompare(buildName(b[0]!)));
  }, [details]);

  const summaries = useMemo(
    () => groups.map((builds) => summarise(builds, scenario.id, metric)),
    [groups, scenario.id, metric],
  );
  const withData = summaries.filter((entry) => entry.leads.length > 0);

  if (details.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Build comparison" />
        <EmptyState>
          No per-build data has been generated for this tier yet.
        </EmptyState>
      </Panel>
    );
  }

  if (withData.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Build comparison" />
        <EmptyState>
          {metric === "priority"
            ? `${scenario.label} reports no main-target damage to rank by — priority damage only exists once there is more than one enemy.`
            : `No ${scenario.label} data for these builds yet.`}
        </EmptyState>
      </Panel>
    );
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Which hero-talent build leads"
          subtitle={
            metric === "dps"
              ? "Every spec SimulationCraft ships more than one build for, ranked by total damage per second."
              : "Every spec SimulationCraft ships more than one build for, ranked by damage landing on the main target — which build kills the boss fastest while the rest of the pack is up."
          }
          actions={
            <SegmentedControl
              label="Rank by"
              value={metric}
              onChange={setMetric}
              options={[
                { value: "dps", label: "Total DPS" },
                { value: "priority", label: "Boss DPS" },
              ]}
            />
          }
        />
        <LeadTable summaries={withData} sweeps={sweeps} />
        <Note>
          A margin shown as a tie is smaller than the two runs’ combined
          sampling error, so the ranking at that target count is not evidence of
          anything. These are SimulationCraft’s own recommended builds, which
          differ in gear as well as talents — read a gap as “this build the way
          simc plays it”, not as the value of the talents alone.
        </Note>
      </Panel>

      <GearHeldStill talents={talents} />

      {sweeps ? (
        <Panel>
          <PanelHeader
            title="The whole sweep, spec by spec"
            subtitle="One panel per spec. Both builds wear the class colour, because they are the same class — the hero tree, named under each panel, is what tells them apart, and the line dash follows it."
          />
          <div className="grid gap-4 px-5 pb-5 lg:grid-cols-2 2xl:grid-cols-3">
            {withData.map((entry) => (
              <SpecPanel
                key={entry.specId}
                summary={entry}
                scenarioId={scenario.id}
                metric={metric}
              />
            ))}
          </div>
        </Panel>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------------
// The same builds, gear held still
// --------------------------------------------------------------------------------

/**
 * The answer to the caveat under the table above.
 *
 * Everything on this view so far compares SimulationCraft's *shipped* builds, which
 * differ in gear as well as talents — so a gap there is "this build the way simc
 * plays it". This panel puts every build of a spec on one character's gear and
 * action list, so the only difference left is the talent hash.
 *
 * Both rankings come out of one run, and the pair is the point: a build can do the
 * most damage while a different one puts the most on the boss. Presented as a table
 * rather than a chart because the reading is categorical — which build wins — and a
 * bar pair per spec would be a chart of two numbers per row saying what the row says.
 */
function GearHeldStill({ talents }: { talents?: TalentDataset | null }) {
  const counts = useMemo(
    () =>
      [...new Set((talents?.specs ?? []).map((entry) => entry.targets))].sort(
        (a, b) => a - b,
      ),
    [talents],
  );
  const [targets, setTargets] = useState<number | null>(null);
  const active =
    targets !== null && counts.includes(targets)
      ? targets
      : (counts[0] ?? null);

  if (!talents || counts.length === 0 || active === null) return null;

  const rows = talents.specs
    .filter((entry) => entry.targets === active)
    .slice()
    .sort(
      (a, b) =>
        Number(b.rankingsDisagree) - Number(a.rankingsDisagree) ||
        a.label.localeCompare(b.label),
    );
  const disagreeing = rows.filter((entry) => entry.rankingsDisagree);

  return (
    <Panel>
      <PanelHeader
        title="The same builds, with the gear held still"
        subtitle="One character’s gear and action list, wearing each build’s talents in turn — so the only difference left is the talents. Ranked by total damage and by damage on the boss, out of one run."
        actions={
          counts.length > 1 ? (
            <SegmentedControl
              label="Targets"
              value={String(active)}
              onChange={(value) => setTargets(Number(value))}
              options={counts.map((count) => ({
                value: String(count),
                label: `${count}T`,
              }))}
            />
          ) : null
        }
      />
      <div className="grid gap-3 px-5 pb-4 sm:grid-cols-2">
        <StatTile
          label="Specs with more than one build"
          value={String(rows.length)}
          caption={`Measured at ${active} target${active === 1 ? "" : "s"}, ${talents.settings.iterations} deterministic iterations each.`}
        />
        <StatTile
          label="Where the two rankings disagree"
          value={String(disagreeing.length)}
          caption={
            disagreeing.length
              ? "The build that does the most damage is not the build that puts the most on the boss. Picking off a damage meter is the wrong call on these."
              : "Every spec puts the same build top on both metrics at this target count."
          }
        />
      </div>
      <TalentTable rows={rows} />
      <Note>
        {talents.note} Gear is held at whichever build sorts first, named in the
        table: that moves the absolute numbers and not the comparison. At one
        target the two columns are identical by construction — everything lands
        on the only enemy there is.
      </Note>
    </Panel>
  );
}

function TalentTable({ rows }: { rows: TalentSpec[] }) {
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[720px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">
              Spec
            </th>
            <th scope="col" className="py-2.5 pr-4 font-medium">
              Build
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              Total DPS
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              On the boss
            </th>
            <th scope="col" className="py-2.5 pr-5 font-medium">
              Leads
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((spec) =>
            spec.builds
              .slice()
              .sort((a, b) => b.dps - a.dps)
              .map((build, index) => (
                <tr
                  key={`${spec.specId}-${build.id}`}
                  className={cx(
                    "border-b border-hairline/60 last:border-0",
                    spec.rankingsDisagree && "bg-elevated/40",
                  )}
                >
                  <td className="py-2 pr-4 pl-5">
                    {index === 0 ? (
                      <span style={{ color: classColor(spec.class) }}>
                        {spec.label}
                      </span>
                    ) : null}
                  </td>
                  <td className="py-2 pr-4">
                    <HeroTreeBadge
                      build={{
                        class: spec.class,
                        heroTalent: build.heroTalent,
                      }}
                    />
                  </td>
                  <td className="tnum py-2 pr-4 text-right text-ink">
                    {fullNumber(build.dps)}
                    <span className="ml-1 text-ink-muted">
                      ±{build.dpsError.toFixed(2)}%
                    </span>
                  </td>
                  <td className="tnum py-2 pr-4 text-right text-ink">
                    {build.priorityDps === null
                      ? "—"
                      : fullNumber(build.priorityDps)}
                  </td>
                  <td className="py-2 pr-5 text-[12.5px] text-ink-secondary">
                    {[
                      spec.bestByDps === build.id ? "total damage" : null,
                      spec.bestByPriorityDps === build.id
                        ? "on the boss"
                        : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </td>
                </tr>
              )),
          )}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------------
// Per-spec panel
// --------------------------------------------------------------------------------

function SpecPanel({
  summary,
  scenarioId,
  metric,
}: {
  summary: SpecSummaryRow;
  scenarioId: string;
  metric: Metric;
}) {
  const { data } = useMemo(
    () => buildSeries(summary.builds, scenarioId, metric),
    [summary.builds, scenarioId, metric],
  );
  const first = summary.builds[0];
  if (!first || data.length === 0) return null;

  return (
    <figure className="rounded-lg border border-hairline px-3 pt-3 pb-2">
      <figcaption className="flex items-center gap-2">
        <SpecIcon build={first} size={20} labelled />
        <span
          className="font-medium"
          style={{ color: classColor(first.class) }}
        >
          {buildName(first)}
        </span>
        <UnvalidatedMark build={first} />
        <span className="ml-auto text-[12px] text-ink-muted">
          {summary.verdict}
        </span>
      </figcaption>

      <ResponsiveContainer width="100%" height={150}>
        <LineChart
          data={data}
          margin={{ top: 10, right: 8, bottom: 4, left: 0 }}
        >
          <CartesianGrid {...GRID} />
          <XAxis
            dataKey="targets"
            tick={AXIS_TICK}
            axisLine={AXIS_LINE}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={AXIS_TICK}
            axisLine={false}
            tickLine={false}
            width={44}
            tickFormatter={(value: number) => compactNumber(value)}
          />
          <Tooltip
            cursor={CURSOR_LINE}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const sorted = [...payload].sort(
                (a, b) => Number(b.value ?? 0) - Number(a.value ?? 0),
              );
              return (
                <TooltipCard
                  title={`${label} ${Number(label) === 1 ? "target" : "targets"}`}
                  subtitle={buildName(first)}
                  rows={sorted.map((entry) => ({
                    id: String(entry.dataKey),
                    label: String(entry.name),
                    color: classColor(first.class),
                    value: fullNumber(Number(entry.value ?? 0)),
                  }))}
                />
              );
            }}
          />
          {summary.builds.map((build, index) => (
            <Line
              key={build.id}
              type="monotone"
              dataKey={build.id}
              name={build.heroTalent}
              stroke={classColor(build.class)}
              strokeWidth={2}
              strokeDasharray={buildDash(index)}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface-1)" }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>

      <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
        {summary.builds.map((build, index) => (
          <li key={build.id} className="flex items-center gap-1.5">
            <svg width="18" height="8" aria-hidden className="shrink-0">
              <line
                x1="0"
                y1="4"
                x2="18"
                y2="4"
                stroke={classColor(build.class)}
                strokeWidth="2"
                strokeDasharray={buildDash(index)}
              />
            </svg>
            <HeroTreeBadge build={build} />
          </li>
        ))}
      </ul>
    </figure>
  );
}

// --------------------------------------------------------------------------------
// The tier-wide table
// --------------------------------------------------------------------------------

interface Lead {
  targets: number;
  winnerId: string;
  winnerLabel: string;
  winnerValue: number;
  runnerUpLabel: string;
  /** Fractional lead of the winner over the runner-up. */
  margin: number;
  /** The two means' standard errors added in quadrature, as a fraction. */
  noise: number;
}

interface SpecSummaryRow {
  specId: string;
  builds: SpecDetail[];
  leads: Lead[];
  verdict: string;
}

function summarise(
  builds: SpecDetail[],
  scenarioId: string,
  metric: Metric,
): SpecSummaryRow {
  const leads = buildLeads(builds, scenarioId, metric);
  const decisive = leads.filter((lead) => lead.margin > lead.noise);
  const first = decisive[0];
  const flip = first
    ? decisive.find((lead) => lead.winnerId !== first.winnerId)
    : undefined;
  const verdict = !first
    ? "Too close to call anywhere"
    : flip
      ? `Lead changes at ${flip.targets} targets`
      : "One build ahead throughout";
  return { specId: builds[0]?.specId ?? "", builds, leads, verdict };
}

function LeadTable({
  summaries,
  sweeps,
}: {
  summaries: SpecSummaryRow[];
  sweeps: boolean;
}) {
  const columns = sweeps ? [1, 5, 10] : [summaries[0]?.leads[0]?.targets ?? 1];
  return (
    <div className="overflow-x-auto pb-2">
      <table className="w-full min-w-[760px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-hairline text-left text-[11.5px] tracking-wide text-ink-muted uppercase">
            <th scope="col" className="py-2.5 pr-4 pl-5 font-medium">
              Spec
            </th>
            {columns.map((count) => (
              <th key={count} scope="col" className="py-2.5 pr-4 font-medium">
                {count === 1 ? "Single target" : `${count} targets`}
              </th>
            ))}
            <th scope="col" className="py-2.5 pr-5 font-medium">
              Crossover
            </th>
          </tr>
        </thead>
        <tbody>
          {summaries.map((entry) => {
            const first = entry.builds[0];
            if (!first) return null;
            return (
              <tr
                key={entry.specId}
                className="border-b border-hairline/60 last:border-0"
              >
                <td className="py-2 pr-4 pl-5">
                  <span className="inline-flex items-center gap-2">
                    <SpecIcon build={first} size={18} labelled />
                    <span
                      className="font-medium"
                      style={{ color: classColor(first.class) }}
                    >
                      {buildName(first)}
                    </span>
                    <UnvalidatedMark build={first} />
                  </span>
                </td>
                {columns.map((count) => {
                  const lead = entry.leads.find(
                    (item) => item.targets === count,
                  );
                  if (!lead) {
                    return (
                      <td key={count} className="py-2 pr-4 text-ink-muted">
                        —
                      </td>
                    );
                  }
                  const tie = lead.margin <= lead.noise;
                  const winner = entry.builds.find(
                    (build) => build.id === lead.winnerId,
                  );
                  return (
                    <td key={count} className="py-2 pr-4">
                      {tie ? (
                        <span className="text-ink-muted">
                          tie — {heroLabel(lead.winnerLabel)} and{" "}
                          {heroLabel(lead.runnerUpLabel)} inside each other’s
                          error
                        </span>
                      ) : (
                        <span className="flex flex-col leading-tight">
                          {winner ? (
                            <HeroTreeBadge build={winner} />
                          ) : (
                            <span className="text-ink">
                              {heroLabel(lead.winnerLabel)}
                            </span>
                          )}
                          <span className="tnum text-[11.5px] text-ink-muted">
                            +{percent(lead.margin)} over{" "}
                            {heroLabel(lead.runnerUpLabel)}
                          </span>
                        </span>
                      )}
                    </td>
                  );
                })}
                <td className="py-2 pr-5 text-ink-secondary">
                  {entry.verdict}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------------
// Shaping
// --------------------------------------------------------------------------------

interface SpecGroup {
  specId: string;
  label: string;
  builds: SpecSummary[];
}

/** Spec rows regrouped as "one spec, its builds", which is how this view reads them. */
export function groupBySpec(specs: SpecSummary[]): SpecGroup[] {
  const byId = new Map<string, SpecGroup>();
  for (const spec of specs) {
    let group = byId.get(spec.specId);
    if (!group) {
      group = {
        specId: spec.specId,
        label: `${spec.spec} ${spec.class}`,
        builds: [],
      };
      byId.set(spec.specId, group);
    }
    group.builds.push(spec);
  }
  return [...byId.values()].sort(
    (a, b) =>
      b.builds.length - a.builds.length || a.label.localeCompare(b.label),
  );
}

function valueOf(cell: Cell, metric: Metric): number | undefined {
  return metric === "dps" ? cell.dps : cell.priorityDps;
}

function buildSeries(
  details: SpecDetail[],
  scenarioId: string,
  metric: Metric,
) {
  const byTargets = new Map<number, Record<string, number | string>>();
  for (const detail of details) {
    for (const cell of detail.scenarios[scenarioId]?.targets ?? []) {
      const value = valueOf(cell, metric);
      if (value === undefined) continue;
      let row = byTargets.get(cell.targets);
      if (!row) {
        row = { targets: cell.targets };
        byTargets.set(cell.targets, row);
      }
      row[detail.id] = value;
    }
  }
  const data = [...byTargets.values()].sort(
    (a, b) => Number(a.targets) - Number(b.targets),
  );
  return { data };
}

function buildLeads(
  details: SpecDetail[],
  scenarioId: string,
  metric: Metric,
): Lead[] {
  const targets = new Set<number>();
  for (const detail of details) {
    for (const cell of detail.scenarios[scenarioId]?.targets ?? [])
      targets.add(cell.targets);
  }

  const leads: Lead[] = [];
  for (const count of [...targets].sort((a, b) => a - b)) {
    const entries = details
      .map((detail) => {
        const cell = detail.scenarios[scenarioId]?.targets.find(
          (candidate) => candidate.targets === count,
        );
        const value = cell ? valueOf(cell, metric) : undefined;
        return cell && value !== undefined
          ? {
              id: detail.id,
              label: detail.heroTalent,
              value,
              error: cell.dpsError / 100,
            }
          : null;
      })
      .filter((entry): entry is NonNullable<typeof entry> => entry !== null)
      .sort((a, b) => b.value - a.value);

    const [winner, runnerUp] = entries;
    if (!winner || !runnerUp) continue;
    leads.push({
      targets: count,
      winnerId: winner.id,
      winnerLabel: winner.label,
      winnerValue: winner.value,
      runnerUpLabel: runnerUp.label,
      margin: winner.value / runnerUp.value - 1,
      noise: Math.hypot(winner.error, runnerUp.error),
    });
  }
  return leads;
}
