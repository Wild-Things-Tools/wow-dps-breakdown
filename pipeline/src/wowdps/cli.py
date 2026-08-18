"""Command line entry point: ``wowdps <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import (
    dataset,
    equipment,
    fightdataset,
    fightprofile,
    fightzones,
    gearsweep,
    profiles,
    scenarios,
    simc_runner,
)
from .scenarios import SimSettings

DEFAULT_OUT = Path("web/public/data")

#: Target counts the gear sweep runs at unless asked otherwise. One target is the
#: raid-loot question these numbers exist to answer; more is a flag away and costs
#: proportionally.
DEFAULT_GEAR_TARGETS = (1,)

#: Iterations per gear variant, and deliberately a third of what `build` uses.
#:
#: The 3000 default elsewhere exists to resolve sub-percent gaps *between specs* in a
#: dataset that is committed nightly. This sweep measures trinket differences, which
#: run 1-5% of DPS, and it has to be cheap enough to re-run after every tuning pass
#: rather than once a season. 1000 deterministic iterations measure to about 0.15%
#: standard error against 0.09% at 3000 -- both far inside the effect -- for a third
#: of the cost. Verified on Arcane Mage: the two settings rank the raid trinkets
#: identically. Raise it with --max-iterations for an official run.
DEFAULT_GEAR_ITERATIONS = 1000


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )


def _resolve_tier(profiles_dir: Path, requested: str | None) -> str:
    """``latest`` and ``previous`` resolve against what simc ships; anything else is literal.

    Both tokens exist so a scheduled run can say what it means -- "the current tier",
    "last season" -- and keep meaning it after the next tier ships.
    """
    if not requested or requested == "latest":
        return profiles.latest_tier(profiles_dir)
    if requested == "previous":
        return profiles.previous_tier(profiles_dir)
    return requested


def _parse_shard(spec: str | None) -> tuple[int, int] | None:
    """``"2/6"`` -> (2, 6). Shard indices are zero-based."""
    if not spec:
        return None
    try:
        index_text, count_text = spec.split("/", 1)
        index, count = int(index_text), int(count_text)
    except ValueError:
        raise SystemExit(f"--shard must look like 0/6, got {spec!r}") from None
    if count < 1 or not 0 <= index < count:
        raise SystemExit(f"--shard {spec!r} is out of range")
    return index, count


def _select(
    all_profiles: list[profiles.SpecProfile],
    classes: list[str] | None,
    spec_ids: list[str] | None,
    limit: int | None,
    shard: tuple[int, int] | None = None,
) -> list[profiles.SpecProfile]:
    selected = all_profiles
    if classes:
        wanted = {c.lower().replace("_", " ").replace("-", " ") for c in classes}
        selected = [p for p in selected if p.wow_class.lower() in wanted]
    if spec_ids:
        wanted_ids = set(spec_ids)
        selected = [p for p in selected if p.id in wanted_ids]
    if shard:
        # Round-robin rather than contiguous blocks: profiles are sorted by class,
        # and classes differ in how many builds they have, so contiguous slices
        # would leave some CI shards with twice the work of others.
        index, count = shard
        selected = selected[index::count]
    if limit:
        selected = selected[:limit]
    return selected


def cmd_list(args: argparse.Namespace) -> int:
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    found = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    print(f"tier {tier}: {len(found)} profiles")
    for profile in found:
        print(f"  {profile.id:<45} {profile.display_name:<45} role={profile.role}")
    return 0


#: Scenario name that expands to every boss the tier's profiles know something
#: about. Spelled as a word rather than a flag so `--scenario` stays the one place
#: a run's scenario set is decided.
BOSS_SCENARIO_TOKEN = "bosses"


def _resolve_scenarios(
    names: list[str] | None, tier: str, profiles_file: str | None = None
) -> list[scenarios.Scenario]:
    """Scenario objects for the names given, built-in or boss.

    Boss scenarios come from `fight_profiles.json` rather than from the built-in
    table, because a boss's shape is data that changes with the tier while the four
    fight styles are code. Loading the profiles is deferred to the point where a
    boss name is actually asked for, so a plain run never touches the file.
    """
    if not names:
        return list(scenarios.ALL_SCENARIOS)

    wants_boss = any(name == BOSS_SCENARIO_TOKEN or name.startswith("boss_") for name in names)
    available: dict[str, scenarios.Scenario] = {}
    if wants_boss:
        loaded = fightprofile.load_profiles(tier, Path(profiles_file) if profiles_file else None)
        available = fightprofile.boss_scenarios(loaded)
        if not available:
            empty = (
                f"no boss in {tier} has a fight profile with anything asserted or "
                f"measured in it, so there is no boss scenario to run. "
                f"`wowdps fight-probe` measures them; `wowdps fight-promote` writes "
                f"a measurement into a profile."
            )
            # Fatal only when the bosses are all that was asked for. As one entry in
            # a scenario list it is an ordinary state -- a season whose raid has not
            # opened has no boss to sim -- and failing there took down all twelve
            # shards of a nightly run that had four other scenarios to do. That
            # happened on 2026-08-18, one day after the re-file moved MID2's
            # asserted bosses to MID1 and left MID2 with eight factless encounters.
            others = [
                name
                for name in names
                if name != BOSS_SCENARIO_TOKEN and not name.startswith("boss_")
            ]
            if not others:
                raise KeyError(empty)
            logging.warning("%s Running the other %d scenario(s).", empty, len(others))

    resolved: list[scenarios.Scenario] = []
    for name in names:
        if name == BOSS_SCENARIO_TOKEN:
            resolved.extend(available.values())
        elif name in available:
            resolved.append(available[name])
        elif name.startswith("boss_"):
            known = ", ".join(sorted(available)) or "none"
            raise KeyError(f"unknown boss scenario {name!r}; this tier offers: {known}")
        else:
            resolved.append(scenarios.get(name))

    # Repeating a name is a typo, not a request to sim it twice.
    unique: dict[str, scenarios.Scenario] = {}
    for scenario in resolved:
        unique.setdefault(scenario.id, scenario)
    return list(unique.values())


def cmd_build(args: argparse.Namespace) -> int:
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)
    # Datasets are namespaced by tier: a tier is a different game state, not a filter.
    out_root = Path(args.out)
    out_dir = out_root / tier

    try:
        selected_scenarios = _resolve_scenarios(args.scenario, tier, args.profiles_file)
    except KeyError as exc:
        logging.error("%s", exc.args[0])
        return 1

    boss_profiles = (
        fightprofile.load_profiles(
            tier, Path(args.profiles_file) if args.profiles_file else None
        ).profiles
        if any(s.id.startswith("boss_") for s in selected_scenarios)
        else {}
    )

    all_profiles = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    selected = _select(
        all_profiles, args.wow_class, args.spec, args.limit, _parse_shard(args.shard)
    )
    if not selected:
        logging.error("no profiles matched the selection")
        return 1

    settings = SimSettings(
        target_error=args.target_error,
        max_iterations=args.max_iterations,
        threads=args.threads,
    )

    # A boss whose profile carries no add waves, no representable amplification and
    # the default fight length sims as N static targets for 300s -- which is the
    # target sweep's own cell with a boss's name on it. Correct number, unearned
    # label, so it is said out loud rather than discovered in the output.
    for scenario in selected_scenarios:
        if not scenario.id.startswith("boss_"):
            continue
        profile = boss_profiles.get(int(scenario.id.removeprefix("boss_")))
        if profile is None:
            continue
        plan = profile.to_plan()
        if plan.restates_a_static_sweep():
            logging.warning(
                "%s adds nothing to the target sweep: %d static target(s) for %ds, "
                "which is Patchwerk at %d. Its profile has no add waves and no "
                "amplification simc can express%s",
                scenario.id,
                plan.targets,
                plan.max_time,
                plan.targets,
                f" ({plan.unrepresented[0]})" if plan.unrepresented else "",
            )

    total_sims = sum(len(s.sims()) for s in selected_scenarios) * len(selected)
    logging.info(
        "tier %s | %d specs x %d scenarios = %d sims | simc %s",
        tier,
        len(selected),
        len(selected_scenarios),
        total_sims,
        simc,
    )

    results: list[dataset.SpecResult] = []
    simc_meta: dict = {}

    for index, profile in enumerate(selected, start=1):
        logging.info("[%d/%d] %s", index, len(selected), profile.display_name)
        result = dataset.run_spec(simc, profile, selected_scenarios, settings, timeout=args.timeout)
        if not result.cells:
            logging.error("  no successful sims for %s, skipping", profile.id)
            continue
        dataset.write_spec(out_dir, result)
        results.append(result)

        if not simc_meta:
            # Metadata is identical across sims; capture it from the first success.
            probe = simc_runner.SimRequest(
                profile=profile, scenario=selected_scenarios[0], targets=1
            )
            try:
                report = simc_runner.run(
                    simc,
                    probe,
                    SimSettings(target_error=0, max_iterations=10),
                    timeout=300,
                )
                simc_meta = simc_runner.simc_metadata(report)
            except Exception as exc:  # noqa: BLE001
                logging.warning("could not capture simc metadata: %s", exc)

    if not results:
        logging.error("every spec failed; not writing a manifest")
        return 1

    # Safe from a shard: `spec_coverage` reads what simc *ships* for the tier, which
    # is the whole profiles directory and has nothing to do with which slice this run
    # simulated. Every shard therefore computes the same answer, and the merge keeping
    # the newest manifest keeps a correct one.
    coverage = profiles.spec_coverage(profiles_dir, tier)
    manifest = dataset.write_manifest(
        out_dir, results, selected_scenarios, tier, simc_meta, settings, coverage
    )
    # An unsharded run is the whole run, so the third coverage state -- shipped by
    # simc and produced nothing -- can be settled here. A sharded run gets it in
    # `merge_shards` instead, where the union of the slices is known.
    if not getattr(args, "shard", None):
        document = json.loads(manifest.read_text(encoding="utf-8"))
        dataset.apply_simulated_coverage(document)
        manifest.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    dataset.write_tier_index(out_root)
    failed = sum(len(r.errors) for r in results)
    logging.info("wrote %s (%d specs, %d failed cells)", manifest, len(results), failed)
    return 0


def cmd_gear(args: argparse.Namespace) -> int:
    """Sweep one equipment slot: which drops are an upgrade over what is already worn."""
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)
    out_dir = Path(args.out) / tier

    pools = equipment.load_pools(tier, Path(args.pools) if args.pools else None)
    wanted_slots = args.slot or sorted(pools.slots)
    missing = [slot for slot in wanted_slots if slot not in pools.slots]
    if missing:
        logging.error("no pool defined for slot(s) %s in tier %s", ", ".join(missing), tier)
        return 1

    all_profiles = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    selected = _select(
        all_profiles, args.wow_class, args.spec, args.limit, _parse_shard(args.shard)
    )
    if not selected:
        logging.error("no profiles matched the selection")
        return 1

    settings = SimSettings(
        target_error=args.target_error,
        max_iterations=args.max_iterations,
        threads=args.threads,
    )
    targets = args.targets or list(DEFAULT_GEAR_TARGETS)

    logging.info(
        "tier %s | %d specs x %d slot(s) x %d target count(s) | simc %s",
        tier,
        len(selected),
        len(wanted_slots),
        len(targets),
        simc,
    )

    results: list[gearsweep.SpecSlotResult] = []
    simc_meta: dict = {}
    for index, profile in enumerate(selected, start=1):
        logging.info("[%d/%d] %s", index, len(selected), profile.display_name)
        for slot_id in wanted_slots:
            result = gearsweep.sweep_spec(
                simc, profile, pools.slots[slot_id], settings, targets, timeout=args.timeout
            )
            if result.targets:
                results.append(result)
            else:
                logging.error("  no successful gear sims for %s / %s", profile.id, slot_id)

        if not simc_meta and results:
            simc_meta = _probe_simc_metadata(simc, profile)

    if not results:
        logging.error("every spec failed; not writing a gear dataset")
        return 1

    path = dataset.write_gear(
        out_dir,
        results,
        {slot: pools.slots[slot] for slot in wanted_slots},
        tier,
        simc_meta,
        settings,
        specs_available=len(all_profiles),
    )
    failed = sum(len(result.errors) for result in results)
    logging.info(
        "wrote %s (%d spec-slot results of %d profiles, %d failures)",
        path,
        len(results),
        len(all_profiles),
        failed,
    )
    return 0


def cmd_gear_candidates(args: argparse.Namespace) -> int:
    """Enumerate what simc's item tables offer for a slot, so a pool can be curated.

    Prints everything the pool file needs except ``source``: simc's shipped data
    carries no drop source at all (see equipment.py), so that column is the one thing
    a human or an external item database has to supply.
    """
    slot = equipment.SLOTS_BY_ID[args.slot]
    found = equipment.discover_items(Path(args.simc_source), slot.inventory_type)
    if args.min_ilevel:
        found = [item for item in found if item.base_ilevel >= args.min_ilevel]
    if args.effects_only:
        found = [item for item in found if item.has_effect]

    print(f"{len(found)} {slot.label.lower()} item(s); source is NOT in simc's data")
    print(f"{'id':>7} {'base':>5} {'q':>2} {'effect':>6}  {'primary':<12} {'secondary':<11} name")
    for item in found:
        print(
            f"{item.item_id:>7} {item.base_ilevel:>5} {item.base_quality:>2} "
            f"{'yes' if item.has_effect else 'no':>6}  {item.primary_stat or '-':<12} "
            f"{item.secondary_stat or '-':<11} {item.name}"
        )
    return 0


def _probe_simc_metadata(simc: Path, profile: profiles.SpecProfile) -> dict:
    """Version and build info, from a throwaway one-iteration run."""
    probe = simc_runner.SimRequest(profile=profile, scenario=scenarios.PATCHWERK, targets=1)
    try:
        report = simc_runner.run(
            simc, probe, SimSettings(target_error=0, max_iterations=10), timeout=300
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("could not capture simc metadata: %s", exc)
        return {}
    return simc_runner.simc_metadata(report)


def cmd_merge(args: argparse.Namespace) -> int:
    shard_dirs = [Path(p) for p in args.shards]
    missing = [p for p in shard_dirs if not p.is_dir()]
    if missing:
        logging.error("shard directories not found: %s", missing)
        return 1

    # Shards are written by `build`, so each one already carries its tier directory.
    # Merging is per tier; the union of the tiers present across the shards tells us
    # which ones to merge without the caller having to say.
    out_root = Path(args.out)
    tiers = sorted({d.name for shard in shard_dirs for d in shard.iterdir() if d.is_dir()})
    if not tiers:
        logging.error("no tier directories inside %s", shard_dirs)
        return 1

    for tier in tiers:
        sources = [shard / tier for shard in shard_dirs if (shard / tier).is_dir()]
        dataset.merge_shards(sources, out_root / tier)
    dataset.write_tier_index(out_root)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from . import warcraftlogs

    return warcraftlogs.cmd_verify(args)


def cmd_fight_profiles(args: argparse.Namespace) -> int:
    """Print the fight profiles for a tier and the simc scenario each one produces.

    Offline on purpose: this is the half of the fight-profile work that needs no
    credentials, so the shape of what the probe is aiming at can be inspected and
    argued with from a development checkout.
    """
    tier_profiles = fightprofile.load_profiles(
        args.tier, Path(args.profiles_file) if args.profiles_file else None
    )
    if not tier_profiles.profiles:
        print(f"no fight profiles defined for tier {args.tier}")
        return 1

    print(f"tier {args.tier}: {len(tier_profiles.profiles)} encounter(s)")
    for profile in tier_profiles.profiles.values():
        plan = profile.to_plan()
        known = sorted(profile.facts)
        print(f"\n{profile.name} (encounter {profile.encounter_id})")
        print(
            f"  facts: {', '.join(known) if known else 'none -- nothing measured or asserted yet'}"
        )
        for key in known:
            print(f"    {key}: {profile.facts[key].provenance.summary()}")
        print(f"  simc: desired_targets={plan.targets} max_time={plan.max_time}")
        for option in plan.options:
            print(f"        {option}")
        for missing in plan.unrepresented:
            print(f"  not modelled: {missing}")
    return 0


def cmd_fight_zones(args: argparse.Namespace) -> int:
    """Read Warcraft Logs' zone list and say which season each boss list belongs to.

    The cheapest query in the project -- one document, no per-fight events -- and
    the one that keeps a whole season's fight data from being filed under the wrong
    label. It answers two questions a checkout cannot answer offline: which raids
    Warcraft Logs is currently ranking, and which raid the encounter ids already in
    ``fight_profiles.json`` actually came from.

    Read-only unless ``--seed`` or ``--move`` is passed, and both of those name what
    they are doing rather than inferring it. The suggestion this prints is an
    inference over zone order and the ``frozen`` flag; the writes are a person's
    decision.
    """
    from .warcraftlogs import Credentials, WarcraftLogsClient, WarcraftLogsError

    path = Path(args.profiles_file) if args.profiles_file else fightzones._data_file()
    raw = json.loads(path.read_text(encoding="utf-8"))

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        logging.error("%s", exc)
        return 1

    with WarcraftLogsClient(credentials) as client:
        zones = fightzones.parse_zones(client.zones())
        ledger = client.ledger

    if not zones:
        logging.error("Warcraft Logs returned no zones")
        return 1

    live = [zone for zone in zones if not zone.frozen]
    print(f"{len(zones)} zone(s), {len(live)} still being ranked\n")
    # Highest id first: the list arrives newest-first, but sorting by id says so
    # explicitly rather than relying on it -- and `--show N` should mean "the N
    # newest", which taking a slice of the tail did not.
    for zone in sorted(zones, key=lambda entry: entry.zone_id, reverse=True)[: args.show]:
        state = "frozen" if zone.frozen else "live"
        print(f"  [{zone.zone_id}] {zone.name} -- {state}, {len(zone.encounters)} encounter(s)")
        if args.verbose_zones:
            for encounter in zone.encounters:
                print(f"        {encounter.encounter_id:>6}  {encounter.name}")

    suggestion = fightzones.suggest_current_zone(zones)
    print(f"\ncurrent season looks like: {suggestion.reason}")

    print("\nwhere each tier's filed encounters actually live:")
    for tier, entry in sorted((raw.get("tiers") or {}).items()):
        ids = [
            int(item["encounterId"])
            for item in entry.get("encounters") or []
            if item.get("encounterId") is not None
        ]
        placement = fightzones.locate(tier, ids, zones)
        print(f"  {tier}: {len(ids)} encounter(s) -> {placement.zone_names}")
        for zone, hits in placement.zones:
            if zone.frozen:
                print(
                    f"    ! {zone.name} is frozen -- {hits} of {tier}'s bosses belong to a "
                    "season that has ended"
                )
        if placement.unplaced:
            print(f"    ? not in any zone: {sorted(placement.unplaced)}")

    if args.move:
        source, destination = args.move
        moved = fightzones.move_tier(raw, source, destination)
        print(f"\nmoved {moved} encounter(s) from {source} to {destination}")

    if args.scan:
        # There is no endpoint that enumerates *every* zone: `worldData.zones`
        # answers "what is currently ranked" and leaves out at least the PTR zones
        # (54 is real and absent from it). `worldData.zone(id:)` reaches any of them
        # one at a time, so walking a range of ids is the enumeration -- derived,
        # not guessed, and cheap: one query per id, and the ids are small integers.
        low, high = args.scan
        print(f"\nscanning zone ids {low}-{high} directly, past what the list returns:")
        with WarcraftLogsClient(credentials) as scan_client:
            for zone_id in range(low, high + 1):
                if any(entry.zone_id == zone_id for entry in zones):
                    continue
                fetched = scan_client.zone(zone_id)
                found = next(iter(fightzones.parse_zones([fetched] if fetched else [])), None)
                if not found:
                    continue
                state = "frozen" if found.frozen else "live"
                print(
                    f"  [{found.zone_id}] {found.name} -- {state}, "
                    f"UNLISTED, {len(found.encounters)} encounter(s)"
                )
                for encounter in found.encounters:
                    print(f"        {encounter.encounter_id:>6}  {encounter.name}")

    if args.seed:
        zone = next((entry for entry in zones if entry.zone_id == args.seed), None)
        if zone is None:
            # The list is not an enumeration. Zone 54 -- Season 2's PTR zone -- is
            # real and is not in it, so "not in the list" must mean "ask directly"
            # rather than "does not exist"; concluding the latter is exactly the
            # mistake this branch was making.
            logging.info("zone %d is not in the list; asking for it directly", args.seed)
            with WarcraftLogsClient(credentials) as client:
                fetched = client.zone(args.seed)
            zone = next(iter(fightzones.parse_zones([fetched] if fetched else [])), None)
        if zone is None:
            logging.error("Warcraft Logs has no zone %d", args.seed)
            return 1
        print(
            f"\nzone {zone.zone_id}: {zone.name} -- "
            f"{'frozen' if zone.frozen else 'live'}, {len(zone.encounters)} encounter(s)"
        )
        for encounter in zone.encounters:
            print(f"    {encounter.encounter_id:>6}  {encounter.name}")
        result = fightzones.seed_tier(raw, args.tier, zone, difficulty=args.difficulty)
        print(f"\nseeded {args.tier} from {zone.name}:")
        for encounter in result.added:
            print(f"  + {encounter.encounter_id:>6}  {encounter.name}")
        if result.kept:
            print(f"  = {len(result.kept)} already filed, left untouched")
        if result.absent:
            print(
                f"  ? {len(result.absent)} filed under {args.tier} but not in "
                f"the zone: {result.absent}"
            )

    if args.write and (args.seed or args.move):
        written = fightzones.write_profiles(raw, path)
        print(f"\nwrote {written}")
    elif args.seed or args.move:
        print("\nnothing written -- pass --write to apply")

    cost = ledger.spent
    # A zero delta is reported as unmeasured rather than as a number: the counter
    # not moving is the absence of a measurement, and printing "0 points" invites
    # the conclusion that the API is free. Same rule as the probe's ledger.
    reading = "UNMEASURED (the hourly counter did not move)" if not cost else f"{cost:.1f} points"
    print(f"\ncost: {reading}, {len(ledger.entries)} query/queries")
    return 0


def cmd_wcl_schema(args: argparse.Namespace) -> int:
    """Introspect the Warcraft Logs schema and print what it offers.

    Written for one open question -- is there a route to the *first* kill that does
    not go through a damage-sorted ranking -- but it is general. Everything in this
    project that talks to Warcraft Logs was written against a third-party schema
    mirror, so "does this field exist" has always been answered by trying it. This
    asks instead, which is the same discipline the loot pools and the boss lists
    already follow.

    Fields, arguments and enum values whose names bear on ordering by time or
    progress are marked with ``*``, because that is the half of the output somebody
    running this is looking for.
    """
    from . import wclschema
    from .warcraftlogs import Credentials, WarcraftLogsClient, WarcraftLogsError

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        logging.error("%s", exc)
        return 1

    names = args.type or list(wclschema.DEFAULT_TYPES)
    missing: list[str] = []
    errored: list[str] = []

    with WarcraftLogsClient(credentials) as client:
        for name in names:
            try:
                data = client.query(
                    wclschema.TYPE_QUERY, {"name": name}, label=f"introspect:{name}"
                )
            except WarcraftLogsError as exc:
                # A name the schema does not have comes back as an *error*, not as
                # a null `__type` -- measured on the first live run, where
                # `EncounterRankings` (a guess: the ranking fields are untyped JSON,
                # so no such object type exists) returned "Internal server error"
                # and aborted the whole pass on its second query. One bad guess must
                # not cost the answers for every other type, so this is recorded and
                # the walk continues.
                logging.warning("introspection errored on %s: %s", name, exc)
                errored.append(name)
                print(f"\n=== {name} (ERRORED)")
                print(f"  the server refused this name: {exc}")
                print("  most likely no such type -- check the spelling against a field's own type")
                continue

            payload = (data or {}).get("__type")
            if not payload:
                missing.append(name)
            print(f"\n=== {name} ({(payload or {}).get('kind', 'ABSENT')})")
            for line in wclschema.describe_type(payload):
                print(line)

        ledger = client.ledger

    if missing:
        print(f"\nabsent from this schema: {', '.join(missing)}")
    if errored:
        print(f"\nthe server errored on: {', '.join(errored)}")
    cost = ledger.spent
    reading = "UNMEASURED (the hourly counter did not move)" if not cost else f"{cost:.1f} points"
    print(f"\ncost: {reading}, {len(ledger.entries)} query/queries")
    return 0


def cmd_spec_index(args: argparse.Namespace) -> int:
    """Publish ``<tier>/spec-index.json``: every class and spec in the game.

    The Spec detail picker draws the whole game rather than the tier's build list,
    so a spec's absence reads as absence. Everything in it is derived -- see
    ``specindex`` for which file answers which question, and for the one thing simc
    does not carry at all, which is what a hero tree is called.
    """
    from . import specindex

    simc_dir = Path(args.simc_source)
    out_root = Path(args.out)
    tier = _resolve_tier(simc_dir / "profiles", args.tier)

    manifest_path = out_root / tier / "index.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    )
    if manifest is None:
        logging.warning("no manifest at %s; the picker will show no builds", manifest_path)

    talents_path = out_root / tier / "talent-trees.json"
    tree_names: dict[int, str] = {}
    if talents_path.is_file():
        tree_names = specindex.tree_names_from_talents(
            json.loads(talents_path.read_text(encoding="utf-8"))
        )
    else:
        logging.warning(
            "no %s, so no hero tree can be named -- run `wowdps talent-trees` first",
            talents_path,
        )

    ptr = bool((manifest or {}).get("simc", {}).get("ptr"))
    document = specindex.build_index(simc_dir, tier, manifest, tree_names, ptr=ptr)
    path = specindex.write_spec_index(out_root / tier, document)

    specs = [spec for entry in document["classes"] for spec in entry["specs"]]
    roles: dict[str, int] = {}
    for spec in specs:
        roles[spec["role"]] = roles.get(spec["role"], 0) + 1
    named = sum(1 for tree in document["heroTrees"] if tree["name"])
    print(
        f"wrote {path}: {len(document['classes'])} classes, {len(specs)} specs "
        f"({', '.join(f'{count} {role}' for role, count in sorted(roles.items()))}), "
        f"{len(document['heroTrees'])} hero trees of which {named} are named"
    )
    return 0


def cmd_buffs(args: argparse.Namespace) -> int:
    """Sweep tier set bonuses and Power Infusion, per spec.

    The two questions a spreadsheet usually answers and nobody can check: what a
    class's tier set is worth, split into its two- and four-piece halves, and what
    an outside Power Infusion is worth on each spec. Both are profileset sweeps
    against the spec's own profile, so both come out as differences with the run's
    own precision attached.
    """
    from . import buffsweep

    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)
    settings = SimSettings(
        target_error=args.target_error, max_iterations=args.max_iterations, threads=args.threads
    )

    sets = buffsweep.sets_for_tier(buffsweep.parse_tier_sets(Path(args.simc_source)), tier)
    if not sets:
        logging.warning("simc ships no set bonuses labelled %s; only Power Infusion will run", tier)

    found = profiles.discover(profiles_dir, tier, dps_only=True)
    selected = _select(found, args.wow_class, args.spec, args.limit, _parse_shard(args.shard))
    results: list[buffsweep.BuffResult] = []
    for index, profile in enumerate(selected, start=1):
        tier_set = buffsweep.class_id_of(profile, sets)
        logging.info(
            "[%d/%d] %s (%s)",
            index,
            len(selected),
            profile.display_name,
            tier_set.name if tier_set else "no tier set",
        )
        result = buffsweep.sweep_spec(
            simc, profile, tier_set, settings, targets=args.targets, timeout=args.timeout
        )
        for message in result.errors or ():
            logging.warning("  %s", message)
        results.append(result)
        # Written per spec, like the gear sweep: an interrupted run leaves a smaller
        # dataset rather than none.
        buffsweep.write_buffs(Path(args.out) / tier, tier, results, settings)

    path = Path(args.out) / tier / "buffs.json"
    print(f"wrote {path}: {len(results)} spec(s)")
    return 0


def cmd_unvalidated(args: argparse.Namespace) -> int:
    """Write out the profiles simc has written and left commented out.

    Not the same claim as a shipped profile, and the command says so on every run.
    A shipped profile is simc's authors saying "this is the spec this season"; one
    of these is the character they had written down when they stopped. The results
    have to carry that difference wherever they are shown.
    """
    from . import unvalidated

    simc_dir = Path(args.simc_source)
    tier = _resolve_tier(simc_dir / "profiles", args.tier)
    found = unvalidated.extract_tier(simc_dir, tier)
    shipped = {path.name for path in (simc_dir / "profiles" / tier).glob("*.simc")}

    print(f"{len(found)} disabled profile(s) in {tier}'s generators:")
    for profile in found:
        mark = "shipped" if profile.filename in shipped else "DISABLED"
        print(f"  [{mark}] {profile.name} ({profile.spec_line})")

    if not args.write:
        print("\nnothing written -- pass --out and --write to materialise them")
        return 0

    written = unvalidated.write_profiles(found, Path(args.out), shipped)
    print(f"\nwrote {len(written)} profile(s) into {args.out}")
    print(
        "These are UNVALIDATED: simc's authors disabled them for this tier, so any "
        "number from them is weaker evidence than one from a shipped profile and "
        "must be labelled that way."
    )
    return 0


def cmd_fights(args: argparse.Namespace) -> int:
    """Publish ``<tier>/fights.json``: what each boss is asserted and measured to be.

    Offline, and deliberately usable with no probe run at all. Without ``--probe``
    the file carries the assertions, the simc scenario each one produces, and a null
    measurement for every encounter -- which is the true state of a checkout that
    has never reached Warcraft Logs, and the state that makes the gaps visible on
    the site. With ``--probe`` pointed at a ``fight-probe-<tier>.json`` downloaded
    from CI, the same command fills in the measured half without a single request.
    """
    tier_profiles = fightprofile.load_profiles(
        args.tier, Path(args.profiles_file) if args.profiles_file else None
    )
    probe = fightdataset.load_probe(Path(args.probe)) if args.probe else None
    if probe and probe.get("tier") and probe["tier"] != args.tier:
        logging.warning(
            "the probe payload is for tier %s, publishing it under %s",
            probe["tier"],
            args.tier,
        )

    document = fightdataset.build_document(args.tier, tier_profiles, probe)
    try:
        path = fightdataset.write_fights(
            Path(args.out) / args.tier, document, force=getattr(args, "force", False)
        )
    except fightdataset.MeasurementWouldBeLost as exc:
        logging.error("%s", exc)
        return 1
    coverage = document["coverage"]
    logging.info(
        "wrote %s (%d encounters, %d with asserted facts, %d measured from logs)",
        path,
        coverage["encounters"],
        coverage["asserted"],
        coverage["measured"],
    )
    return 0


def cmd_fight_probe(args: argparse.Namespace) -> int:
    from . import fightprobe

    return fightprobe.cmd_fight_probe(args)


def cmd_talents(args: argparse.Namespace) -> int:
    """Compare a spec's hero builds with the gear held still, ranked two ways."""
    from . import talentsweep

    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)

    found = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    # Filtered by *spec name* rather than by build id: this command compares the
    # builds of a spec, so naming one build would leave it nothing to compare
    # against. `--spec Arcane`, not `--spec mage_arcane_sunfury`.
    if args.wow_class:
        wanted = {c.lower().replace("_", " ").replace("-", " ") for c in args.wow_class}
        found = [p for p in found if p.wow_class.lower() in wanted]
    if args.spec:
        wanted_specs = {name.lower() for name in args.spec}
        found = [p for p in found if p.spec.lower() in wanted_specs]
    if not found:
        logging.error("no profiles matched the selection")
        return 1

    by_spec: dict[str, list] = {}
    for profile in found:
        by_spec.setdefault(profile.spec_id, []).append(profile)

    settings = SimSettings(
        target_error=args.target_error,
        max_iterations=args.max_iterations,
        threads=args.threads,
    )

    results = []
    for _spec_id, group in sorted(by_spec.items()):
        for targets in args.targets:
            result = talentsweep.sweep_spec(
                simc, group, settings, targets=targets, timeout=args.timeout
            )
            if result is None:
                continue
            results.append(result)
            by_dps = result.ranked_by("dps")
            by_priority = result.ranked_by("prioritydps")
            print(f"\n{result.spec_label} at {targets} target(s), on {result.base_profile_id}")
            for build in by_dps:
                priority = (
                    f"  priority {build.priority_dps:>10,.0f}"
                    if build.priority_dps is not None
                    else ""
                )
                print(
                    f"  {build.hero_talent:<28} {build.dps:>11,.0f}"
                    f"  +/-{build.dps_error:.2f}%{priority}"
                )
            if by_priority and by_dps and by_priority[0].key != by_dps[0].key:
                print(
                    f"  -> {by_dps[0].hero_talent} does the most damage, but "
                    f"{by_priority[0].hero_talent} puts the most on the priority target"
                )

    if args.out and results:
        # Written into the tier layout the site already reads, beside gear.json and
        # fights.json. Optional in exactly the same way: a tier without one is a
        # tier nobody has run this for, and the view says so.
        out_dir = Path(args.out) / tier
        out_dir.mkdir(parents=True, exist_ok=True)
        talentsweep.write_talents(out_dir, tier, results, settings)
        logging.info(
            "wrote %s (%d spec/target combination(s))", out_dir / "talents.json", len(results)
        )
    return 0 if results else 1


