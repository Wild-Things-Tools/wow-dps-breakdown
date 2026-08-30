/** Shape of the JSON the pipeline writes. Mirrors pipeline/src/wowdps/dataset.py. */

export interface Ability {
  name: string;
  id?: number;
  /** Fraction of the spec's total damage, 0..1. Shares across a cell sum to 1. */
  share: number;
  executes: number;
}

export interface Cell {
  targets: number;
  dps: number;
  /** Standard error of the DPS mean, in percent. */
  dpsError: number;
  dpsStddev: number;
  iterations: number;
  fightLength: number;
  abilities: Ability[];
  /** Damage per second landing on the main target. Absent at one target. */
  priorityDps?: number;
  /** priorityDps / dps, i.e. the fraction landing on the main target. */
  priorityShare?: number;
  /**
   * priorityShare x targets. 1.0 = damage spread evenly across all targets;
   * N = every point of damage lands on the main target.
   *
   * Distribution only. A spec with no area damage scores N here without the extra
   * targets having helped it at all — for that question use funnelGain.
   */
  concentration?: number;
  /**
   * priorityDps at N targets / dps at one target. Above 1.0 the extra targets are
   * actively feeding the main target (resources from damage-over-time effects,
   * procs); below 1.0 the global cooldowns spent on area damage cost it damage.
   * This is what "funnel" means in play.
   */
  funnelGain?: number;
  /** Mean damage per second, one value per timelineBin seconds. */
  timeline?: number[];
  timelineBin?: number;
  /** Peak 20s window DPS relative to the fight average. 1.0 = perfectly flat. */
  burstRatio?: number;
}

export interface ScenarioCells {
  targets: Cell[];
}

export interface SpecDetail {
  id: string;
  class: string;
  spec: string;
  heroTalent: string;
  specId: string;
  displayName: string;
  role: string;
  talentHash: string | null;
  /**
   * True when this build came from a profile simc wrote into its generator and left
   * commented out -- complete, and not switched on for the tier. Absent, never
   * false, so a tier of shipped profiles produces the bytes it did before this
   * existed. "This is the character we had written down when we stopped" is a
   * different claim from "this is the spec this season", so it is drawn with a
   * label rather than folded into the ranking silently.
   */
  unvalidated?: boolean;
  /**
   * Where this build's *talents* come from, for a profile this project
   * materialised: "repaired" | "harvested" | "computed". Absent on every build
   * simc ships. The evidence sentence is the first entry of `caveats`.
   */
  origin?: string;
  caveats: string[];
  errors: string[];
  scenarios: Record<string, ScenarioCells>;
}

export interface SpecSummaryScenario {
  /**
   * Keyed by target count, one entry per measured cell. The keys are the
   * contract: the overview derives its offered counts from them, which is what
   * lets a boss scenario measured at 2 targets appear at all. Datasets written
   * before 2026-08-29 carry only the counts 1/3/5/10 here and none of the two
   * maps below -- every reader degrades to "no split, no per-count error".
   */
  dps: Record<string, number>;
  /**
   * Damage on the priority target, per count. Absent wherever the cell had a
   * single enemy — simc emits the field only when enemy_targets > 1, and
   * raid-event adds count, so a configured-1-target scenario like Add Waves
   * carries a real split under the key "1" while plain Patchwerk at one
   * target carries none. An entry's absence is "not a defined question",
   * never zero.
   */
  priorityDps?: Record<string, number>;
  /** Per-count standard error in percent, for the tie rule (hypot of two). */
  dpsError?: Record<string, number>;
  /** Measured at five targets. */
  concentration?: number;
  priorityShare?: number;
  funnelGain?: number;
  burstRatio?: number;
}

export interface SpecSummary {
  id: string;
  class: string;
  spec: string;
  heroTalent: string;
  specId: string;
  displayName: string;
  role: string;
  /**
   * The talent build simc ships for this row, as the manifest publishes it.
   *
   * The build's own spec file has always carried it; what this is for is the
   * *join*, and the join needs it here because the ranking reads the manifest
   * and nothing else. A computed row's margin was measured against simc's build
   * as it stood on the day the search ran, and simc repairs its own profiles —
   * so `bestBuildFor` compares the two and withholds the mark when they differ.
   *
   * Optional: absent on every tier built before 2026-08-30, and absent on a
   * profile that states no hash. Absent means the comparison is not made, never
   * that it failed.
   */
  talentHash?: string | null;
  scenarios: Record<string, SpecSummaryScenario>;
  errors?: string[];
  /**
   * False when this build's gear could not be matched to the tier's, so its place in
   * a ranking of absolute DPS is partly gear rather than spec. Absent when it is
   * comparable. simc's disabled profiles are routinely a whole tier behind its
   * shipped ones -- MID2's wear item level 289 against a shipped 334-344 -- which
   * puts every one of them below every shipped build for reasons that have nothing to
   * do with the spec. The full sentence is in the build's own `caveats`.
   */
  gearComparable?: boolean;
  /**
   * False when this build's tier-set state is not the one a strict majority of the
   * tier's shipped profiles wear. Absent when it matches, so a tier that agrees with
   * itself emits nothing -- MID1's shipped profiles all wear four pieces and gain no
   * key at all.
   *
   * **A second flag, never folded into `gearComparable`.** That one means item
   * level; this one means set state, and a build can carry either without the other.
   * MID2's two Arcane Mage builds have this one alone -- their gear sits squarely
   * inside the tier's 334-344 band and they wear no set bonus at all, worth a
   * measured +13.13% (Spellslinger) and +14.42% (Sunfury) at one target. The
   * disabled profiles carry both. One boolean would leave the sentence beside it
   * guessing which it meant, and an item-level bump would close one gap while
   * leaving the other looking fixed.
   *
   * The flag is *symmetric* and says only "not the tier's state": wearing the set
   * where the tier does not is as incomparable as going without where it does, and
   * the tier ships more than two states (simc's own thresholds, not an assumed 2 and
   * 4). Which way round it went for this build is in its `caveats`, never inferred
   * from the boolean.
   */
  tierSetComparable?: boolean;
  /**
   * True when this build came from a profile simc wrote into its generator and left
   * commented out -- complete, and not switched on for the tier. Absent, never
   * false, so a tier of shipped profiles produces the bytes it did before this
   * existed. "This is the character we had written down when we stopped" is a
   * different claim from "this is the spec this season", so it is drawn with a
   * label rather than folded into the ranking silently.
   */
  unvalidated?: boolean;
  /**
   * Where this build's *talents* come from, for a profile this project
   * materialised: "repaired" | "harvested" | "computed". Absent on every build
   * simc ships, so a tier without extra builds produces the bytes it did before
   * this existed. The evidence sentence is in the build's own spec file.
   */
  origin?: string;
}

