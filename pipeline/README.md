# Pipeline

Turns SimulationCraft into the static JSON the site reads.

```
profiles.py    → which builds exist (class, spec, hero talent) from simc's tier profiles
scenarios.py   → which fights to run them in, and at how many targets
simc_runner.py → invoke simc, collect json2
parse.py       → the metrics: concentration, ability shares, timeline, burst
                 (funnel gain is derived in dataset.py — it needs the 1-target baseline)
dataset.py     → write index.json + specs/<id>.json
warcraftlogs.py→ optional cross-check against real raid rankings
```

## Commands

```bash
wowdps list                       # what would be simulated
wowdps build --out <dir>          # run sims, write the dataset
wowdps merge <shard…> --out <dir> # combine parallel shards (CI)
wowdps verify --data <dir>        # Warcraft Logs comparison (needs API credentials)
```

Run `wowdps <command> --help` for the full flag list. The project README covers the
methodology and how CI drives all of this on a schedule.

## Output contract

`index.json` carries metadata, the scenario definitions and one summary row per build —
enough to render the ranking view without fetching anything else. `specs/<id>.json` holds
the full grid for one build: every scenario × target count, with ability breakdown,
timelines at representative target counts, and any simulation failures recorded inline.

The TypeScript mirror of this shape lives in `web/src/lib/types.ts`. Change both together.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

The suite covers funnel gain versus concentration (including the cases where they
disagree), the arithmetic against hand-computed values, profile
identification against every simc class token, timeline truncation, ability merging, and
that sharding partitions the matrix exactly once however it is split.