def cmd_hero_trees(args: argparse.Namespace) -> int:
    """Detect and record the hero tree of every build simc ships unnamed.

    Every spec plays a hero tree; simc just omits it from the name of a spec's
    default build. This runs each such profile for one iteration, reads which
    hero-tree-gated abilities fired, and writes the resolved names to the
    checked-in data file `profiles.discover` reads. Detected from simc rather than
    hand-typed, so a new tier needs a re-run and not an edit.
    """
    from . import herotrees

    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)

    found = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    # A build simc already named needs nothing; only the unnamed ones are resolved.
    unnamed = [p for p in found if p.hero_talent is None]
    if not unnamed:
        logging.info("%s: every build already names its hero tree", tier)
        return 0

    resolved: dict[str, str] = dict(herotrees.load_overrides(tier))
    unresolved: list[str] = []
    for profile in unnamed:
        text = profile.path.read_text(encoding="utf-8", errors="replace")
        import re as _re

        name_match = _re.search(r'="(MID\d+[^"]*)"', text) or _re.search(r'="([^"]+)"', text)
        internal = name_match.group(1) if name_match else profile.path.stem
        report = simc_runner.run(
            simc,
            simc_runner.SimRequest(profile=profile, scenario=scenarios.PATCHWERK, targets=1),
            SimSettings(target_error=0, max_iterations=1),
            timeout=args.timeout,
        )
        tree = herotrees.detect_hero_tree(report, profile.wow_class, profile.spec)
        if tree is None:
            unresolved.append(f"{profile.wow_class} {profile.spec} ({internal})")
            logging.warning(
                "could not resolve the hero tree for %s %s -- add a signature to "
                "herotrees.HERO_TREE_SIGNATURES",
                profile.wow_class,
                profile.spec,
            )
            continue
        resolved[internal] = tree
        print(f"  {profile.wow_class} {profile.spec:<14} {internal:<34} -> {tree}")

    if resolved:
        path = herotrees.write_overrides(tier, resolved)
        logging.info("wrote %s (%d resolved)", path, len(resolved))
    return 1 if unresolved else 0