export interface ScenarioMeta {
  id: string;
  label: string;
  description: string;
  fightStyle: string | null;
  targetCounts: number[];
  maxTime: number;
  supportsFunnel: boolean;
  /**
   * Scenario id whose single-target cell funnel gain divides by. "self" means this
   * scenario's own 1-target run; null means gain is not computable here.
   */
  funnelBaseline?: string | null;
  /** False when the scenario runs at one target count only. */
  sweepsTargets?: boolean;
}

export interface SimcMeta {
  simcVersion?: string;
  buildDate?: string;
  gitRevision?: string;
  gitBranch?: string;
  ptr?: boolean;
  beta?: boolean;
  reportVersion?: string;
  /** The WoW build simc modelled — the patch these numbers reflect. */
  wowVersion?: string;
  wowBuild?: number;
  /** The game-data hotfix date. Balance changes after it are not yet in the data. */
  hotfixDate?: string;
  changes?: SimcChanges;
}

export interface TierMeta {
  /** simc's tier directory name, e.g. "MID2". */
  id: string;
  /** Human form, e.g. "Midnight Season 2". */
  label: string;
  generatedAt: string | null;
  specCount: number;
  simcVersion?: string | null;
}

/**
 * Which tiers the dataset holds, oldest first, and which one is current.
 *
 * A tier is a different game state rather than a filter: the profiles carry
 * different gear, different talents and a different spec list, so the site shows
 * one tier at a time instead of mixing them into a single ranking.
 */
export interface TierIndex {
  current: string;
  tiers: TierMeta[];
}

export interface RunSettings {
  /** Requested standard error in percent. 0 means the run was deterministic instead. */
  targetError: number;
  /** Fixed iteration count in deterministic mode, ceiling in adaptive mode. */
  maxIterations: number;
  /** True when simc ran a fixed, reproducibly seeded iteration count. */
  deterministic?: boolean;
  /**
   * Median standard error actually measured across every cell of the run, in
   * percent. This is the honest figure: in deterministic mode nobody requests an
   * error, so `targetError` is 0 and says nothing about how precise the run is.
   */
  medianDpsError?: number | null;
}

export interface Manifest {
  schemaVersion: number;
  generatedAt: string;
  tier: string;
  simc: SimcMeta;
  settings: RunSettings;
  scenarios: ScenarioMeta[];
  specs: SpecSummary[];
  /** Absent on datasets built before spec coverage was published. */
  coverage?: SpecCoverage;
}

export interface LogsComparison {
  specId: string;
  displayName: string;
  encounterId: number;
  encounterName: string;
  sampleSize: number;
  median: number;
  /** Absent below twenty ranked parses: an extrapolation from the single best one. */
  p95?: number;
  max: number;
  simDps: number;
  logsToSimRatio: number;
}

/**
 * One boss, over every build that had enough ranked parses on it.
 *
 * `rankAgreement` is a rank correlation between the simulated ordering of those
 * builds and the logged one, so +1 is "the sim names the same winners", 0 is "it
 * says nothing" and -1 is "it names them backwards". Null below the sample floor.
 */
export interface LogsBossReading {
  encounterId: number;
  encounterName: string;
  builds: number;
  median: number;
  min: number;
  max: number;
  rankAgreement: number | null;
}

/**
 * One build, across the bosses it was logged on.
 *
 * `vsField` is the build's ratio divided by the median ratio of every build on the
 * same boss, so it says whether real raids cost this build more or less than they
 * cost its peers on the same fight. That is the part of the disagreement that is
 * about the build rather than about the encounter.
 */
export interface LogsBuildReading {
  specId: string;
  displayName: string;
  bosses: number;
  median: number;
  vsField: number | null;
  vsFieldMin: number | null;
  vsFieldMax: number | null;
  /** Median change in rank from the simulated ordering to the logged one. */
  rankMove: number | null;
  sampleSize: number;
}

export interface LogsAnalysis {
  builds: number;
  bosses: LogsBossReading[];
  perBuild: LogsBuildReading[];
  /**
   * Share of the spread in the ratio that goes away once you know which boss (or
   * which build) a row came from. Not eta-squared: medians are subtracted, not
   * means. The point is that the two are computed identically and so comparable.
   */
  varianceExplained: { boss: number | null; build: number | null };
  /** Rank agreement with the bosses pooled — low by construction, published as the
   * number somebody would reach for first. */
  pooledRankAgreement: number | null;
  /** Correlation between a row's parse count and its `vsField`. Near zero means the
   * build ordering is not an artefact of how many people log the build. */
  sampleSizeBias: number | null;
  minRankSample: number;
  minBossSample: number;
}

export interface LogsVerification {
  generatedAt: string;
  metric: string;
  difficulty: number;
  note: string;
  comparisons: LogsComparison[];
  /** Absent on files written before the readings existed. */
  analysis?: LogsAnalysis | null;
  minSampleSize?: number;
  withheldForSmallSample?: number;
}

