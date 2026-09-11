# progress-cohort: the data contract (v1, 2026-09-11)

The interface between three pieces of code:

| role | what | where |
|---|---|---|
| producer 1 | `wowdps progress-sweep` | this repository (public), GitHub Actions on free runners |
| producer 2 | `manage.py export_progress_hours` | wtt-backend -- the one-time seed from the production database |
| consumer | `manage.py import_progress_hours` | wtt-backend, a daily Cloud Run job upserting `ProgressBossHours` |

Transport is git. The files live in the **private** repository
`Wild-Things-Tools/wtt-progress-data`, branch `main`, directory `progress-cohort/`.
`.github/workflows/progress-sweep.yml` checks that repository out with
`actions/checkout` (`repository` + `token` = secret `PROGRESS_DATA_PAT`, a fine-grained
PAT with Contents read/write on `wtt-progress-data` ONLY) into `./data`, runs the sweep
with `--out data/progress-cohort`, validates, and commits and pushes there
(rebase-and-push, three attempts). **Nothing about guilds is ever written into this
repository.** The import job downloads the repository's tarball with the same token,
read from Secret Manager -- a credential on the private side only.

Owner decisions applied: the rows are private (option B); the derivation logic stays
private -- sweep rows carry **raw inputs**, and the import computes `logging_gap_ratio`
and `night_span_hours` with the backend's own functions. Nothing derived leaves the
public side.

## Files

Ten file sets: zones 53, 46, 44, 42, 38 x difficulties 5, 4.

```
progress-cohort/
  README.md                       what this is, who writes it, who reads it
  manifest.json                   {"v":1,"files":[{"path":"z53-d5.rows.jsonl","zoneId":53,
                                    "difficulty":5,"rows":N,"refused":M,"lastSweptAt":ISO|null}, ...]}
                                  NO run timestamp at top level: a run that changes nothing
                                  must not change the manifest
  z<zone>-d<diff>.rows.jsonl      one measured guild per line, APPEND-ONLY, UTF-8,
                                  "\n"-terminated, compact JSON (no spaces)
  z<zone>-d<diff>.refused.jsonl   one judged-but-not-measured guild per line, append-only
  z<zone>-d<diff>.state.json      sweep state per encounter (the cursor), rewritten
                                  atomically after every encounter
```

Encounter ids are Warcraft Logs **live** ids from `worldData.zone(id:) { encounters
{ id name } frozen }` -- never PTR `53xxx` ids, never `fight_profiles.json`.

## A `rows.jsonl` line

```
{"v":1,"zoneId":44,"encounterId":3129,"difficulty":5,"guildId":123456,
 "guildName":"…"|null,"serverSlug":"…"|null,"serverRegion":"EU"|null,
 "hours":12.345678901234,
 "attempts":210,"nightsObserved":5,"spanDays":9.2,
 "firstKillAtMs":1723456789000|null,
 "killAtMs":1723456000000|null,
 "reportsSeen":31,
 "reportStartsMs":[...],
 "nightsMs":[[firstStartMs,lastEndMs],...],
 "loggingGapRatio":1.4|null,"nightSpanHours":18.1|null,
 "measuredAt":"2026-09-12T13:17:04+00:00","run":"34599910061"}
```

- `guildName` / `serverSlug` / `serverRegion`: from the ranking row via a port of
  `pullhours.guild_identity`; `null` means not recognised, never guessed.
- `hours`: **unrounded**, `> 0` (`pull_time` returns `unusable` for 0).
- `firstKillAtMs`: the **ranking's** `killTime` -- the ranking defines the first kill.
- `killAtMs`: the log's kill fight start; informational, not imported.
- `reportStartsMs`: RAW INPUT -- `startTime` of every report the walk read for this
  guild in this zone, ints, ascending.
- `nightsMs`: RAW INPUT -- one `[first start, last end]` pair per raid night
  (`NIGHT_GAP_MS` = 21 600 000), over the attempts up to and including the kill.
- `loggingGapRatio` / `nightSpanHours`: OPTIONAL, present on **seed** rows only
  (already computed in the database).