def cmd_check_profiles(args: argparse.Namespace) -> int:
    """Which of a tier's profiles still build an actor against current spell data.

    Exists because "old-tier profiles rot" is a fact this project acts on -- the
    previous tier is deliberately kept off the schedule because of it -- and a fact
    that decides a schedule should be re-measurable in one command rather than
    rediscovered as a loop somebody writes from the README.
    """
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)

    found = profiles.discover(profiles_dir, tier, dps_only=not args.include_tanks)
    if not found:
        logging.error("no profiles found for %s under %s", tier, profiles_dir)
        return 1

    healthy: list[profiles.ProfileHealth] = []
    broken: list[profiles.ProfileHealth] = []
    for index, profile in enumerate(found, start=1):
        logging.debug("[%d/%d] %s", index, len(found), profile.display_name)
        health = profiles.check_loads(simc, profile, timeout=args.timeout)
        (healthy if health.loads else broken).append(health)

    rotten = [entry for entry in broken if entry.rotten_talents]
    for entry in broken:
        print(f"{'TALENTS' if entry.rotten_talents else 'BROKEN '}  {entry.profile.id}")
        print(f"          {entry.reason}")
    print(
        f"\n{tier}: {len(healthy)} of {len(found)} profiles load"
        + (f"; {len(rotten)} fail on a talent hash the spec no longer offers" if rotten else "")
    )
    if broken:
        print(
            "A profile that does not load produces no actor, so a run over this tier "
            "publishes a dataset with those builds missing -- and the breakage "
            "correlates with the talent changes a season comparison is supposed to "
            "surface. Weigh that before scheduling this tier."
        )
    # Reporting is the point, so a broken profile is not a failed command unless
    # somebody is using this as a gate.
    return 1 if broken and args.strict else 0