/**
 * One build measured with the gear held still (<tier>/talents.json).
 *
 * Deliberately *not* the same comparison as the spec rows in the manifest. Those put
 * each build on the gear SimulationCraft's authors picked for it, which answers
 * "which build should I play". This holds one character's gear and action list and
 * swaps only the talent hash, which answers "what do these talents do". Both are
 * right; on MID2 Arcane they differ by about 0.6 points out of 7.
 */
export interface TalentBuild {
  id: string;
  label: string;
  heroTalent: string;
  dps: number;
  dpsError: number;
  /** Damage per second to the priority target. Equals `dps` at one target. */
  priorityDps: number | null;
  iterations: number;
}

export interface TalentSpec {
  specId: string;
  label: string;
  /** The class, for colour and icons. Published rather than derived from `specId`,
   * which would need string surgery that breaks on two-word class names. */
  class: string;
  /** Whose gear and action list every build wore. Moves the absolute numbers, not
   * the comparison, so it is published rather than hidden. */
  baseProfile: string;
  targets: number;
  builds: TalentBuild[];
  bestByDps: string | null;
  bestByPriorityDps: string | null;
  /** The case worth naming: most damage overall is not most damage on the boss. */
  rankingsDisagree: boolean;
  note: string;
}

export interface TalentDataset {
  schemaVersion: number;
  generatedAt: string;
  tier: string;
  settings: { iterations: number; deterministic: boolean };
  note: string;
  specs: TalentSpec[];
}

// --------------------------------------------------------------------------------
// Gear comparison (<tier>/gear.json)
// --------------------------------------------------------------------------------

/**
 * One item level candidates are run at, with the evidence for the number.
 *
 * simc's data does not name Blizzard's upgrade tracks, so the label is a reading of
 * the item level ladder rather than something read out of the game files. The
 * evidence string says which, and the view shows it.
 */
export interface GearItemLevel {
  id: string;
  label: string;
  ilevel: number;
  evidence?: string;
}

export interface GearItemMeta {
  id: number;
  name: string;
  slug: string;
  /** "raid" | "mythicplus". Asserted by the pipeline's pool file, not derived. */
  source: string;
  /** null for trinkets that allocate no primary stat, which anyone can use. */
  primaryStat: string | null;
  secondaryStat?: string;
}

/** One baseline-pool item measured on its own, with every other socket empty. */
export interface GearPoolEntry {
  id: number;
  ilevel: number;
  dps: number;
  dpsError: number;
  /** DPS this item adds over wearing nothing at all in the slot. */
  standaloneGain: number;
  /**
   * The best full set containing this item, when the sweep enumerated them.
   * Absent under the additive rule, and absent from data written before the
   * exhaustive method existed — in both cases `standaloneGain` is the ranking.
   */
  bestCombinationDps?: number | null;
  /** True for the items that became the baseline. */
  chosen: boolean;
}

/** One measured way of filling the slot, as a runner-up to something better. */
export interface GearRunnerUp {
  items: { id: number; ilevel: number }[];
  dps: number;
  /** How far the winner is ahead, as a fraction of this set's DPS. */
  gap: number;
  gapError: number;
  /** A gap inside `gapError` is a tie: the winner did not measurably win. */
  tie: boolean;
}

/**
 * The best the slot can hold at one item level, drawn from the whole pool.
 *
 * Not the same answer as "the baseline plus its best single drop". That is a
 * two-step search — fix the farmed pair, then swap one socket — and it cannot reach
 * a set whose farmed half is not in the best farmed pair.
 */
export interface GearBestSet {
  /** Matches a GearItemLevel id. */
  level: string;
  ilevel: number;
  items: { id: number; ilevel: number }[];
  dps: number;
  dpsError: number;
  /**
   * Fraction of `baselineDps` gained. Zero when the ceiling *is* the baseline.
   *
   * Note the denominator: `baselineDps` is the winning farmed set as the
   * *combination* invocation measured it, while `GearTargetResult.baseline.dps` is
   * the same gear as the *candidate* invocation measured it. Dividing the two
   * published DPS figures gives a third number; `baseline.drift` is how far apart
   * the two runs were.
   */
  gain: number;
  gainError: number;
  /** The reference `gain` was taken against, spelled out rather than implied. */
  baselineDps: number;
  /** The project's tie rule, applied by the pipeline: `|gain| <= gainError`. */
  isTie?: boolean;
  /** True when nothing in the drop pool improves on what the build already wears. */
  isBaseline: boolean;
  runnerUp: GearRunnerUp | null;
}

export interface GearCandidate {
  id: number;
  /** Matches a GearItemLevel id. */
  level: string;
  ilevel: number;
  /** Item id this candidate was put in place of. */
  replaces: number;
  dps: number;
  dpsError: number;
  /** Fraction of baseline DPS gained. Negative means the baseline is better. */
  gain: number;
  /**
   * The baseline's and the candidate's standard errors in quadrature. A gain
   * smaller than this is a tie, not a lead — the same rule the Builds view uses.
   */
  gainError: number;
  priorityDps?: number;
}

export interface GearTargetResult {
  targets: number;
  /** DPS with every socket in the slot empty. The floor the pool is measured from. */
  emptyDps: number;
  baseline: {
    items: number[];
    ilevel: number;
    dps: number;
    dpsError: number;
    /**
     * "exhaustive" — every combination was filled and run, and the baseline is the
     * one that won. "additive" — the pool was too large for the budget, so the top
     * items by standalone value were taken instead. The two disagree on half the
     * tier and the numbers look identical either way, so this is recorded rather
     * than assumed. Absent on data written before it was.
     */
    method?: string;
    /** How many combinations the choice rests on. 0 under the additive rule. */
    combinations?: number;
    /**
     * How far the same gear moved between the two invocations that both measured
     * it, with the two errors in quadrature. Exactly zero on a deterministic run,
     * which is the point — a reader can see it rather than assume it. Non-zero
     * under `--target-error`, and then the ceiling gains and the candidate gains on
     * one page are measured from references this far apart.
     */
    drift?: number;
    driftError?: number;
    /**
     * Which of `items` the candidates displace: the measured weakest of the set.
     * Never "the last one" — that is pool-file order, and reading it that way is
     * the defect the pipeline carried. Absent on data written before it.
     */
    replaces?: number;
    runnerUp?: GearRunnerUp;
  };
  pool: GearPoolEntry[];
  candidates: GearCandidate[];
  /**
   * The ceiling, one entry per item level. Absent when the enumeration was over
   * budget — which is not the same as "the baseline is the ceiling", so it is
   * missing rather than equal to it.
   */
  bestSets?: GearBestSet[];
}