- `run`: `GITHUB_RUN_ID`, or `seed:<date>` for exported rows.

Import rule: if the `loggingGapRatio`/`nightSpanHours` keys are PRESENT they are taken
as they are (null included); else they are computed from the raw inputs; if neither is
possible they are null. `hours <= 0`, `attempts < 1`, difficulty not in (4, 5), or a
missing required key is a `parse` refusal (the row is skipped and counted). Labels are
truncated by the import to the model's `max_length` (120/64/16).

## A `refused.jsonl` line

```
{"v":1,"encounterId":3129,"guildId":123456,"outcome":"kill-too-late",
 "killTimeMs":1723456789000|null,"reportsSeen":31,"at":ISO,"run":"…"}
```

Outcomes: `unlogged-kill | kill-too-late | kill-too-early | no-reports | no-fights |
no-kill | no-report-time | unusable | truncated | no-kill-time | error`. `error` rows
are what `--retry-errors` re-attempts. `refused.jsonl` carries **no names**.

## `state.json`

```
{"v":1,"zoneId":44,"zoneName":"Manaforge Omega","frozen":true,"difficulty":5,
 "encounters":{"3129":{"name":"Plexus Sentinel","cursorKillTimeMs":1723456789000|null,
   "cursorGuildId":123456|null,"rankingExhausted":false,"walled":true,
   "stoppedOnBudget":false,"sweptAt":ISO|null,
   "guildsSeen":1000,"withoutGuild":38,"attempted":186,"named":962,"shape":"",
   "outOfOrder":0,"outcomes":{"measured":40,"kill-too-late":65,"unlogged-kill":40,
   "no-reports":41}}}}
```

Cursor semantics are wtt-backend's `before_cursor`, ported 1:1: a strictly earlier
`killTime` was already judged; an equal one only for the cursor row itself; a row
without `killTime` is never skipped; the cursor moves forward only, and only past rows
that got a verdict (measured or refused); `int()` truncation of the millisecond.

`walled` := the last page read was 20 (`RANKING_MAX_PAGE`), `hasMorePages` was still
true, and no fresh row was on it -- set **only** when `--guilds` did not stop the walk
first. The "already seen" set for an encounter is the guild ids in `rows.jsonl` ∪
`refused.jsonl` of the pair for that encounter. Rows without a `guild.id` are counted
as `withoutGuild` and never enter it.

Seed: `walled` is derived by the export as `guilds_seen >= 1000 and not
ranking_exhausted` from `ProgressHoursSweep`, and the encounter entry is marked
`"seed": true`.

## Skip matrix (public sweep)

| zone | skipped when |
|---|---|
| frozen | `(rankingExhausted or walled)` and not `stoppedOnBudget` and the last run's `attempted == 0`; `--refresh-after 168` hours re-walks anyway |
| live, not walled | the last walk's `attempted == 0` and `sweptAt` is under `--refresh-after-live 6` hours ago |
| live and walled | the same, under `--live-wall-refresh-after 24` hours |

`stoppedOnBudget` or `attempted > 0` last time -> never skipped.

## Schema alarms (fail closed, per pair)

- a ranking row with `fromlog` **missing** -> `ranking-row-unscreened`: this PAIR writes
  nothing (no rows, no state), the run continues with the next pair, exit 3 at the end;
- a ranking row with `fromlog` present but `killTime` **missing** -> REFUSED as
  `no-kill-time`, never passed to `pull_time` (both repositories used to disable
  screen 2 silently in that case; do not copy that);
- the response cache is OFF for every sweep query. A cached ranking freezes the order
  and blinds the cursor.

## Budget and cadence (public sweep)

- The point ceiling is against the **absolute** hourly counter
  (`rateLimitData.pointsSpentThisHour`), refreshed before every walk. At the ceiling
  the run **sleeps** until `pointsResetIn` + 30 s and continues (free on a public
  runner) -- it never exits on the ceiling -- until `--deadline-minutes` (default 300)
  is reached; then it writes and exits 0. A 429 stops the run the same way.
