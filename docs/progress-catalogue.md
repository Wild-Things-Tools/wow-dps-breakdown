# progress-catalogue: the data contract (v1, 2026-09-12)

Regal A's second folder -- the **catalogue**, Stufe 3 (Verzeichnis) and Stufe 2
(Gestalt), without names. `docs/warcraftlogs-konzept.md` chapters 3, 4.5 and 7 hold
the reasoning; where that file and this one disagree about the reasoning, it wins,
and where they disagree about the **format**, this one wins.

| role | what | where |
|---|---|---|
| producer | `wowdps catalogue` | this repository (public), GitHub Actions on free runners |
| consumer | every `wowdps` command that **selects** kills rather than evaluating them | this repository |

Transport is git, exactly as `docs/progress-cohort.md` describes it: the files live in
the **private** repository `Wild-Things-Tools/wtt-progress-data`, branch `main`,
directory `progress-catalogue/`, checked out into `./data` with the fine-grained PAT
`PROGRESS_DATA_PAT` (Contents read/write on that one repository). The public workflow
keeps `permissions: contents: read` and never commits to itself.

**A second folder rather than a second repository** is the owner's decision
(12.09.2026), with its own `--validate` rule and its own manifest. The two folders
share a repository and share nothing else: no file, no manifest, no schema version.

## Why it is private even though it carries no names

Because the line has to be **structural**, not procedural (Konzept 4.5). The scrub
below is a saving and a second belt; it is not the wall. A scrub is one forgotten
field away from failing silently, and a public repository is irreversible -- this
project has that measurement already, as 70 guild ids that sat in a public file
because a projection between an artifact and a published document did not exist.

## The split is a measurement, not a preference

The cohort folder splits per (zone, difficulty). The catalogue **cannot**, and the
reason is the one measured fact that makes Stufe 3 affordable at all:

```
REPORT_KILLS_QUERY takes exactly ONE variable, $code      (warcraftlogs.py)
  -> one request per report answers every boss AND every difficulty in it
FIGHT_STRUCTURE_QUERY takes code + encounter + difficulty (fightprobe.py:566)
  -> "this call is the only filter between finding a kill and asking to open it"
```

So:

| Stufe | file | split | why |
|---|---|---|---|
| 3 | `z<zone>.reports.jsonl` | per **zone** | a report's kill list is not per difficulty; writing it into two files would throw away the saving that makes the stage affordable, and filtering it into one would lose the other difficulty's kills for good |
| 2 | `z<zone>-d<diff>.kills.jsonl` | per (zone, difficulty) | the server-side filter is the only difficulty guard there is, and a file that claims one difficulty and carries two is not a thin answer but a false one |

Splitting Stufe 3 per difficulty is therefore **refused**, not merely not done.

## Files

```
progress-catalogue/
  README.md                      what this is, who writes it, who reads it
  manifest.json                  {"v":1,"files":[{"path":"z53.reports.jsonl","zoneId":53,
                                   "stage":3,"lines":N,"refused":M,"lastSweptAt":ISO|null},
                                  {"path":"z53-d5.kills.jsonl","zoneId":53,"difficulty":5,
                                   "stage":2,"lines":N,"refused":M,"lastSweptAt":ISO|null}, ...]}
                                 NO run timestamp at top level: a run that changes
                                 nothing must not change the manifest
  z<zone>.reports.jsonl          Stufe 3, one READ REPORT per line, append-only, UTF-8,
                                 "\n"-terminated, compact JSON (no spaces)
  z<zone>.refused.jsonl          Stufe 3 refusals, append-only
  z<zone>.state.json             the Stufe 3 cursor, rewritten atomically per window
  z<zone>-d<diff>.kills.jsonl    Stufe 2, one KILL per line, append-only
  z<zone>-d<diff>.refused.jsonl  Stufe 2 refusals, append-only
  z<zone>-d<diff>.state.json     the Stufe 2 cursor, rewritten atomically per report
```

Zone ids and encounter ids are Warcraft Logs **live** ids from
`worldData.zone(id:) { encounters { id name } frozen }` -- never PTR `5xxxx` ids,
never `fight_profiles.json`. `PTR_TWIN_ID_FLOOR = 50_000` is the refusal, and it is
the same constant `progresssweep` and wtt-backend's export carry; the three must stay
equal.

## A `reports.jsonl` line (Stufe 3)

```
{"v":1,"zoneId":53,"code":"aBcD…","startedAtMs":1723456000000,
 "kills":[{"e":3421,"d":5,"f":32,"sMs":1723456100000,"eMs":1723456534000}, …],
 "killsListed":57,"readAt":"2026-09-12T21:00:00+00:00","run":"34720902342"}
```