export interface GearSpecResult {
  id: string;
  class: string;
  spec: string;
  heroTalent: string;
  specId: string;
  displayName: string;
  primaryStat: string;
  targets: GearTargetResult[];
  errors?: string[];
}

export interface GearSlot {
  id: string;
  label: string;
  sockets: string[];
  baselineSource: string;
  baselineSourceLabel: string;
  candidateSource: string;
  candidateSourceLabel: string;
  note: string;
  itemLevels: GearItemLevel[];
  items: GearItemMeta[];
  specs: GearSpecResult[];
  /**
   * The run that measured THIS slot (#95). A single-slot re-run merges into the
   * published document, so two slots of one file may name different simc
   * revisions, precisions and coverage -- the document-level blocks cannot say
   * which slot they describe. On a slot whose rows were merged across runs,
   * `coverage.specs` is recounted and `settings.medianDpsError` recomputed from
   * the merged rows; `simc` is the newest contributor's and bounds the newest
   * measurement. Absent on slots last measured before the blocks existed --
   * fall back to the document level, which is then the old, weaker claim.
   */
  simc?: SimcMeta;
  settings?: RunSettings;
  coverage?: { specs: number; specsAvailable: number };
}

export interface GearDataset {
  schemaVersion: number;
  generatedAt: string;
  tier: string;
  /**
   * The newest contributing run's blocks. After a single-slot re-run they
   * describe that run, not every slot -- the per-slot blocks on `GearSlot` are
   * what a reader behind a slot selector must show. `settings.medianDpsError`
   * alone is recomputed by the merge over every row the document holds.
   */
  simc: SimcMeta;
  settings: RunSettings;
  /**
   * The UNION of build ids over all slots -- a claim about the document, never
   * about the slot on screen (#95: the Trinket tab counted 28 over a table of
   * 26). Show `GearSlot.coverage` where a slot is selected.
   */
  coverage: { specs: number; specsAvailable: number };
  slots: GearSlot[];
}

// --------------------------------------------------------------------------------
// Fight shapes per boss (<tier>/fights.json)
// --------------------------------------------------------------------------------

/** A pooled number with the range it was pooled from. Never a bare median. */
export interface FightSpread {
  median: number;
  low: number;
  high: number;
  /** How many fights it was pooled from. */
  n: number;
}

/**
 * One fact about an encounter, and where it came from.
 *
 * `source` is the whole point: `hand` is a person who plays the fight (first-class
 * here — the API cannot tell you what an aura does), `logs` is a probe measurement,
 * and `default` means nothing is known and the value is the project's fallback.
 * A `default` fact must never be rendered as a finding.
 */
export interface FightFact {
  key: string;
  label: string;
  source: "hand" | "logs" | "default";
  sourceLabel: string;
  summary: string;
  detail: string;
  statedBy: string | null;
  observedAt: string | null;
  sample: number | null;
  reports: string[];
  value: unknown;
}

export interface FightAddWave {
  name: string;
  count: number;
  first: number;
  duration: number;
  /** Seconds between waves; null means it happens once. */
  cadence: number | null;
}

export interface FightAmplification {
  ability: string;
  abilityId: number | null;
  multiplier: number;
  first: number;
  duration: number;
  /** "priority" | "add" | "unknown". Only "priority" has a simc equivalent. */
  target: string;
  magnitudeSource: string;
  /**
   * Where `target` came from, separately from the fact's own provenance.
   *
   * An amplification whose magnitude a person stated and whose carrier the logs
   * measured is the normal end state, so one source over the whole thing would
   * misdescribe both halves. Null means nobody has filled it in.
   */
  targetSource: string | null;
  /** Which enemy, in how many pulls, and why it was called priority or add. */
  targetEvidence: string | null;
  /** Always false: no field in the Warcraft Logs API says what an aura does. */
  magnitudeMeasurable: boolean;
  /** False when simc has nothing to express this with. */
  representable: boolean;
}

/** One enemy an aura landed on, pooled across the sampled pulls. */
export interface AuraCarrier {
  name: string;
  gameId: number | null;
  applications: number;
  instances: number;
  seenInFights: number;
  /** "priority" | "add" | "unknown" | "mixed". */
  role: string;
}

/**
 * What the logs could contribute to a fight profile, and what stops them.
 *
 * Published, never applied: writing one into the profile takes an explicit
 * `wowdps fight-promote --write`, and a measurement never overwrites a fact a
 * person asserted. This is the proposal, so the decision can be read before
 * anybody runs the command.
 */
export interface FightPromotion {
  key: string;
  label: string;
  value: unknown;
  summary: string;
  evidence: string;
  sample: number;
  reports: string[];
  eligible: boolean;
  reason: string;
  /** "hand" when a person's statement is what holds it back. */
  blockedBy: string | null;
  current: unknown;
  /** True only for a real contradiction, never for a blank being filled. */
  disagrees: boolean;
}

export interface FightProfileBlock {
  baselineTargets: number;
  /** Null when no target count was asserted — not false. */
  constantTargets: boolean | null;
  fightLengthSeconds: number | null;
  raidSize: number | null;
  addWaves: FightAddWave[];
  amplifications: FightAmplification[];
  phases: Array<Record<string, unknown>>;
}