- Cron four times a day, off the hour (`17 0,6,12,18 * * *`), `timeout-minutes: 350`
  (GitHub's job limit is 6 h), one concurrency group, `cancel-in-progress: false`.
  Measured on this repository: the hourly `fight-probe` cron fired only 2-8 times a
  day since 2026-08-27, with delays up to 60 min -- so few long runs, not many short
  ones. The cron line ships commented out and is switched on after the seed and the
  first hand runs.
- `--workers 1` is the default and the only implemented value. The walk is I/O-bound
  at 13-16 s per guild, and the ceiling binds before the deadline, so parallelism buys
  little. The design for more than one (a ledger lock, a low-water cursor) is
  specified and not built; `attempt_guild` is the unit a pool would call.
- Own-guild rows stay a private backend concern (`--own-guild-only`,
  `outside_cohort=True`). The private job must NOT use a low ceiling against the
  absolute counter -- the sweep can leave 10k or more in it -- use 0.9; it needs about
  500 points.

## Import (wtt-backend, daily Cloud Run job `wtt-progress-import`)

- Download the tarball with the token; refuse (exit 1) if `manifest.json` is missing.
  Per file: skip if unchanged (sha256 of the file recorded in
  `ProgressHoursImport(object_name PK, digest, rows_seen, rows_upserted,
  rows_refused_*, imported_at)`).
- Encounter check in one query: `ProgressEncounter.objects.filter(id__in=ids)
  .values_list("id", "zone_id")`; unknown -> `unknown-encounter` refusal (never
  create); `zone_id != row.zoneId` -> `zone-mismatch` refusal (the PTR-twin guard:
  zone 54 IS in the catalogue with `53xxx` ids).
- `bulk_create(objs, batch_size=1000, update_conflicts=True,
  unique_fields=["encounter", "difficulty", "guild_id"], update_fields=[guild_name,
  server_slug, server_region, hours, attempts, nights_observed, span_days,
  logging_gap_ratio, first_kill_at, night_span_hours, outside_cohort, measured_at])`,
  `outside_cohort=False` constant; `first_kill_at = guildmeasure.kill_time(firstKillAtMs)`;
  `measured_at` from the row. NEVER delete.
- `ProgressHoursSweep.update_or_create` per (encounter, difficulty) from `state.json`
  including the new field `ranking_walled` (migration 0014).
- Staleness: `max(lastSweptAt)` older than 48 h -> WARN; older than 3 days with open
  state -> exit 1 (the Cloud Monitoring alert on failed executions is the reader).
- 100 % of a file refused -> exit 1 (the wrong-database signature); more than 10 %
  parse refusals in a file -> the file is refused, exit 1.

## Guard in wtt-backend

`WCL_PROGRESS_COHORT_SOURCE` = `backend` (default in code) | `public`. Under `public`,
`fetch_progress_hours` refuses any run that is not `--own-guild-only`. Set to `public`
on the job AND the service (the admin UI can launch commands) at the switch-over, and
in `cloudbuild.yaml`'s `--update-env-vars` so a deploy keeps it.

## What this repository's side implements, and how to run it

```
wowdps progress-sweep --out data/progress-cohort                   # the sweep
wowdps progress-sweep --seed-only --out data/progress-cohort       # read the files, no query
wowdps progress-sweep --validate data/progress-cohort              # the commit gate
wowdps progress-sweep --retry-errors --out data/progress-cohort    # exactly the `error` guilds
```

Exit codes: 0 done or stopped on budget (what is measured is written), 1 a
`--validate` violation or a usage error, 2 a zone Warcraft Logs would not list, 3 a
schema alarm on some pair.

The validator checks three things and refuses on any of them: every line of every
`.jsonl` is JSON, the manifest's counts equal the files' line counts, and no tracked
rows/refused file is shorter than it is at `HEAD` -- the append-only promise the
import's per-file digest rests on.

Two things about the pair-level alarm worth knowing from the implementation side:
it fires on the **ranking walk**, before any guild of that encounter is attempted, so
nothing paid for is discarded; and an encounter of the same pair that already
completed cleanly earlier in the run keeps what it wrote, because its own pages
carried the field. A renamed field affects every row of every page, so in practice
the first encounter of the pair alarms on page 1 and the pair writes nothing.
