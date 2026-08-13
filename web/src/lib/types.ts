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

export interface Manifest {
  schemaVersion: number
  generatedAt: string
  tier: string
  simc: SimcMeta
  settings: { targetError: number; maxIterations: number }
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