/** The simc scenario a profile produces, and what did not survive the trip. */
export interface FightScenario {
  encounterId: number;
  name: string;
  targets: number;
  maxTime: number;
  options: string[];
  unrepresented: string[];
  /** Fact keys whose value is somebody's word rather than a measurement. */
  asserted: string[];
  /** Always null: naming a fight style makes simc clear the raid events. */
  fightStyle: string | null;
  /** Target count over time, as [second, count] pairs. */
  steps: Array<[number, number]>;
}

export interface MeasuredAdd {
  key: string | number;
  name: string;
  gameId: number | null;
  seenInFights: number;
  instances: FightSpread | null;
  firstSeen: FightSpread | null;
  lifetime: FightSpread | null;
  cadence: FightSpread | null;
  damageShare: FightSpread | null;
  presentAtPull: boolean;
}

export interface MeasuredAura {
  abilityId: number;
  ability: string;
  seenInFights: number;
  applications: number;
  start: FightSpread | null;
  duration: FightSpread | null;
  distinctTargets: number;
  /** Which enemies carried it. Absent on a run made before this was extracted. */
  carriedBy?: AuraCarrier[];
  /** "priority" | "add" | "unknown" | "mixed" across every carrier. */
  role?: string;
  roleEvidence?: string;
  anyTruncated: boolean;
}

export interface MeasuredPhase {
  id: number;
  name: string;
  isIntermission: boolean;
  start: FightSpread | null;
  duration: FightSpread | null;
  /**
   * How many sampled kills carried this phase at all — not how many windows they
   * carried. A phase recurs within one pull, so the two differ by a factor: on
   * MID2's Entombed Sentinels, two of eight kills contribute four Stage One
   * windows each, and the spreads' `n` of 8 is those windows.
   *
   * Warcraft Logs does not return `phaseTransitions` on every fight, so a low
   * count here beside a high `fightsSampled` is ordinary rather than a defect —
   * and it is the number a reader has to see before treating the timings as
   * evidence about the encounter.
   */
  seenInFights: number;
  /**
   * The window count, i.e. what `start.n` and `duration.n` are computed over.
   * Optional because a document written before this field existed has none, and
   * a reader must not read its absence as zero.
   */
  windows?: number;
}

export interface FightPhaseWindow {
  id: number;
  name: string;
  isIntermission: boolean;
  start: number;
  duration: number;
}

export interface FightAuraWindow {
  abilityId: number;
  ability: string;
  start: number;
  duration: number;
  /** No measured end: the window is a floor, not a value. */
  truncated: boolean;
  instance: number | null;
  /** The enemy this window was on. Absent before the carrier extraction. */
  actorName?: string | null;
  role?: string | null;
}

/** One sampled pull, published whole. Never an average of pulls. */
export interface RepresentativeFight {
  reportCode: string;
  fightId: number;
  kill: boolean;
  durationSeconds: number;
  raidSize: number | null;
  steps: Array<[number, number]>;
  mean: number;
  peak: number;
  peakShare: number;
  constant: boolean;
  /** Every enemy that took a hit, including ones under the significance floor. */
  allEnemySteps: Array<[number, number]>;
  allEnemyPeak: number;
  /** Seconds of this pull the event fetch reached, and that over its length. */
  observed?: number | null;
  coverage?: number | null;
  phases: FightPhaseWindow[];
  auras: FightAuraWindow[];
  truncated: boolean;
  warnings: string[];
}

/** One sampled pull as it is drawn behind the chosen curve. */
export interface ContextPull {
  reportCode: string;
  fightId: number;
  durationSeconds: number;
  kill: boolean;
  steps: Array<[number, number]>;
  coverage?: number | null;
}

/**
 * A shape at least two of the sampled pulls shared.
 *
 * Pulls of one boss are not all the same fight: a wave killed before the next one
 * spawns in half the logs and overlapping in the other half is two patterns, and
 * showing only the pull nearest the median would answer a question nobody asked.
 * The list is ordered by how many pulls each shape holds, so the first is what most
 * of these kills looked like — and a boss whose pulls all agree yields exactly one,
 * which is how the view knows to show no chooser.
 */
export interface FightPattern {
  id: string;
  /** How many sampled pulls had this shape, and that over all of them. */
  pulls: number;
  share: number;
  /** Factual, never an interpretation: "3 targets throughout", "peaks at 7". */
  label: string;
  /**
   * The widest disagreement inside this pattern, as a share of the fight. A pattern
   * held together at 0.19 and one held together at 0.02 are different claims.
   */
  spread: number;
  representative: RepresentativeFight;
  alsoInThisPattern: ContextPull[];
  reportCodes: string[];
  /** Sampled pulls this pattern does not contain. */
  unmatched: ContextPull[];
}

export interface FightTimeline {
  /** "representative" — one real pull, never a per-second average. */
  pooling: string;
  why: string;
  chosenBecause: string;
  /** Absent on files written before pulls were clustered. */
  patterns?: FightPattern[];
  representative: RepresentativeFight;
  others: ContextPull[];
}

/** One point of the aggregate distribution: how many targets were up here, across kills. */
export interface TargetBandPoint {
  /** Normalised fight time, 0..1. */
  t: number;
  /** That time in seconds at the median kill length, for labelling. */
  second: number;
  median: number;
  /** Inter-quartile range across kills. */
  low: number;
  high: number;
  min: number;
  max: number;
}

/**
 * How many targets are up at each point of the fight, across every kill sampled --
 * the direct answer to "how many are normally up, and when". Built from the
 * distribution over kills, not from one representative pull, and only from kills the
 * event fetch read in full.
 */
export interface TargetBand {
  fights: number;
  buckets: number;
  medianLengthSeconds: number;
  band: TargetBandPoint[];
  why: string;
}