def cmd_loot_sources(args: argparse.Namespace) -> int:
    from . import lootsources

    return lootsources.cmd_loot_sources(args)


def cmd_gear_pool(args: argparse.Namespace) -> int:
    from . import gearpool

    return gearpool.cmd_gear_pool(args)


def cmd_simc_changes(args: argparse.Namespace) -> int:
    """Record when simc last changed, and what moved, into the tier's manifest.

    Separate from `build` because it needs simc's *git history*, which the sim jobs
    do not have -- they get a compiled binary and a profiles directory, no `.git`.
    A metadata-only clone is seconds, so this runs beside the merge instead.
    """
    from . import simcchanges

    root = Path(args.data)
    tier = args.tier
    if not tier or tier == "latest":
        index = root / "tiers.json"
        if not index.is_file():
            logging.error("no tier index at %s", index)
            return 1
        tier = json.loads(index.read_text(encoding="utf-8"))["current"]

    path = root / tier / "index.json"
    if not path.is_file():
        logging.error("no manifest at %s", path)
        return 1
    manifest = json.loads(path.read_text(encoding="utf-8"))

    simc = manifest.get("simc") or {}
    # What this run built, against what the *published* manifest was built from. The
    # published one is read before it is overwritten, so ordering matters: this must
    # run after `merge` has written the new manifest but the revision it compares to
    # comes from --since, which the workflow reads beforehand.
    block = simcchanges.describe(Path(args.simc_source), args.since or None)

    if simc.get("changes") == block:
        logging.info("%s already carries this simc change summary; nothing written", path)
        return 0

    simc["changes"] = block
    manifest["simc"] = simc
    path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    logging.info(
        "wrote %s: simc revision dated %s, %s commit(s) since %s",
        path,
        block.get("revisionDate"),
        block.get("commits", "an unknown number of"),
        args.since or "(nothing published)",
    )
    return 0


