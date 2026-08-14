/** Shape of the JSON the pipeline writes. Mirrors pipeline/src/wowdps/dataset.py. */

export interface Ability {
  name: string
  id?: number
  /** Fraction of the spec's total damage, 0..1. Shares across a cell sum to 1. */
  share: number
  executes: number
}

export interface Cell {
  targets: number
  dps: number
  /** Standard error of the DPS mean, in percent. */
  dpsError: number
  dpsStddev: number
  iterations: number
  fightLength: number
  abilities: Ability[]
  /** Damage per second landing on the main target. Absent at one target. */
  priorityDps?: number
  /** priorityDps / dps, i.e. the fraction landing on the main target. */
  priorityShare?: number
  /**
   * priorityShare x targets. 1.0 = damage spread evenly across all targets;
   * N = every point of damage lands on the main target.
   *
   * Distribution only. A spec with no area damage scores N here without the extra
   * targets having helped it at all — for that question use funnelGain.
   */
  concentration?: number
  /**
   * priorityDps at N targets / dps at one target. Above 1.0 the extra targets are
   * actively feeding the main target (resources from damage-over-time effects,
   * procs); below 1.0 the global cooldowns spent on area damage cost it damage.
   * This is what "funnel" means in play.
   */
  funnelGain?: number
  /** Mean damage per second, one value per timelineBin seconds. */
  timeline?: number[]
  timelineBin?: number
  /** Peak 20s window DPS relative to the fight average. 1.0 = perfectly flat. */
  burstRatio?: number
}

export interface ScenarioCells {
  targets: Cell[]
}

export interface SpecDetail {
  id: string
  class: string
  spec: string
  heroTalent: string
  specId: string
  displayName: string
  role: string
  talentHash: string | null
  caveats: string[]
  errors: string[]
  scenarios: Record<string, ScenarioCells>
}

export interface SpecSummaryScenario {
  dps: Record<string, number>
  /** Measured at five targets. */
  concentration?: number
  priorityShare?: number
  funnelGain?: number
  burstRatio?: number
}

export interface SpecSummary {
  id: string
  class: string
  spec: string
  heroTalent: string
  specId: string
  displayName: string
  role: string
  scenarios: Record<string, SpecSummaryScenario>
  errors?: string[]
}

export interface ScenarioMeta {
  id: string
  label: string
  description: string
  fightStyle: string | null
  targetCounts: number[]
  maxTime: number
  supportsFunnel: boolean
  /**
   * Scenario id whose single-target cell funnel gain divides by. "self" means this
   * scenario's own 1-target run; null means gain is not computable here.
   */
  funnelBaseline?: string | null
  /** False when the scenario runs at one target count only. */
  sweepsTargets?: boolean
}

export interface SimcMeta {
  simcVersion?: string
  buildDate?: string
  gitRevision?: string
  gitBranch?: string
  ptr?: boolean
  beta?: boolean
  reportVersion?: string
}

export interface TierMeta {
  /** simc's tier directory name, e.g. "MID2". */
  id: string
  /** Human form, e.g. "Midnight Season 2". */
  label: string
  generatedAt: string | null
  specCount: number
  simcVersion?: string | null
}

/**
 * Which tiers the dataset holds, oldest first, and which one is current.
 *
 * A tier is a different game state rather than a filter: the profiles carry
 * different gear, different talents and a different spec list, so the site shows
 * one tier at a time instead of mixing them into a single ranking.
 */
export interface TierIndex {
  current: string
  tiers: TierMeta[]
}

export interface RunSettings {
  /** Requested standard error in percent. 0 means the run was deterministic instead. */
  targetError: number
  /** Fixed iteration count in deterministic mode, ceiling in adaptive mode. */
  maxIterations: number
  /** True when simc ran a fixed, reproducibly seeded iteration count. */
  deterministic?: boolean
  /**
   * Median standard error actually measured across every cell of the run, in
   * percent. This is the honest figure: in deterministic mode nobody requests an
   * error, so `targetError` is 0 and says nothing about how precise the run is.
   */
  medianDpsError?: number | null
}

export interface Manifest {
  schemaVersion: number
  generatedAt: string
  tier: string
  simc: SimcMeta
  settings: RunSettings
  scenarios: ScenarioMeta[]
  specs: SpecSummary[]
}