**There is no report end time here, and that is the document rather than an
omission**: `REPORT_KILLS_QUERY` selects `report { code startTime fights{…} }` and no
`endTime`. The last kill's `eMs` is a lower bound on it; anything more would have to
come from Stufe 2, which asks a different question.

- `kills` holds **only** `kill: true` fights, re-checked in the extraction even though
  `killType: Kills` is passed -- a filter that silently stopped filtering would put a
  wipe into a catalogue of kills, and `firstkills.kills_from_report` already carries
  that rule.
- `sMs`/`eMs` are **absolute**. `ReportFight.startTime` counts from the *report's*
  start, and this project has paid for that unit error once; a line whose time base
  cannot be established is refused, never written near the epoch.
- `killsListed` is **the number of rows the kill-filtered query returned**, before this
  producer's own re-check and refusals. It is what separates "this report holds no kill
  of anything" from "this report was not read" -- a line exists at all only because the
  report was read.

  **v1 of this document called it `fightsSeen` and described it as "every fight the
  report listed, kills and wipes". That is not obtainable from this query**, and the
  correction is worth keeping as a shape rather than quietly applied: `fights(killType:
  Kills)` filters SERVER-side, so a wipe never reaches us, and reading the whole list
  would need a second unfiltered query nothing here has asked for or priced. Three
  payload errors in this file were caught before it merged by reading the documents for
  **field presence**; this fourth one survived because it is about a field's **meaning
  under a server-side filter**, which reading the selection set does not show. Check what
  a query *filters* as well as what it *selects*.
- **No `title`.** `report.title` has zero readers across every Python file here
  (measured 2026-09-12) and is free-text a person wrote.

## A `kills.jsonl` line (Stufe 2)

```
{"v":1,"zoneId":53,"encounterId":3421,"difficulty":5,"code":"aBcD…","fightId":32,
 "name":"The Twin Fangs","startedAtMs":1723456100000,"lengthMs":434752,"size":20,
 "friendlyPlayers":[…ids…],
 "enemyNPCs":[{"id":11,"gameID":270898,"instanceCount":84,"groupCount":6}, …],
 "phaseTransitions":[{"id":1,"startTime":0}, …],
 "phases":[{"id":1,"name":"…","isIntermission":false}, …],
 "separatesWipes":true,
 "actors":[{"id":11,"gameID":270898,"type":"NPC","subType":"…","name":"Broodling of Ithraz","petOwner":null}, …],
 "abilities":[{"gameID":1246385,"name":"Avenging Wrath","type":1}, …],
 "readAt":"2026-09-12T21:00:00+00:00","run":"34720902342"}
```

Every field is what `FIGHT_STRUCTURE_QUERY` already returns; nothing is derived.

Two shape facts that are easy to get wrong, both read off the document rather than
assumed:

- **`report.phases` is per ENCOUNTER**, `{encounterID, separatesWipes, phases:[…]}`,
  and `_phase_metadata` picks out the entry whose `encounterID` matches. The line
  stores that entry's nested list plus its `separatesWipes`, not the outer array: a
  catalogue line is about one encounter, and carrying the whole array would put
  another boss's phase names under this one's key.