def cmd_talent_trees(args: argparse.Namespace) -> int:
    """Decode every build's loadout string and publish the tree it describes.

    Offline and credential-free by design: the layout and the format both come out of
    the simc checkout, so this runs anywhere the sims run. See talenttree.py for how
    the decode was verified without a single API call.
    """
    from . import talenttree

    root = Path(args.data)
    tier = args.tier
    if not tier or tier == "latest":
        index = root / "tiers.json"
        if not index.is_file():
            logging.error("no tier index at %s -- run `wowdps build` first", index)
            return 1
        tier = json.loads(index.read_text(encoding="utf-8"))["current"]

    spec_dir = root / tier / "specs"
    if not spec_dir.is_dir():
        logging.error("no spec files at %s", spec_dir)
        return 1
    builds = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(spec_dir.glob("*.json"))
    ]

    # simc ships two trait tables and the profiles were run against one of them. The
    # manifest already records which, so the default follows it rather than asking --
    # reading the live table for a PTR tier is the kind of mismatch that decodes
    # cleanly and quietly describes the wrong tree.
    ptr = args.ptr
    manifest_path = root / tier / "index.json"
    if not ptr and manifest_path.is_file():
        recorded = (json.loads(manifest_path.read_text(encoding="utf-8")).get("simc") or {}).get(
            "ptr"
        )
        if recorded:
            logging.info("%s was simulated against simc's PTR data; reading that table", tier)
            ptr = True

    traits = talenttree.parse_trait_data(Path(args.simc_source), ptr=ptr)
    if not traits:
        logging.error("no trait data found under %s", args.simc_source)
        return 1

    document = talenttree.build_document(tier, builds, traits)
    path = talenttree.write_talent_trees(document, root / tier)
    logging.info(
        "wrote %s: %d build(s) over %d tree(s)",
        path,
        len(document["builds"]),
        len(document["trees"]),
    )
    for note in document["notes"]:
        logging.warning("%s", note)
    return 0