export interface LogsComparison {
  specId: string
  displayName: string
  encounterId: number
  encounterName: string
  sampleSize: number
  median: number
  p95: number
  max: number
  simDps: number
  logsToSimRatio: number
}

export interface LogsVerification {
  generatedAt: string
  metric: string
  difficulty: number
  note: string
  comparisons: LogsComparison[]
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
  id: string
  label: string
  ilevel: number
  evidence?: string
}

export interface GearItemMeta {
  id: number
  name: string
  slug: string
  /** "raid" | "mythicplus". Asserted by the pipeline's pool file, not derived. */
  source: string
  /** null for trinkets that allocate no primary stat, which anyone can use. */
  primaryStat: string | null
  secondaryStat?: string
}

/** One baseline-pool item measured on its own, with every other socket empty. */
export interface GearPoolEntry {
  id: number
  ilevel: number
  dps: number
  dpsError: number
  /** DPS this item adds over wearing nothing at all in the slot. */
  standaloneGain: number
  /** True for the items that became the baseline. */
  chosen: boolean
}

export interface GearCandidate {
  id: number
  /** Matches a GearItemLevel id. */
  level: string
  ilevel: number
  /** Item id this candidate was put in place of. */
  replaces: number
  dps: number
  dpsError: number
  /** Fraction of baseline DPS gained. Negative means the baseline is better. */
  gain: number
  /**
   * The baseline's and the candidate's standard errors in quadrature. A gain
   * smaller than this is a tie, not a lead — the same rule the Builds view uses.
   */
  gainError: number
  priorityDps?: number
}

export interface GearTargetResult {
  targets: number
  /** DPS with every socket in the slot empty. The floor the pool is measured from. */
  emptyDps: number
  baseline: {
    items: number[]
    ilevel: number
    dps: number
    dpsError: number
  }
  pool: GearPoolEntry[]
  candidates: GearCandidate[]
}

export interface GearSpecResult {
  id: string
  class: string
  spec: string
  heroTalent: string
  specId: string
  displayName: string
  primaryStat: string
  targets: GearTargetResult[]
  errors?: string[]
}

export interface GearSlot {
  id: string
  label: string
  sockets: string[]
  baselineSource: string
  baselineSourceLabel: string
  candidateSource: string
  candidateSourceLabel: string
  note: string
  itemLevels: GearItemLevel[]
  items: GearItemMeta[]
  specs: GearSpecResult[]
}

export interface GearDataset {
  schemaVersion: number
  generatedAt: string
  tier: string
  simc: SimcMeta
  settings: RunSettings
  /** How much of the tier this run actually covered. Never inferred from length. */
  coverage: { specs: number; specsAvailable: number }
  slots: GearSlot[]
}

// --------------------------------------------------------------------------------
// Fight shapes per boss (<tier>/fights.json)
// --------------------------------------------------------------------------------

/** A pooled number with the range it was pooled from. Never a bare median. */
export interface FightSpread {
  median: number
  low: number
  high: number
  /** How many fights it was pooled from. */
  n: number
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
  key: string
  label: string
  source: 'hand' | 'logs' | 'default'
  sourceLabel: string
  summary: string
  detail: string
  statedBy: string | null
  observedAt: string | null
  sample: number | null
  reports: string[]
  value: unknown
}

export interface FightAddWave {
  name: string
  count: number
  first: number
  duration: number
  /** Seconds between waves; null means it happens once. */
  cadence: number | null
}

export interface FightAmplification {
  ability: string
  abilityId: number | null
  multiplier: number
  first: number
  duration: number
  /** "priority" | "add" | "unknown". Only "priority" has a simc equivalent. */
  target: string
  magnitudeSource: string
  /** Always false: no field in the Warcraft Logs API says what an aura does. */
  magnitudeMeasurable: boolean
  /** False when simc has nothing to express this with. */
  representable: boolean
}

export interface FightProfileBlock {
  baselineTargets: number
  /** Null when no target count was asserted — not false. */
  constantTargets: boolean | null
  fightLengthSeconds: number | null
  raidSize: number | null
  addWaves: FightAddWave[]
  amplifications: FightAmplification[]
  phases: Array<Record<string, unknown>>
}