export interface MeasuredFight {
  fightsSampled: number;
  reports: string[];
  durationSeconds?: FightSpread | null;
  raidSize?: FightSpread | null;
  playersListed?: FightSpread | null;
  meanTargets?: FightSpread | null;
  peakTargets?: FightSpread | null;
  peakTargetShare?: FightSpread | null;
  activeTimeFraction?: FightSpread | null;
  /** The aggregate distribution of concurrent targets over the fight. */
  targetBand?: TargetBand | null;
  /**
   * How much of each sampled fight the event fetch actually reached.
   *
   * The gate on every count in this block. Enemy damage-taken is paginated and
   * bounded, and a twenty-player Mythic pull outruns the budget, so a fight can
   * be read for a fifth of its length with every count silently averaged over
   * all of it. Absent on a run made before this was measured, which is itself a
   * reason not to trust a whole-fight claim from that run.
   */
  eventCoverage?: FightSpread | null;
  /**
   * When the sampled kills happened. `--order first` takes the earliest kills
   * *among the ranking pages gathered*, and Warcraft Logs sorts those by damage —
   * so a slow first-night kill can sit past the window and never be seen, while
   * the sample is still truthfully "the earliest ones we saw". These dates are
   * what let a reader tell the two apart. Absent on payloads written before the
   * kill time was carried through.
   */
  killedBetween?: { first: string; last: string; spanDays: number } | null;
  adds?: MeasuredAdd[];
  auras?: MeasuredAura[];
  phases?: MeasuredPhase[];
  truncated?: boolean;
  /** Everything limiting how far these numbers can be read. Not a footnote. */
  caveats: string[];
  timeline: FightTimeline | null;
}

/**
 * One row of asserted against measured.
 *
 * `delta` is arithmetic, never a verdict — there is deliberately no "agrees" flag.
 * A profile saying 300s against a measured 288s is not wrong; a boss measured at
 * two targets where the owner plays three means the extraction is probably broken.
 * Both need a person.
 */
export interface FightComparisonRow {
  fact: string;
  profile: number | string | null;
  measured: number | string | null;
  provenance: string;
  note: string;
  delta: number | null;
}

export interface FightEncounter {
  encounterId: number;
  name: string;
  difficulty: number;
  /** True when anything at all has been asserted or measured about this boss. */
  hasFacts: boolean;
  facts: FightFact[];
  profile: FightProfileBlock;
  scenario: FightScenario;
  /** Null when no probe has ever looked. Present with 0 fights when one looked and found nothing. */
  measured: MeasuredFight | null;
  comparison: FightComparisonRow[];
  /** Absent on a dataset published before the promotion machinery existed. */
  promotions?: FightPromotion[];
  promoteCommand?: string;
}

export interface FightMeasurementRun {
  generatedAt: string | null;
  difficulty: number | null;
  metric: string | null;
  reportsPerEncounter: number | null;
  /** "first" (earliest kills, alike) or "top" (rankings damage order, speed kills). */
  order?: string | null;
  /** Page 1 is the world's best pulls, which are not shaped like a typical kill. */
  rankingsPage: number | null;
  eventStreams: string[];
  significantDamageShare: number | null;
  samplingBias: string | null;
  abortedBecause: string | null;
  cost: Record<string, unknown> | null;
}

export interface FightsDataset {
  schemaVersion: number;
  generatedAt: string;
  tier: string;
  note: string;
  /** Null when the file was published from the profiles alone, with no probe run. */
  measurement: FightMeasurementRun | null;
  coverage: { encounters: number; asserted: number; measured: number };
  encounters: FightEncounter[];
}

/**
 * The talent tree, decoded from simc's own loadout string against simc's own trait
 * table -- see `pipeline/src/wowdps/talenttree.py` for how that decode was verified
 * offline. `tree` is a key into `TalentTreeDataset.trees`, shared by every build that
 * plays the same spec and hero tree.
 */
export interface TalentTreeNodeEntry {
  id: number;
  name: string;
  spellId: number;
}

export interface TalentTreeNode {
  id: number;
  /** simc's `talent_tree`: 1 class, 2 specialisation, 3 hero. */
  tree: number;
  row: number;
  col: number;
  /** `trait_node_type_e`: 0 normal, 1 tiered, 2 choice. */
  type: number;
  maxRanks: number;
  entries: TalentTreeNodeEntry[];
}

export interface TalentTreeLayout {
  specId: number;
  subTree: number | null;
  nodes: TalentTreeNode[];
}

export interface TalentTreeBuild {
  specId: string;
  displayName: string;
  tree: string;
  heroTalent: string | null;
  points: { class: number; spec: number; hero: number };
  /** Set when the profile's own loadout looks unfinished; shown beside the tree. */
  caveat: string | null;
  selected: { id: number; entry: number; rank: number }[];
}

export interface TalentTreeDataset {
  schemaVersion: number;
  tier: string;
  note: string;
  trees: Record<string, TalentTreeLayout>;
  builds: TalentTreeBuild[];
  notes: string[];
}

/**
 * Which of the game's damage specs this tier ships a profile for. The reference
 * list is derived from the other tiers simc ships, not written down -- see
 * `profiles.spec_coverage`. Absent on a dataset built before this existed.
 */