- **`phases` (report-wide names) and `phaseTransitions` (this fight's times) are
  neither of them a phase list alone**, which is why both are here.

## The scrub: what is dropped, and what is deliberately kept

Dropped at extraction, so it never reaches disk:

| field | why |
|---|---|
| `report.title` | free text a person wrote; zero readers |
| `masterData.actors[].name` **for a player-owned actor** | a character name is a quasi-identifier, and the catalogue answers no question that needs it |
| the ranking rows' `name`/`guild`/`server` | the catalogue is not built from rankings; if a producer ever reaches for them, they stop here |

Kept, and each is load-bearing rather than leftover:

- **`actors[].id`, `type`, `subType`, `petOwner`** -- `friendly_source_ids` follows
  ownership *transitively* to decide whether an aura is an encounter mechanic, and a
  type test alone separates nothing (a hunter's pet, a Mirror Image and a boss's add
  are all `Pet`). Dropping `petOwner` would put Mirror Image's Frostbolt back in a
  boss's aura list, which this project has shipped once.
- **`actors[].gameID` and the NPC `name`** -- an NPC name is not personal and is the
  only readable handle on "Broodling of Ithraz". Actor ids are report-local; `gameID`
  is what pools across reports.
- **`friendlyPlayers`** as ids only. The ids are what the player-aura filter needs;
  without them that filter is inoperative, which is a state the probe already warns
  about because it is otherwise invisible.
- **`abilities` whole, names included.** An ability name is what makes an aura
  readable ("Avenging Wrath", "Light Infused") and is not personal. `observe_fight`
  takes `ability_names`; a catalogue that dropped them would answer with spell ids.

**The scrub is per actor, not wholesale, and `actor_names` is why.** `_probe_fight`
builds `{actor id: name}` over **every** actor and hands it to `observe_fight`, which
is how "Broodling of Ithraz" gets its name; the map falls back to `str(id)` where a
name is missing, so removing a *player's* name costs that reader nothing and removing
an NPC's would cost it the only readable handle it has.

**A player-actor name reaching the writer is a schema alarm, not a bug to log**: the
pair writes nothing, the run continues, exit 3. A scrub that fails open is the one
failure this folder's privacy cannot absorb.

## `state.json`

```
z<zone>.state.json
{"v":1,"zoneId":53,"zoneName":"The Venomous Abyss","frozen":false,"stage":3,
 "windows":[{"fromMs":…,"toMs":…,"pagesRead":5,"reportsSeen":500,"walled":true,
             "walledBy":"our-page-limit"|"service-page-cap",  // only when walled
             "sweptAt":ISO}],
 "codesKnown":1234,"refusedCodes":7}

z<zone>-d<diff>.state.json
{"v":1,"zoneId":53,"difficulty":5,"stage":2,
 "encounters":{"3421":{"name":"The Twin Fangs","killsKnown":211,"killsRead":36,
   "cursorStartedAtMs":1723456100000|null,"sweptAt":ISO,"stoppedOnBudget":false,
   "outcomes":{"read":36,"difficulty-mismatch":0,"structure-error":1}}}}
```

- **The Stufe 2 cursor is a kill's absolute `startedAtMs`**, not a position. A report
  can be deleted or set private and every later position shifts by one; a start time
  belongs to the kill and stays a valid boundary when the row it came from is gone.
  Same argument, and the same wording, as the cohort cursor's `killTime`.
- **`sweptAt` is the encounter's own time, not the run's.** On a five-hour run the
  last encounter would otherwise be stamped five hours stale and re-walked early --
  measured on the cohort sweep and fixed there; do not repeat it.
- **`walled`** on a Stufe 3 window means a page limit was reached with the page
  still full. It is not exhaustion: `fight-probe` already records the difference
  (`searchExhausted` against `searchBudget`) because a boss nobody has killed would
  otherwise read as a boss with no more kills to find.
- **`walledBy` says WHICH page limit**, and that is the difference between a window
  somebody can do something about and one nobody can. `our-page-limit` is
  `--report-pages`: raise it and dispatch again. `service-page-cap` is **Warcraft
  Logs refusing page 26** --

      The maximum allowed page is 25 until the performance of paginated queries
      can be improved.

  -- measured live on 2026-09-12 (run 34723281158, zone 53, dispatched at
  `--report-pages 40`). It is a GraphQL error rather than a short page, so the walk
  did not stop at that wall, it **crashed on it** with 25 pages already paid for;
  `catalogue.MAX_REPORT_PAGE` is now where the walk stops and the CLI refuses a
  larger `--report-pages` instead of clamping it. At any `--report-limit` the cap
  puts **2,500 reports on one window**, so past it the only route further is a
  narrower `fromMs`/`toMs` -- which is what the window list has always had the shape
  for and which this command does not yet take an option for.
  **The field is emitted only where there IS a wall.** A window that ran out of
  reports has none, and a window written before 2026-09-13 was walled and cannot say
  by what -- absent is that third state rather than a fourth value nobody measured.

## Refusals

Each one writes a `refused.jsonl` line naming the thing and the reason, never a silent
skip -- the rule this repository already applies to `unplaced` items, `legacy` rings
and `staleRows`.

| reason | when |
|---|---|
| `ptr-id` | an encounter id at or above `PTR_TWIN_ID_FLOOR`. The import cannot catch this one: a catalogue row under a PTR id joins to a live catalogue and lands under the wrong zone |
| `unknown-time-base` | a fight whose absolute start cannot be computed (no report start) |
| `difficulty-mismatch` | a fight stating a difficulty other than the file's. The fetch is already scoped, so this means the scoping did not hold. A fight stating **none** is allowed through -- unknown is not wrong, `harvest`'s three-way rule |
| `not-a-kill` | `kill` is false on a row the kill filter returned |
| `structure-error` | `FIGHT_STRUCTURE_QUERY` refused or failed for that kill |
| `report-error` | `REPORT_KILLS_QUERY` refused or failed for that report |

## Schema alarms (fail closed, per file set)

- a player-owned actor arriving **with a name** -> the file set writes nothing (no
  lines, no state), the run continues, exit 3;
- a `masterData` block that is absent entirely -> the same. A fight with no actor list
  makes the player-aura filter inoperative downstream, and a catalogue line that
  quietly carries none is worse than a missing line;
- the response cache is **on** for this producer and that is deliberate: every
  question it asks is about an immutable thing (a report's fights, a fight's shape).
  It is the opposite of the cohort sweep, whose rankings are the thing it re-reads.

## Budget and cadence

- The ceiling, the sleep-until-reset and the deadline are `progresssweep`'s, ported
  rather than re-derived: absolute counter, refreshed before every window and every
  report batch; a ceiling stop sleeps and continues; `--deadline-minutes` ends the run
  with what it has; exit 1 only when the FIRST reading fails, because then nothing was
  paid for.
- **Priced, and the price is a floor.** 500 reports per encounter at the measured
  1.66 points per query is **~830 points** -- 4.6% of an 18,000 hour, 23% of a 3,600
  one. That is Stufe 3 for one encounter's window; Stufe 2 is one query per kill on
  top. What a zone's catalogue costs in full is **UNMEASURED**, and the reason
  changed on 2026-09-12: it is no longer "nobody knows how many reports a zone holds"
  but "one window serves at most 2,500 of them, and nobody has measured how many
  windows a zone needs". Do not put this on a cron before that number exists.
- **Half the cost of a page walk is the guard.** Measured on the same run: 52 queries
  for a 25-page walk, 26 of them `Budget.check()` reading the absolute counter before
  every page. Left alone deliberately -- the cohort sweep already paid this tax on
  purpose, and a ceiling argued from a stale number is what a 429 costs.
- **Stufe 3 is built per zone ONCE and then only appended to**, never per boss again.
  The decision is the Konzept's and it follows from the query taking only `$code`.
- Order of work, when a run cannot do everything: Stufe 3 for the **live** zone first
  (cheapest per finding, and a report can be set private), then Stufe 2 broadly.
  Stufe 2 loses nothing by waiting; a report that disappears loses everything.

## What this folder is NOT

- **Not Stufe 1.** No events, ever. The raw cut stays the `actions/cache` plus a
  14-day artifact (Konzept, Regal B), and the owner's decision 4 keeps it that way
  while the cost does not rise.
- **Not a replacement for `fights.json` or `spawns.json`.** Those are published
  *answers*; this is the material they are selected from.
- **Not a rankings mirror.** The catalogue never stores a ranking row. A ranking moves
  and is the one thing a re-read exists for.

## Open measurements this depends on

Both are in issue #170 and both need a live query:

- ~~**How many reports does a zone really hold?**~~ **Answered on 2026-09-12, and
  the answer is a ceiling rather than a count**: `reportData.reports` refuses page 26,
  so one window serves 2,500 and no more. Zone 53 filled all 25 pages, so its own
  figure is a lower bound of 2,500 and not a measurement of the zone. The question
  that replaces it is **how many time windows a zone needs**, which needs
  `--from`/`--to` on this command before it can be asked. Without that number "every
  kill" is still not priceable and the cadence still cannot be set.
- **Does `includeResources` cost points, and does an unfiltered `FIGHT_STRUCTURE`
  cost more than a filtered one?** The second bears on this folder directly: the
  Konzept refuses the unfiltered form on correctness grounds, and the measurement
  decides only how much that refusal costs.

## Status

**The command, the gate and the workflow are built** (`wowdps catalogue`,
`catalogue.py`, `.github/workflows/catalogue.yml`), and building them found the
`killsListed` correction above.

**The workflow is `workflow_dispatch` only, and the distinction is load-bearing.**
A first draft of this paragraph said the workflow was deliberately NOT built
"because this document already refuses a cron without that number" -- which reads
the refusal as bigger than it is. What is refused above is a **cron**, and a
dispatch-only run is precisely the instrument that takes the measurement.

**It has taken it, and the run that took it found three defects rather than one.**
Run 34723281158, 2026-09-12, zone 53 at `--report-pages 40`:

1. the service's own page cap, above -- the question is answered and the answer is
   a ceiling;
2. **a `WarcraftLogsError` from a sweep escaped `run_catalogue`.** `_zone_block` a
   dozen lines above carries exactly that clause and the sweeps beside it did not,
   so the refusal left the command as a traceback: exit 1, which the workflow fails
   the step on, and **no summary file**;
3. and the commit message, computed from that absence, read *"+0 reports, +0 kills,
   +0 refusals, UNMEASURED points, 0 queries"* over a run that had paid for 25 pages
   and written a window. That is the worst of the three: a traceback is loud, and a
   committed record asserting zeros is not. Both the guard and the message are
   fixed; UNMEASURED, never zero, applies to a commit message too.

The cron stays commented out in the workflow, now with the narrower reason beside it.

Worth keeping as a shape, because it is this file's own failure pointing inward: a
refusal restated one paragraph later grew from "not on a cron" to "not at all", and
the larger version would have blocked the run that lifts it.

The contract was written **first**, the way `docs/progress-cohort.md` was, because an
append-only format committed into a private repository is not a thing to iterate on
afterwards. A disagreement between the code and this file is a defect in the code --
except where the code found this file wrong, which is recorded here rather than fixed
silently.