/** The simc scenario a profile produces, and what did not survive the trip. */
export interface FightScenario {
  encounterId: number
  name: string
  targets: number
  maxTime: number
  options: string[]
  unrepresented: string[]
  /** Fact keys whose value is somebody's word rather than a measurement. */
  asserted: string[]
  /** Always null: naming a fight style makes simc clear the raid events. */
  fightStyle: string | null
  /** Target count over time, as [second, count] pairs. */
  steps: Array<[number, number]>
}

export interface MeasuredAdd {
  key: string | number
  name: string
  gameId: number | null
  seenInFights: number
  instances: FightSpread | null
  firstSeen: FightSpread | null
  lifetime: FightSpread | null
  cadence: FightSpread | null
  damageShare: FightSpread | null
  presentAtPull: boolean
}

export interface MeasuredAura {
  abilityId: number
  ability: string
  seenInFights: number
  applications: number
  start: FightSpread | null
  duration: FightSpread | null
  distinctTargets: number
  anyTruncated: boolean
}

export interface MeasuredPhase {
  id: number
  name: string
  isIntermission: boolean
  start: FightSpread | null
  duration: FightSpread | null
  seenInFights: number
}

export interface FightPhaseWindow {
  id: number
  name: string
  isIntermission: boolean
  start: number
  duration: number
}

export interface FightAuraWindow {
  abilityId: number
  ability: string
  start: number
  duration: number
  /** No measured end: the window is a floor, not a value. */
  truncated: boolean
  instance: number | null
}

/** One sampled pull, published whole. Never an average of pulls. */
export interface RepresentativeFight {
  reportCode: string
  fightId: number
  kill: boolean
  durationSeconds: number
  raidSize: number | null
  steps: Array<[number, number]>
  mean: number
  peak: number
  peakShare: number
  constant: boolean
  /** Every enemy that took a hit, including ones under the significance floor. */
  allEnemySteps: Array<[number, number]>
  allEnemyPeak: number
  phases: FightPhaseWindow[]
  auras: FightAuraWindow[]
  truncated: boolean
  warnings: string[]
}

export interface FightTimeline {
  /** "representative" — one real pull, never a per-second average. */
  pooling: string
  why: string
  chosenBecause: string
  representative: RepresentativeFight
  others: Array<{
    reportCode: string
    fightId: number
    durationSeconds: number
    kill: boolean
    steps: Array<[number, number]>
  }>
}

export interface MeasuredFight {
  fightsSampled: number
  reports: string[]
  durationSeconds?: FightSpread | null
  raidSize?: FightSpread | null
  playersListed?: FightSpread | null
  meanTargets?: FightSpread | null
  peakTargets?: FightSpread | null
  peakTargetShare?: FightSpread | null
  activeTimeFraction?: FightSpread | null
  adds?: MeasuredAdd[]
  auras?: MeasuredAura[]
  phases?: MeasuredPhase[]
  truncated?: boolean
  /** Everything limiting how far these numbers can be read. Not a footnote. */
  caveats: string[]
  timeline: FightTimeline | null
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
  fact: string
  profile: number | string | null
  measured: number | string | null
  provenance: string
  note: string
  delta: number | null
}

export interface FightEncounter {
  encounterId: number
  name: string
  difficulty: number
  /** True when anything at all has been asserted or measured about this boss. */
  hasFacts: boolean
  facts: FightFact[]
  profile: FightProfileBlock
  scenario: FightScenario
  /** Null when no probe has ever looked. Present with 0 fights when one looked and found nothing. */
  measured: MeasuredFight | null
  comparison: FightComparisonRow[]
}

export interface FightMeasurementRun {
  generatedAt: string | null
  difficulty: number | null
  metric: string | null
  reportsPerEncounter: number | null
  /** Page 1 is the world's best pulls, which are not shaped like a typical kill. */
  rankingsPage: number | null
  eventStreams: string[]
  significantDamageShare: number | null
  samplingBias: string | null
  abortedBecause: string | null
  cost: Record<string, unknown> | null
}

export interface FightsDataset {
  schemaVersion: number
  generatedAt: string
  tier: string
  note: string
  /** Null when the file was published from the profiles alone, with no probe run. */
  measurement: FightMeasurementRun | null
  coverage: { encounters: number; asserted: number; measured: number }
  encounters: FightEncounter[]
}