export interface SpecCoverage {
  /** Damage specs this tier has a profile for. */
  damageSpecs: number;
  /** Damage specs simc has shipped for *some* tier, which is the reference. */
  damageSpecsKnown: number;
  /** Shipped by no tier-N profile at all: simc has not written one yet. */
  missing: { class: string; spec: string }[];
  /** The tiers the reference list was drawn from. */
  comparedWith: string[];
  /** What this tier ships a profile for, which is what a complete run produces. */
  shipped?: { class: string; spec: string }[];
  /**
   * Shipped by simc for this tier and yet absent from the dataset -- the profile
   * no longer loads. Distinct from `missing`, and on an old tier it is the larger
   * number: MID1 ships all 26 damage specs and 16 of its 41 profiles fail on
   * current simc, so ten specs are broken where none are missing. Both are absent
   * from the ranking and only one of them is simc's authors not having got to it
   * yet. Undefined on a dataset built before the split existed.
   */
  broken?: { class: string; spec: string }[];
  /** Specs this tier ships *and* produced results for. */
  simulated?: number;
  /**
   * Specs simc wrote a complete profile for and left commented out in its
   * generator. Neither shipped nor missing, and the reason MID2 looks as thin as
   * it does: the profile exists, talent hash and every gear slot, with a warning
   * on it. A number from one of these is a weaker claim than one off a shipped
   * profile and is labelled that way wherever it is drawn.
   */
  unvalidated?: { class: string; spec: string }[];
  /** How many of those this run actually got a result out of. */
  unvalidatedSimulated?: number;
}

/**
 * What moved in SimulationCraft between the previously published run and this one,
 * derived from its git history — simc ships no changelog file. Absent on datasets
 * built before this existed; `commits` absent when the checkout could not see back
 * far enough, which is not the same as "nothing changed".
 */
export interface SimcChanges {
  /** When the revision was committed — not when CI compiled it. */
  revisionDate: string | null;
  /** The revision compared against, or null when nothing was published before. */
  since: string | null;
  /** Present only when the comparison could actually be made. */
  commits?: number;
  /** simc's own automated data dumps, counted rather than listed. */
  generatedFiles?: number;
  byTag?: { tag: string; commits: number }[];
  otherTags?: number;
  recent?: {
    revision: string;
    date: string;
    subject: string;
    tag?: string;
    pullRequest?: number;
  }[];
  /** Why there is no comparison, when there is none. */
  why?: string;
}

/**
 * Every class and spec in the game, for the Spec detail picker.
 *
 * Derived from simc — see `specindex.py`. The picker draws the whole game rather
 * than the tier's build list, so a spec's absence from the rankings reads as
 * absence rather than as a bad result. That is the coverage panel's argument, one
 * step closer to the reader.
 */
export interface SpecIndex {
  tier: string;
  classes: SpecIndexClass[];
  heroTrees: SpecIndexHeroTree[];
  /**
   * Coverage one level finer than the spec: a spec plays two hero trees and a tier
   * routinely ships a build for one of them. Absent on a dataset built before this
   * existed, and null when the manifest carries no coverage block — an empty
   * coverage claim would read as complete coverage.
   */
  heroTreeCoverage?: HeroTreeCoverage | null;
  /**
   * Profiles simc ships or wrote whose stored talent hash the current tree refuses,
   * decided offline against simc's trait table rather than by running simc. Absent
   * on a dataset built before this existed.
   */
  refusedProfiles?: RefusedProfile[] | null;
  note: string;
}

export interface HeroTreeCoverage {
  /** (damage spec x hero tree) pairs the trait table places. */
  cells: number;
  /** …of which this tier publishes a build. */
  covered: number;
  uncovered: UncoveredHeroTree[];
  /**
   * A build playing a tree simc's trait table places on no spec at all —
   * Annihilator carries no `id_spec` on any of its nodes today. Reported rather
   * than counted: counting it would invent a pairing, dropping it silently would
   * lose the only evidence the table has a hole.
   */
  unplaced: { build: string; subTree: number; tree: string | null }[];
}

export interface UncoveredHeroTree {
  class: string;
  spec: string;
  specId: number;
  subTree: number;
  /** null only on a checkout too old to ship simc's hero tree name table. */
  tree: string | null;
  /** `shipped` | `unvalidated` | `missing` | `broken`, from the manifest's own block. */
  state: string;
  /** simc's refusal of this profile's talent hash, with the node id, where there is one. */
  reason: string | null;
}

export interface RefusedProfile {
  class: string;
  spec: string;
  /** simc's internal profile name. */
  profile: string;
  heroTree: string | null;
  unvalidated: boolean;
  reason: string;
}

export interface SpecIndexClass {
  class: string;
  token: string | null;
  specs: SpecIndexSpec[];
}

export interface SpecIndexSpec {
  specId: number;
  name: string;
  class: string;
  /**
   * `damage` | `tank` | `healer` | `unknown`. Unknown is a real state, not a gap:
   * role comes from a profile's `role=` line, and simc ships no healing profiles
   * at all, so a healer is honestly "simc does not simulate this" rather than
   * asserted to be a healer by a table this project would have to maintain.
   */
  role: string;
  /** simc ships a profile for this spec in this tier. */
  profiled: boolean;
  /** …in any tier. The difference is "not this season" versus "never". */
  profiledEver: boolean;
  /** Dataset build ids this tier publishes for the spec. */
  builds: string[];
  subTrees: number[];
}

export interface SpecIndexHeroTree {
  subTree: number;
  class: string;
  specIds: number[];
  /**
   * From simc's own `__trait_sub_tree_data`. null only on a checkout old enough not
   * to ship that table, where a tree could be named only by a build that played it.
   */
  name: string | null;
  /** Dataset build ids of this tier's builds that play it. */
  builds?: string[];
}

/**
 * What a tier set and an outside Power Infusion are worth, per spec.
 *
 * Every figure is a *difference* between two profilesets against the spec's own
 * profile, not a level — see `buffsweep.py`. The four-piece value is what it adds
 * over the two-piece, because that is the choice being made.
 */