def cmd_fight_promote(args: argparse.Namespace) -> int:
    from . import fightpromote

    return fightpromote.cmd_fight_promote(args)


def cmd_logs_analyse(args: argparse.Namespace) -> int:
    from . import logsanalysis

    return logsanalysis.cmd_logs_analyse(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wowdps", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--profiles",
            default=".work/simc/profiles",
            help="path to the simc profiles directory",
        )
        p.add_argument(
            "--tier",
            default="latest",
            help="tier directory such as MID2, or 'latest' (default)",
        )
        p.add_argument("--include-tanks", action="store_true", help="also sim tank specs")

    p_list = sub.add_parser("list", help="list the profiles that would be simmed")
    add_common(p_list)
    p_list.set_defaults(func=cmd_list)

    p_build = sub.add_parser("build", help="run sims and write the dataset")
    add_common(p_build)
    p_build.add_argument("--simc", help="path to the simc binary (default: $PATH)")
    p_build.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    p_build.add_argument(
        "--scenario",
        action="append",
        metavar="NAME",
        help=(
            "limit to one scenario (repeatable). One of "
            + ", ".join(sorted(scenarios.BY_ID))
            + "; or 'boss_<encounterId>' to run a boss's own fight profile, or "
            "'bosses' for every boss the tier's profiles know something about"
        ),
    )
    p_build.add_argument(
        "--profiles-file",
        help="alternative fight profile file, for the boss scenarios",
    )
    p_build.add_argument(
        "--wow-class",
        action="append",
        help="limit to one class, e.g. 'Mage' or 'Death Knight' (repeatable)",
    )
    p_build.add_argument("--spec", action="append", help="limit to one spec id (repeatable)")
    p_build.add_argument("--limit", type=int, help="only the first N profiles")
    p_build.add_argument(
        "--shard",
        help="run only part of the matrix, as INDEX/COUNT (zero-based), e.g. 0/6",
    )
    p_build.add_argument(
        "--target-error",
        type=float,
        default=0.0,
        help="converge until the DPS standard error is below this percent; "
        "0 (default) runs a fixed, deterministic iteration count instead",
    )
    p_build.add_argument(
        "--max-iterations",
        type=int,
        default=3000,
        help="fixed iteration count in deterministic mode, ceiling in adaptive mode",
    )
    p_build.add_argument("--threads", type=int, default=0)
    p_build.add_argument("--timeout", type=int, default=1800)
    p_build.set_defaults(func=cmd_build)

    p_gear = sub.add_parser(
        "gear", help="compare drops for an equipment slot against what is already worn"
    )
    add_common(p_gear)
    p_gear.add_argument("--simc", help="path to the simc binary (default: $PATH)")
    p_gear.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    p_gear.add_argument(
        "--pools", help="alternative gear pool file (default: the one shipped in the package)"
    )
    p_gear.add_argument(
        "--slot",
        action="append",
        choices=sorted(equipment.SLOTS_BY_ID),
        help="limit to one equipment slot (repeatable); default is every slot with a pool",
    )
    p_gear.add_argument(
        "--targets",
        type=int,
        action="append",
        help=f"target count to sweep at (repeatable); default {DEFAULT_GEAR_TARGETS[0]}",
    )
    p_gear.add_argument("--wow-class", action="append", help="limit to one class (repeatable)")
    p_gear.add_argument("--spec", action="append", help="limit to one spec id (repeatable)")
    p_gear.add_argument("--limit", type=int, help="only the first N profiles")
    p_gear.add_argument("--shard", help="run only part of the matrix, as INDEX/COUNT")
    p_gear.add_argument("--target-error", type=float, default=0.0)
    p_gear.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_GEAR_ITERATIONS,
        help=f"fixed iteration count per variant (default {DEFAULT_GEAR_ITERATIONS}); "
        f"raise it for an official run, lower it for an exploratory one",
    )
    p_gear.add_argument("--threads", type=int, default=0)
    p_gear.add_argument("--timeout", type=int, default=3600)
    p_gear.set_defaults(func=cmd_gear)

    p_candidates = sub.add_parser(
        "gear-candidates",
        help="list the items simc knows for a slot, as raw material for a pool file",
    )
    p_candidates.add_argument(
        "--simc-source", default=".work/simc", help="path to the simc source checkout"
    )
    p_candidates.add_argument("--slot", default="trinket", choices=sorted(equipment.SLOTS_BY_ID))
    p_candidates.add_argument(
        "--min-ilevel", type=int, default=0, help="hide items below this base item level"
    )
    p_candidates.add_argument(
        "--effects-only", action="store_true", help="only items with an on-use or equip effect"
    )
    p_candidates.set_defaults(func=cmd_gear_candidates)

    p_merge = sub.add_parser("merge", help="merge sharded dataset directories")
    p_merge.add_argument("shards", nargs="+", help="shard directories to merge")
    p_merge.add_argument("--out", default=str(DEFAULT_OUT))
    p_merge.set_defaults(func=cmd_merge)

    p_verify = sub.add_parser(
        "verify", help="cross-check sim output against Warcraft Logs rankings"
    )
    p_verify.add_argument("--data", default=str(DEFAULT_OUT), help="dataset directory")
    p_verify.add_argument(
        "--tier",
        default="latest",
        help="which tier of the dataset to verify, or 'latest' (default)",
    )
    p_verify.add_argument(
        "--encounter", type=int, action="append", help="encounter id (repeatable)"
    )
    p_verify.add_argument("--metric", default="dps", help="Warcraft Logs ranking metric")
    p_verify.add_argument("--difficulty", type=int, default=5, help="5 = Mythic, 4 = Heroic")
    p_verify.set_defaults(func=cmd_verify)

    p_fight_profiles = sub.add_parser(
        "fight-profiles",
        help="print each boss's fight profile and the simc scenario it produces",
    )
    p_fight_profiles.add_argument("--tier", default="MID2")
    p_fight_profiles.add_argument("--profiles-file", help="alternative fight profile file")
    p_fight_profiles.set_defaults(func=cmd_fight_profiles)

    p_fights = sub.add_parser(
        "fights",
        help="publish <tier>/fights.json from the fight profiles and an optional probe run",
    )
    p_fights.add_argument("--tier", default="MID2")
    p_fights.add_argument("--out", default=str(DEFAULT_OUT), help="dataset output directory")
    p_fights.add_argument(
        "--probe",
        help="a fight-probe-<tier>.json to fill in the measured half; omit to publish "
        "the assertions alone",
    )
    p_fights.add_argument("--profiles-file", help="alternative fight profile file")
    p_fights.add_argument(
        "--force",
        action="store_true",
        help="write even when it would discard measurements the published file "
        "already carries. Without --probe that is what this command does, and it "
        "reports success while doing it",
    )
    p_fights.set_defaults(func=cmd_fights)

    p_buffs = sub.add_parser(
        "buffs",
        help="what the tier set and an outside Power Infusion are worth, per spec",
    )
    p_buffs.add_argument("--tier", default="latest")
    p_buffs.add_argument("--simc", help="path to the simc binary")
    p_buffs.add_argument("--profiles", default="simc/profiles")
    p_buffs.add_argument(
        "--simc-source", default="simc", help="a simc checkout, for engine/dbc/generated"
    )
    p_buffs.add_argument("--out", default=str(DEFAULT_OUT))
    p_buffs.add_argument("--targets", type=int, default=1)
    p_buffs.add_argument("--target-error", type=float, default=0.0)
    p_buffs.add_argument("--max-iterations", type=int, default=3000)
    p_buffs.add_argument("--threads", type=int, default=0)
    p_buffs.add_argument("--timeout", type=int, default=1800)
    p_buffs.add_argument("--class", dest="wow_class", action="append")
    p_buffs.add_argument("--spec", action="append")
    p_buffs.add_argument("--limit", type=int)
    p_buffs.add_argument("--shard")
    p_buffs.set_defaults(func=cmd_buffs)

    p_unvalidated = sub.add_parser(
        "unvalidated",
        help="list or write out the profiles simc wrote and left commented out",
    )
    p_unvalidated.add_argument("--tier", default="latest")
    p_unvalidated.add_argument("--simc-source", default="simc")
    p_unvalidated.add_argument("--out", help="directory to write the profiles into")
    p_unvalidated.add_argument("--write", action="store_true")
    p_unvalidated.set_defaults(func=cmd_unvalidated)

    p_spec_index = sub.add_parser(
        "spec-index",
        help="publish <tier>/spec-index.json: every class and spec in the game, for "
        "the Spec detail picker",
    )
    p_spec_index.add_argument("--tier", default="latest")
    p_spec_index.add_argument(
        "--simc-source", default="simc", help="a simc checkout (profiles + dbc/generated)"
    )
    p_spec_index.add_argument("--out", default="web/public/data")
    p_spec_index.set_defaults(func=cmd_spec_index)

    p_fight_zones = sub.add_parser(
        "fight-zones",
        help="list Warcraft Logs' raid zones and say which season each boss list "
        "belongs to (needs credentials)",
    )
    p_fight_zones.add_argument("--tier", default="MID2", help="tier to seed with --seed")
    p_fight_zones.add_argument("--profiles-file", help="alternative fight profile file")
    p_fight_zones.add_argument(
        "--show", type=int, default=8, help="how many of the newest zones to print"
    )
    p_fight_zones.add_argument(
        "--verbose-zones", action="store_true", help="print every encounter of every zone shown"
    )
    p_fight_zones.add_argument(
        "--seed", type=int, metavar="ZONE_ID", help="add that zone's encounters to --tier"
    )
    p_fight_zones.add_argument(
        "--move",
        nargs=2,
        metavar=("FROM", "TO"),
        help="re-file a tier's encounters, with their facts, under another tier name",
    )
    p_fight_zones.add_argument(
        "--scan",
        nargs=2,
        type=int,
        metavar=("FROM", "TO"),
        help="walk this range of zone ids through the by-id lookup and print every "
        "zone the list does not return. There is no endpoint that enumerates all "
        "zones, and at least the PTR ones are missing from `worldData.zones`",
    )
    p_fight_zones.add_argument("--difficulty", type=int, default=5)
    p_fight_zones.add_argument("--write", action="store_true", help="apply --seed/--move to disk")
    p_fight_zones.set_defaults(func=cmd_fight_zones)

    p_wcl_schema = sub.add_parser(
        "wcl-schema",
        help="introspect the Warcraft Logs schema: which fields and orderings exist "
        "(needs credentials)",
    )
    p_wcl_schema.add_argument(
        "--type",
        action="append",
        help="type to introspect; repeatable. Defaults to the ones bearing on how "
        "kills can be ordered",
    )
    p_wcl_schema.set_defaults(func=cmd_wcl_schema)

    p_fight_probe = sub.add_parser(
        "fight-probe",
        help="measure a boss's fight structure from Warcraft Logs (needs credentials)",
    )
    from . import fightprobe

    fightprobe.add_arguments(p_fight_probe)
    p_fight_probe.set_defaults(func=cmd_fight_probe)

    p_talents = sub.add_parser(
        "talents",
        help="compare a spec's hero builds with the gear held still, ranked by "
        "damage and by damage to the priority target",
    )
    add_common(p_talents)
    p_talents.add_argument("--simc", help="path to the simc binary (default: $PATH)")
    p_talents.add_argument(
        "--wow-class", action="append", help="limit to one class, e.g. 'Mage' (repeatable)"
    )
    p_talents.add_argument(
        "--spec",
        action="append",
        help="limit to one specialisation by name, e.g. 'Arcane' (repeatable). Not a "
        "build id: this command compares the builds of a spec against each other",
    )
    p_talents.add_argument(
        "--targets",
        type=int,
        nargs="+",
        default=[1],
        help="target counts to compare the builds at (default: 1)",
    )
    p_talents.add_argument("--target-error", type=float, default=0.0)
    p_talents.add_argument("--max-iterations", type=int, default=3000)
    p_talents.add_argument("--threads", type=int, default=0)
    p_talents.add_argument("--timeout", type=int, default=1800)
    p_talents.add_argument(
        "--out",
        nargs="?",
        const=str(DEFAULT_OUT),
        help="also publish the results to <out>/<tier>/talents.json "
        f"(default when given with no value: {DEFAULT_OUT})",
    )
    p_talents.set_defaults(func=cmd_talents)

    p_hero = sub.add_parser(
        "hero-trees",
        help="detect the hero tree of every build simc ships without one in its "
        "name, and record it for the dataset",
    )
    add_common(p_hero)
    p_hero.add_argument("--simc", help="path to the simc binary (default: $PATH)")
    p_hero.add_argument("--timeout", type=int, default=120, help="seconds per profile")
    p_hero.set_defaults(func=cmd_hero_trees)

    p_check = sub.add_parser(
        "check-profiles",
        help="which of a tier's simc profiles still build an actor against current "
        "spell data (old tiers rot as talents change)",
    )
    add_common(p_check)
    p_check.add_argument("--simc", help="path to the simc binary (default: $PATH)")
    p_check.add_argument("--timeout", type=int, default=120, help="seconds per profile")
    p_check.add_argument(
        "--strict", action="store_true", help="exit non-zero when any profile fails"
    )
    p_check.set_defaults(func=cmd_check_profiles)

    p_loot_sources = sub.add_parser(
        "loot-sources",
        help="derive item drop sources and this season's dungeons from Blizzard's "
        "Game Data API (needs credentials)",
    )
    from . import lootsources

    lootsources.add_arguments(p_loot_sources)
    p_loot_sources.set_defaults(func=cmd_loot_sources)

    p_gear_pool = sub.add_parser(
        "gear-pool",
        help="rebuild a slot's item pool from Blizzard's journal joined against simc's "
        "item table, so pool membership is this season's rather than inferred "
        "(needs credentials)",
    )
    from . import gearpool

    gearpool.add_arguments(p_gear_pool)
    p_gear_pool.set_defaults(func=cmd_gear_pool)

    p_talent_trees = sub.add_parser(
        "talent-trees",
        help="decode each build's loadout string into the talent tree it describes, "
        "from simc's own trait table (no credentials, no external service)",
    )
    p_talent_trees.add_argument("--data", default="web/public/data", help="dataset directory")
    p_talent_trees.add_argument("--tier", default="latest")
    p_talent_trees.add_argument(
        "--simc-source", required=True, help="simc source checkout, for the trait table"
    )
    p_talent_trees.add_argument(
        "--ptr",
        action="store_true",
        help="force the PTR trait table. The default follows the tier's own manifest, "
        "which records which data set the sims ran against.",
    )
    p_talent_trees.set_defaults(func=cmd_talent_trees)

    p_simc_changes = sub.add_parser(
        "simc-changes",
        help="record when simc last changed and what moved, from its git history",
    )
    p_simc_changes.add_argument("--data", default="web/public/data")
    p_simc_changes.add_argument("--tier", default="latest")
    p_simc_changes.add_argument(
        "--simc-source", required=True, help="a simc checkout with history (not --depth 1)"
    )
    p_simc_changes.add_argument(
        "--since", help="the previously published simc revision to compare against"
    )
    p_simc_changes.set_defaults(func=cmd_simc_changes)

    p_fight_promote = sub.add_parser(
        "fight-promote",
        help="offer a probe run's measurements as fight profile facts; prints the "
        "plan unless --write is given, and never overwrites a hand-asserted fact",
    )
    from . import fightpromote

    fightpromote.add_arguments(p_fight_promote)
    p_fight_promote.set_defaults(func=cmd_fight_promote)

    p_logs_analyse = sub.add_parser(
        "logs-analyse",
        help="recompute the readings in a published logs-verification.json; needs no "
        "credentials and spends no Warcraft Logs points",
    )
    from . import logsanalysis

    logsanalysis.add_arguments(p_logs_analyse)
    p_logs_analyse.set_defaults(func=cmd_logs_analyse)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