export interface BuffDataset {
  tier: string;
  generatedAt: string;
  settings: { deterministic: boolean; iterations: number };
  note: string;
  /**
   * How many builds the sweep covered, and how many the tier held when it ran.
   *
   * `buffsweep.write_buffs` has emitted this for as long as `gear.json` has, and
   * this type did not carry it -- so `BuffsView` passed `null` to `sweepCoverage`
   * under a comment saying the file has no coverage block. The comment was true of
   * this interface and false of the file. A missing field in a type is not a
   * missing field in the data, and the view cannot read what the type hides.
   *
   * Optional because a document written before `builds_available` existed has none,
   * and "absent" is a third answer beside a complete and an incomplete sweep.
   */
  coverage?: { specs: number; specsAvailable: number };
  specs: BuffSpec[];
}

export interface BuffSpec {
  id: string;
  displayName: string;
  class: string;
  spec: string;
  heroTalent: string;
  baseDps: number;
  dpsError: number;
  /** Null when simc ships no set for this class in this tier. */
  setName: string | null;
  twoPieceGain: number | null;
  twoPiecePercent: number | null;
  /** Over the two-piece, not over nothing. */
  fourPieceGain: number | null;
  fourPiecePercent: number | null;
  powerInfusionGain: number | null;
  powerInfusionPercent: number | null;
  /** The seconds Power Infusion lands at. The number means nothing without them. */
  powerInfusionTimes: number[];
  /** The season boundary, or null for a tier with no predecessor. */
  crossover: BuffCrossover | null;
  errors: string[];
}

/**
 * The three set states a player chooses between when a season turns.
 *
 * Levels rather than gains, deliberately: they are alternatives to each other and
 * there is no natural baseline among them — which one is the reference is the
 * question being asked. `split` is the state a player passes *through*, when the
 * first two new pieces have replaced the old four-piece and the third and fourth
 * have not arrived.
 */
export interface BuffCrossover {
  previousSetName: string | null;
  previousFourDps: number;
  splitDps: number | null;
  currentFourDps: number;
  splitOverPreviousFour: number | null;
  currentFourOverSplit: number | null;
  currentFourOverPreviousFour: number | null;
}

// ---------------------------------------------------------------------------
// Computed builds (<tier>/computed-builds.json)
//
// Optional, and its absence is a supported state rather than a gap: a tier that
// has never been through `wowdps build-search` has no such file, and every
// reader degrades to "SimulationCraft's build, unmarked". Only the fields the
// site actually reads are typed here -- the document also carries the gear
// anchor, the run's caveats and the harvest evidence, which the spec page will
// want and the ranking does not.

export interface ComputedContender {
  origin: "simc" | "search" | "harvest";
  label: string;
  talentHash: string | null;
  heroTalent?: string | null;
  dps: number;
  /** Percent standard error of the mean, as simc reports it. */
  dpsError: number;
  iterations: number;
  /**
   * Damage on the priority target, measured in the same anchored run as `dps`.
   * The published document also carries it on most one-target rows, where it
   * simply equals `dps` — a single-enemy cell has no second axis, and readers
   * must treat that signature as "no split" rather than as a boss figure. What
   * travels to the published figure is the *ratio* between two contenders'
   * values, never the absolute (same reasoning as the ranking margin). Issue
   * #99 is the finding this field exists to surface: a marked build can do
   * less on the boss while doing more overall.
   */
  priorityDps?: number;
}

export interface ComputedSpec {
  id: string;
  scenario: string;
  targets: number;
  /** False when no search covered this build. Not the same as "found nothing". */
  searched: boolean;
  simc: ComputedContender | null;
  best: ComputedContender | null;
  runnerUp: ComputedContender | null;
  caveats: string[];
  /**
   * The same head-to-head measured on **simc's own shipped kit** rather than on
   * the gear anchor. Optional because a document written before the pipeline
   * measured it has none, and absent is not the same as a margin of zero: it is
   * the state the ranking falls back to the projection for.
   */
  shipped?: ComputedShipped;
}

/**
 * simc's build against the computed one, on the kit simc's own profile wears.
 *
 * This exists because the projection it replaces was measured and does not hold.
 * Over all twelve marked MID2 builds on 2026-08-26 the anchored margin and this
 * one agree to about a tenth of a point on seven of nine -- and disagree by
 * **2.52 points** on Devastation Evoker (Scalecommander), whose entire published
 * gain is absent on simc's own gear. The sign goes both ways, so no correction
 * factor exists.
 */
export interface ComputedShipped {
  simcDps: number;
  bestDps: number;
  /** Signed relative lead of the computed build, measured on shipped gear. */
  margin: number;
  /** The tie band that lead has to clear, from the two runs' errors. */
  tieBand: number;
  /** Whether it clears it. Recomputed in the view; carried for the tooltip. */
  separates: boolean;
}

/**
 * Which run measured one `(scenario, targets)` pair.
 *
 * The pair is the unit because it is what `merge_specs` replaces, so one document
 * routinely holds pairs from different nights on different simc revisions — the
 * 5- and 10-target passes of 2026-08-26/27 did exactly that. A document-level block
 * would be wrong immediately, which is the same defect `gear.json` had over three
 * slots (#95) and this document had over three target counts (#100).
 *
 * `simc` is absent, never empty, when the run could not read its own metadata:
 * unknown is not "no revision".
 */
export interface ComputedRun {
  scenario: string | null;
  targets: number | null;
  generatedAt: string;
  settings: { iterations: number; deterministic: boolean };
  simc?: SimcMeta;
}

export interface ComputedBuildsDataset {
  schemaVersion: number;
  generatedAt: string;
  tier: string;
  note: string;
  /**
   * The NEWEST run's settings, not every row's. `runs` is the per-pair truth; this
   * stays because readers predate it.
   */
  settings: { iterations: number; deterministic: boolean };
  coverage: { specs: number; specsAvailable: number };
  /**
   * One entry per `(scenario, targets)` pair. Optional: a document written before
   * this existed carries none, and the merge invents no blocks for its rows —
   * absent means "nobody recorded which simc measured this", which is the honest
   * state and not the same as a missing field being zero.
   */
  runs?: ComputedRun[];
  specs: ComputedSpec[];
}
