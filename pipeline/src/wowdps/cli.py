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


def _tier_set_reference(
    profiles_dir: Path,
    tier: str,
    ptr: bool = False,
) -> dataset.TierSetReference | None:
    """Which tier-set state the tier's shipped profiles wear, or ``None`` and why.

    **It discovers the tier itself rather than taking a profile list**, which is what
    makes it shard-safe in the way ``profiles.spec_coverage`` already is: there is no
    parameter a shard could hand its own slice through, so all twelve shards compute
    one reference and ``merge_shards`` keeping the newest manifest keeps a correct
    one. Passing ``cmd_build``'s ``all_profiles`` would be right today and would be
    one refactor away from passing ``selected``, and the resulting per-shard majority
    would be a full set of plausible flags on the wrong builds.

    Tanks are discovered too, for the same reason and not because their damage
    matters: they are shipped profiles that wear the set, and reading them makes the
    reference independent of ``--include-tanks`` as well as of the shard. Measured on
    simc 22b442e, MID2: with tanks the tally is 33 profiles at the four-piece against
    2 at none, without them 26 against 2 -- the same verdict from a wider base.

    Two of simc's generated tables answer the question and both live beside the
    profiles directory, so the simc checkout is derived from it rather than asked for
    again -- ``--profiles`` defaults to ``.work/simc/profiles`` and every workflow
    lays it out that way. A checkout without them is not a reason to lose a night of
    simulations, so it warns and returns ``None``; nothing is then flagged, which is
    the state the dataset was in before this existed.

    **Which of simc's two item tables is stated, never discovered, and the version
    this replaces described a mechanism it did not implement.** It read the ``ptr``
    flag out of ``<out_root>/<tier>/index.json`` and called that "the same question
    ``talent-trees`` answers, answered the same way" -- but ``talent-trees`` takes an
    explicit ``--ptr`` and falls back to a manifest under ``--out``, which for that
    command *is* the published dataset, while ``cmd_build``'s ``--out`` is where this
    run writes. The nightly passes ``--out shard``, a fresh empty directory, so the
    file never existed, ``ptr`` silently defaulted to ``False``, and nothing said so.
    ``--ptr`` is the whole of the interface now, matching ``gear-anchor``, which reads
    these same two tables.

    **And the manifest could not have answered it anyway.** ``manifest.simc.ptr`` is
    ``report["ptr_enabled"]``, which is ``SC_USE_PTR`` -- a compile-time constant,
    defined as 1 in ``engine/config.hpp`` on simc's midnight branch, so it says
    the binary *carries* PTR data and not that anything used it. What the run used is
    ``dbc.version_used``, and measured on simc 625a591 on 2026-08-23 against the exact
    argv ``simc_runner.build_command`` produces, it is **Live**: this pipeline never
    passes ``ptr=1``, and the option has to precede the profile to take effect at all,
    because simc copies the sim's dbc into the player while parsing the profile. Hence
    the default here is the live table, which is what the sims read.
    ``test_build_command_never_enables_ptr_data`` is what stops that going stale.

    Live and PTR agree today in both tables -- 12,260 items carry an ``id_set`` in
    each and the two maps are equal, as are all 376 set rows, measured on 22b442e and
    again on 625a591 -- which is exactly the kind of agreement that stops being true
    without announcing itself. That is why the choice is stated rather than left to a
    file that happens not to be there.
    """
    from . import buffsweep, gearanchor

    simc_dir = profiles_dir.parent
    try:
        sets = buffsweep.parse_tier_sets(simc_dir, ptr=ptr)
        item_sets = gearanchor.parse_item_sets(simc_dir, ptr=ptr)
        reference = dataset.shipped_set_states(
            profiles.discover(profiles_dir, tier, dps_only=False), tier, sets, item_sets
        )
    except (OSError, gearanchor.AnchorError) as exc:
        logging.warning(
            "cannot read simc's set tables under %s (%s); no build will be checked "
            "against the tier's tier-set state",
            simc_dir,
            exc,
        )
        return None

    written = ", ".join(
        f"{count} at {'no set' if state == 0 else str(state) + 'pc'}"
        for state, count in reference.tally
    )
    if reference.state is None:
        logging.warning(
            "tier %s has no majority tier-set state among its shipped profiles (%s); "
            "no build will be flagged, because there is nothing to differ from",
            tier,
            written or "no profile votes",
        )
        return reference
    logging.info("tier %s ships tier-set states: %s", tier, written)
    return reference


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

    # From every profile the tier ships, not from this shard's slice, so all twelve
    # shards anchor on the same number. A build whose gear sits away from it gets a
    # caveat rather than a silent place in the ranking -- absolute DPS does not
    # survive an item-level difference, and simc's disabled profiles are routinely a
    # whole tier behind its shipped ones.
    reference_item_level = dataset.shipped_item_levels(all_profiles)
    if reference_item_level:
        logging.info("tier %s ships item levels %s", tier, reference_item_level)

    # The other systematic gear difference a tier can hold, derived the same way and
    # from the same list. Read once here rather than per profile: the item table is
    # 26 MB and parsing it is the whole cost of the answer.
    reference_set = _tier_set_reference(profiles_dir, tier, ptr=args.ptr)

    results: list[dataset.SpecResult] = []
    simc_meta: dict = {}

    for index, profile in enumerate(selected, start=1):
        logging.info("[%d/%d] %s", index, len(selected), profile.display_name)
        result = dataset.run_spec(
            simc,
            profile,
            selected_scenarios,
            settings,
            timeout=args.timeout,
            reference_item_level=reference_item_level,
            reference_set=reference_set,
        )
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

    def publish() -> Path:
        """Write everything swept so far, and carry its own coverage count.

        Called after every spec rather than once at the end, which CLAUDE.md has
        claimed for a while and the code did not do. A sweep that is interrupted at
        spec 9 of 26 then leaves a dataset that is smaller *and* honest about being
        smaller, instead of leaving nothing at all -- and this matters more now than
        it did, because enumerating the whole pool roughly doubles what a ring spec
        costs and so doubles what a timeout throws away.
        """
        return dataset.write_gear(
            out_dir,
            results,
            {slot: pools.slots[slot] for slot in wanted_slots},
            tier,
            simc_meta,
            settings,
            specs_available=len(all_profiles),
        )

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
        if results:
            publish()

    if not results:
        logging.error("every spec failed; not writing a gear dataset")
        return 1

    # The loop's last iteration already published exactly this, so serialising a
    # several-hundred-kilobyte document again would only restamp `generatedAt`.
    path = out_dir / "gear.json"
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
    build_sub_trees: dict[str, int] = {}
    if talents_path.is_file():
        talents = json.loads(talents_path.read_text(encoding="utf-8"))
        tree_names = specindex.tree_names_from_talents(talents)
        build_sub_trees = specindex.builds_by_sub_tree(talents)
    else:
        logging.warning(
            "no %s, so no build can be placed in a hero tree and coverage stays at "
            "the spec level -- run `wowdps talent-trees` first. That fallback is in "
            "`hero_tree_coverage`, which publishes nothing rather than reporting "
            "every shipped spec as uncovered",
            talents_path,
        )

    # Which generated trait table the sims read -- never `simc.ptr` directly, which
    # was `SC_USE_PTR` until issue #42 and is a compile constant in every manifest
    # published before it. See `simc_runner.manifest_used_ptr_data`.
    ptr = simc_runner.manifest_used_ptr_data(manifest)
    # Which of this tier's profiles simc will refuse, decided offline: the reason and
    # the node id come out of the trait table without a binary. See
    # `specindex.refused_profiles`; `wowdps check-profiles` is the version that asks
    # simc itself.
    refused = specindex.refused_profiles(simc_dir, tier, ptr=ptr)
    document = specindex.build_index(
        simc_dir,
        tier,
        manifest,
        tree_names,
        ptr=ptr,
        build_sub_trees=build_sub_trees,
        refused=refused,
    )
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
    coverage = document.get("heroTreeCoverage")
    if not coverage:
        # Never silent: "no hero tree coverage" and "complete hero tree coverage" are
        # the same empty block on the site if nobody says which happened.
        print(
            "  hero tree coverage: not published -- no build could be placed in a "
            "tree, so the panel falls back to spec-level coverage"
        )
    if coverage:
        print(
            f"  hero tree coverage: {coverage['covered']} of {coverage['cells']} "
            f"damage spec x hero tree pairs have a build"
        )
        for entry in refused:
            print(f"  ! {entry['profile']:<44} will not load: {entry['reason']}")
        for entry in coverage["unplaced"]:
            logging.warning(
                "%s plays %s (sub-tree %d), which simc's trait table places on no "
                "spec -- the pair is not counted",
                entry["build"],
                entry["tree"],
                entry["subTree"],
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

    all_sets = buffsweep.parse_tier_sets(Path(args.simc_source))
    sets = buffsweep.sets_for_tier(all_sets, tier)
    if not sets:
        logging.warning("simc ships no set bonuses labelled %s; only Power Infusion will run", tier)

    # The season boundary needs the tier before this one. Resolved against the tiers
    # simc ships rather than by decrementing the name, so it keeps meaning "last
    # season" after the next one lands.
    previous_sets: list[buffsweep.TierSet] = []
    try:
        previous_tier = profiles.previous_tier(profiles_dir)
    except Exception as exc:  # noqa: BLE001 - a first tier has no predecessor
        logging.info("no previous tier to compare sets against: %s", exc)
    else:
        previous_sets = buffsweep.sets_for_tier(all_sets, previous_tier)
        if previous_sets:
            logging.info("comparing against %s's sets for the season boundary", previous_tier)
        else:
            logging.warning("simc ships no set bonuses labelled %s", previous_tier)

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
            simc,
            profile,
            tier_set,
            settings,
            targets=args.targets,
            timeout=args.timeout,
            previous_set=buffsweep.class_id_of(profile, previous_sets),
        )
        for message in result.errors or ():
            logging.warning("  %s", message)
        results.append(result)
        # Written per spec, like the gear sweep: an interrupted run leaves a smaller
        # dataset rather than none.
        # `found`, not `selected`: the denominator is the tier's size, and every
        # shard must state the same one or the merge cannot recount against it.
        buffsweep.write_buffs(
            Path(args.out) / tier, tier, results, settings, builds_available=len(found)
        )

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
        print("\nnothing written -- pass --write to materialise them")
        return 0

    # Default destination is the tier's own profile directory, which is what makes
    # `wowdps build` pick them up with no flag of its own: the state travels in the
    # file (`unvalidated.MARKER`), so a sharded run materialises them identically in
    # every job and no shard can disagree with another about what it simulated.
    out_dir = Path(args.out) if args.out else simc_dir / "profiles" / tier
    written = unvalidated.write_profiles(found, out_dir, shipped)
    print(f"\nwrote {len(written)} profile(s) into {out_dir}")
    print(
        "These are UNVALIDATED: simc's authors disabled them for this tier, so any "
        "number from them is weaker evidence than one from a shipped profile and "
        "must be labelled that way."
    )
    return 0


def cmd_extra_builds(args: argparse.Namespace) -> int:
    """Materialise the builds this project supplies for missing (spec, tree) cells.

    Runs after ``wowdps unvalidated`` and before ``wowdps build``, in every shard
    and in the publish job, so the cells exist wherever profiles are discovered.
    A cell whose hash fails offline validation is refused and reported; the other
    cells are still written, and the exit code stays 0 unless ``--strict`` asks
    otherwise -- one rotted cell must not cost the night, the same rule as
    ``hero-trees``.
    """
    from . import extrabuilds

    simc_dir = Path(args.simc_source)
    tier = _resolve_tier(simc_dir / "profiles", args.tier)
    cells = extrabuilds.load_cells(tier)
    if not cells:
        print(f"no extra builds are recorded for {tier}; nothing to do")
        return 0

    print(f"{len(cells)} extra build(s) recorded for {tier}:")
    for cell in cells:
        print(f"  [{cell.origin}] {cell.profile} (talents on {cell.base}'s character)")

    if not args.write:
        print("\nnothing written -- pass --write to materialise them")
        return 0

    out_dir = Path(args.out) if args.out else simc_dir / "profiles" / tier
    report = extrabuilds.write_cells(simc_dir, tier, out_dir, cells)
    print(f"\nwrote {len(report.written)} profile(s) into {out_dir}")
    if report.unchecked:
        print(
            "WARNING: no trait table under the checkout, so the hashes were not "
            "validated offline; simc itself is the only gate left."
        )
    for profile, reason in report.skipped:
        print(f"  REFUSED {profile}: {reason}")
    if report.skipped and args.strict:
        return 1
    return 0


def cmd_gear_anchor(args: argparse.Namespace) -> int:
    """Show the normalized kit a computed build of this tier would wear.

    Offline and read-only. It exists because the anchor moves numbers -- measured on
    MID2 at 1000 deterministic iterations, one target, it costs a shipped profile
    3.65% to 6.17% and lifts a disabled one 45.91% to 65.65% -- and a reader who
    cannot see the anchor has to take it on trust.
    """
    from . import buffsweep, gearanchor

    simc_dir = Path(args.simc_source)
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    found = profiles.discover(profiles_dir, tier, dps_only=True)
    if not found:
        # Distinct from the filter miss below, which used to swallow this case and
        # report the filter as None -- naming something the user never supplied.
        logging.error(
            "no damage profile at all under %s/%s, so there is nothing to anchor",
            profiles_dir,
            tier,
        )
        return 1

    selected = [p for p in found if not args.profile or args.profile in p.path.stem]
    if not selected:
        logging.error(
            "none of %s's %d damage profiles matches %r",
            tier,
            len(found),
            args.profile,
        )
        return 1

    try:
        sets: list[buffsweep.TierSet] | None = buffsweep.parse_tier_sets(simc_dir, ptr=args.ptr)
        item_sets: dict[int, int] | None = None
        if sets is not None and not buffsweep.sets_for_tier(sets, tier):
            logging.warning("simc ships no set bonus labelled %s; anchoring gear alone", tier)
            sets = None
        if sets is not None:
            # After the check, not before it: the item table is 115,470 rows and a
            # tier with no set has no use for a single one of them.
            item_sets = gearanchor.parse_item_sets(simc_dir, ptr=args.ptr)

        # Only the *set name* and the zeroed tokens vary by class; the item level, the
        # tally and the state do not. Deriving per profile re-ran the whole tier tally
        # once per profile -- 40 tallies and ~1,120 profile reads on MID2 -- so it is
        # derived once per distinct class and reused.
        targets: dict[str, gearanchor.AnchorTarget] = {}
        for profile in selected:
            if profile.wow_class not in targets:
                targets[profile.wow_class] = gearanchor.derive_target(
                    found, tier, sets, item_sets, wow_class=profile.wow_class
                )
        anchors = [
            (
                profile,
                gearanchor.apply(targets[profile.wow_class], gearanchor.read_kit(profile.path)),
            )
            for profile in selected
        ]
    except gearanchor.AnchorError as exc:
        # Both of this module's refusals are worded for a human and name their own
        # fix. A traceback buries that sentence under six frames.
        logging.error("%s", exc)
        return 1

    target = targets[selected[0].wow_class]
    print(f"{tier}: item level {target.ilevel} ({target.ilevel_evidence})")
    print(f"      tier set {target.set_pieces}-piece ({target.set_evidence})")
    if target.zeroed_options:
        print(f"      {len(target.zeroed_options)} other set(s) written to zero")
    print()

    for profile, anchor in anchors:
        mark = " [unvalidated]" if profile.unvalidated else ""
        print(f"{profile.path.stem}{mark}")
        print(f"  {gearanchor.describe(anchor)}")
        if args.options:
            for option in anchor.options():
                print(f"    {option}")
    return 0


def buildsearch_final() -> int:
    from . import buildsearch

    return buildsearch.FINAL_ITERATIONS


def buildsearch_climb() -> int:
    from . import buildsearch

    return buildsearch.CLIMB_STEPS


def cmd_projection_check(args: argparse.Namespace) -> int:
    """Measure whether the published projection holds on simc's own gear (issue #52).

    Two head-to-heads per build, both profileset-against-profileset: the same pair of
    talent hashes on the gear anchor, and on simc's shipped kit. The first is the
    control -- it must reproduce the published margin, or the run is measuring
    something else and its second number means nothing.
    """
    from . import buildsearchrun, gearanchor, projectioncheck, talentrepair

    simc_dir = Path(args.simc_source)
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)

    document_path = Path(args.document or (Path(args.out) / tier / "computed-builds.json"))
    if not document_path.is_file():
        logging.error("no computed-builds document at %s", document_path)
        return 1
    document = json.loads(document_path.read_text(encoding="utf-8"))

    marked = projectioncheck.marked_builds(document)
    if not marked:
        logging.error(
            "%s carries no build whose computed talents beat simc's outside the tie "
            "band, so there is no projection to check",
            document_path,
        )
        return 1
    if args.build:
        marked = [e for e in marked if args.build in e["id"]]
    if args.limit:
        # Ranked by the margin the site actually draws, so a bounded run checks the
        # loudest claims rather than an arbitrary slice.
        marked.sort(key=lambda e: -(e["best"]["dps"] / e["simc"]["dps"]))
        marked = marked[: args.limit]
    if not marked:
        logging.error("no marked build matches %r", args.build)
        return 1

    found = profiles.discover(profiles_dir, tier, dps_only=True)
    by_id = {p.id: p for p in found}
    traits = talenttree_traits(simc_dir, args.ptr)
    corpus, _ = talentrepair.corpus_from(simc_dir, (tier,), ptr=args.ptr)
    if not corpus:
        logging.error("no shipped profile of %s decodes; cannot derive budget or framing", tier)
        return 1
    framing = talentrepair.observed_framing(corpus)
    budget = talentedit_budget(corpus, tier)
    try:
        target = _anchor_target_for(simc_dir, tier, found, args.ptr)
    except gearanchor.AnchorError as exc:
        logging.error("%s", exc)
        return 1

    simc = simc_runner.find_simc(args.simc)
    settings = SimSettings(target_error=0.0, max_iterations=args.iterations, threads=args.threads)

    rows: list[projectioncheck.Comparison] = []
    notes: list[str] = []
    for entry in marked:
        profile = by_id.get(entry["id"])
        if profile is None:
            notes.append(f"{entry['id']}: the tier no longer ships this profile, skipped")
            continue
        context = buildsearchrun.prepare(
            profile,
            traits=traits,
            target=_target_for(target, simc_dir, tier, found, profile, args.ptr),
            budget=budget,
            framing=framing,
            blind=False,
            seed_value=0,
        )
        if context.blocked:
            notes.append(f"{entry['id']}: {context.blocked}")
            continue
        candidates = projectioncheck.candidates_for(entry, context.nodes)
        if candidates is None:
            notes.append(f"{entry['id']}: a published talent hash will not decode, skipped")
            continue

        logging.info("%s: measuring anchored and shipped-gear head-to-head", entry["id"])
        anchored = buildsearchrun.measure(
            simc, context, settings, candidates, args.iterations, args.targets, args.timeout
        )
        # `gear=()` is the whole point: simc's own kit, only `talents=` overridden.
        shipped = buildsearchrun.measure(
            simc,
            context,
            settings,
            candidates,
            args.iterations,
            args.targets,
            args.timeout,
            gear=(),
        )
        published = (entry["best"]["dps"] / entry["simc"]["dps"]) - 1
        row = projectioncheck.compare(entry["id"], anchored, shipped, published)
        if row is None:
            notes.append(f"{entry['id']}: a side of the comparison did not measure, skipped")
            continue
        rows.append(row)
        logging.info(
            "%s: anchored %+.2f%%, shipped %+.2f%%, difference %+.2f pts (band %.2f)%s",
            row.build_id,
            row.anchored_margin * 100,
            row.shipped_margin * 100,
            row.difference * 100,
            row.band * 100,
            "" if row.reproduced is not False else "  [did NOT reproduce the published margin]",
        )

    for note in notes:
        logging.warning("%s", note)
    print(projectioncheck.verdict(rows))
    if args.report:
        projectioncheck.write_report(Path(args.report), tier, rows, notes)
        logging.info("wrote %s", args.report)
    return 0 if rows else 1


def cmd_build_search(args: argparse.Namespace) -> int:
    """Search for talent builds, calibrate the search, and publish what passes.

    Two commands' worth of work in one, because they are one decision: a search result
    is only publishable if the search has been shown to find the answer where the
    answer is known, and showing that is what ``--calibrate`` does. Running them apart
    would let a Step 5 pass be published against a Step 4 that was never run.
    """
    from . import buildsearch, buildsearchrun, computedbuilds, gearanchor, talentrepair

    simc_dir = Path(args.simc_source)
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    found = profiles.discover(profiles_dir, tier, dps_only=True)
    if not found:
        logging.error("no damage profile under %s/%s", profiles_dir, tier)
        return 1

    traits = talenttree_traits(simc_dir, args.ptr)
    corpus, _ = talentrepair.corpus_from(simc_dir, (tier,), ptr=args.ptr)
    if not corpus:
        logging.error(
            "no shipped profile of %s decodes, so neither the point budget nor the "
            "framing range the soundness screen needs can be derived",
            tier,
        )
        return 1
    framing = talentrepair.observed_framing(corpus)
    budget = talentedit_budget(corpus, tier)
    logging.info(
        "%s: budget %s, framing %s, from %d shipped build(s)",
        tier,
        dict(budget.per_tree),
        framing,
        len(corpus),
    )

    try:
        target = _anchor_target_for(simc_dir, tier, found, args.ptr)
    except gearanchor.AnchorError as exc:
        logging.error("%s", exc)
        return 1

    harvested = None
    if args.harvest:
        harvest_path = Path(args.harvest)
        if not harvest_path.is_file():
            logging.error("no harvested-builds document at %s", harvest_path)
            return 1
        harvested = buildsearchrun.read_harvested(harvest_path)
        logging.info("harvested seeds available for %d spec(s)", len(harvested))
    else:
        logging.info(
            "no --harvest document supplied: no seed in this run came from a real player's build"
        )

    selected = [p for p in found if not args.build or args.build in p.id]
    if not selected:
        logging.error("no build of %s matches %r", tier, args.build)
        return 1

    contexts = [
        buildsearchrun.prepare(
            profile,
            traits=traits,
            target=_target_for(target, simc_dir, tier, found, profile, args.ptr),
            budget=budget,
            framing=framing,
            blind=args.calibrate,
            seed_value=args.seed,
            harvested=harvested,
        )
        for profile in selected
    ]

    if args.plan:
        for context in contexts:
            state = context.blocked or f"{len(context.seeds)} seed(s)"
            mark = " [repaired]" if context.repair and context.repair.ok else ""
            print(f"{context.profile.id:46s} {state}{mark}")
        return 0

    simc = simc_runner.find_simc(args.simc)
    settings = SimSettings(target_error=0.0, max_iterations=args.iterations, threads=args.threads)
    rounds = (
        buildsearch.plan_rounds(1, start=args.iterations, final=args.iterations)
        if args.rounds == 1
        else None
    )

    entries: list[computedbuilds.SpecEntry] = []
    rows: list[computedbuilds.CalibrationRow] = []
    notes: list[str] = []
    out_dir = Path(args.out) / tier
    publishing = not args.calibrate or args.write_calibration
    if harvested is None:
        notes.append(
            "No harvested-builds document was available to this run, so no candidate "
            "came from a real player's build. Seeds were simc's own builds (repaired "
            "where simc refuses its own hash) and generated variants."
        )

    for context in contexts:
        if context.blocked:
            logging.warning("%s: %s", context.profile.id, context.blocked)
            entries.append(_unsearched_entry(context, args.targets, gearanchor))
            if publishing:
                _publish(args, tier, entries, rows, notes, len(found), out_dir)
            continue
        try:
            outcome = buildsearchrun.run_build(
                simc,
                context,
                settings,
                targets=args.targets,
                breadth=args.breadth,
                seed_value=args.seed,
                blind=args.calibrate,
                timeout=args.timeout,
                rounds=rounds,
                climb_steps=args.climb_steps,
            )
        except (simc_runner.SimcError, buildsearch.SearchError) as exc:
            logging.error("%s: search failed: %s", context.profile.id, exc)
            entries.append(_unsearched_entry(context, args.targets, gearanchor, reason=str(exc)))
            if publishing:
                _publish(args, tier, entries, rows, notes, len(found), out_dir)
            continue

        head_to_head, row = _head_to_head(
            simc, context, settings, outcome, args, buildsearchrun, buildsearch
        )
        if row is not None:
            rows.append(row)
            logging.info(
                "%s: %s (simc %.0f, search %s)",
                context.profile.id,
                row.verdict,
                row.simc.dps,
                "none" if row.found is None else f"{row.found.dps:.0f}",
            )
        entries.append(_entry_for(context, outcome, head_to_head, args, computedbuilds, gearanchor))
        if publishing:
            _publish(args, tier, entries, rows, notes, len(found), out_dir)

    # A head-to-head runs on every build, blind or not -- it is what fills the
    # document's `simc` side. It is only *calibration* when the search was blind,
    # because the gate's whole meaning is that the search did not see the answer.
    # Publishing a non-blind head-to-head under that name would be a gate that
    # graded the search on a paper it had already read.
    calibration = computedbuilds.Calibration(rows=tuple(rows)) if rows and args.calibrate else None
    if calibration is not None:
        print()
        print("CALIBRATION -- " + calibration.criterion())
        for row in calibration.rows:
            margin = "n/a" if row.margin is None else f"{row.margin:+.3%}"
            band = "n/a" if row.band is None else f"{row.band:.3%}"
            print(f"  {row.build_id:46s} {row.verdict:14s} {margin:>10s} band {band:>8s}")
        print("  " + calibration.summary())
        print()

    if publishing:
        path = _publish(args, tier, entries, rows, notes, len(found), out_dir)
        logging.info("wrote %s (%d entr(ies))", path, len(entries))
    else:
        logging.info("calibration run: nothing published (pass --write-calibration to record it)")

    if args.calibrate:
        # The gate decides the exit code, and a failure is a *result*: the workflow
        # reports it and refuses to commit rather than treating it as a broken run.
        return 0 if calibration is not None and calibration.passed else 2
    return 0


def _publish(args, tier, entries, rows, notes, builds_available, out_dir):
    """Rewrite the whole document. Called after **every** build, not once at the end.

    CLAUDE.md records this exact defect in the gear sweep: the entry claimed a per-spec
    write while ``write_gear`` was called once after the loop, so an interrupted sweep
    left *nothing* rather than a smaller dataset. A search costs CPU-hours, so being
    interrupted is the expected case, and ``coverage`` is already honest about covering
    fewer builds than the tier has.

    Worth naming how that defect nearly shipped again here: the edit that introduced
    this function was applied by a scripted string replacement whose anchor no longer
    matched after a reformat, so it silently did nothing -- while the CLAUDE.md entry
    describing per-build writes was written anyway. ``test_buildsearch_cli`` asserts the
    document really is rewritten per build, which is the only thing that can tell a
    described behaviour from an implemented one.
    """
    from . import computedbuilds

    calibration = computedbuilds.Calibration(rows=tuple(rows)) if rows and args.calibrate else None
    return computedbuilds.write_computed_builds(
        out_dir,
        computedbuilds.build_document(
            tier,
            entries,
            iterations=args.iterations,
            deterministic=True,
            builds_available=builds_available,
            calibration=calibration,
            notes=notes,
        ),
    )


def talenttree_traits(simc_dir: Path, ptr: bool) -> list:
    from . import talenttree

    return talenttree.parse_trait_data(simc_dir, ptr=ptr)


def talentedit_budget(corpus: list, tier: str):
    from . import talentedit

    return talentedit.derive_point_budget(corpus, source=f"{tier}'s shipped profiles")


def _anchor_target_for(simc_dir: Path, tier: str, found: list, ptr: bool):
    from . import buffsweep, gearanchor

    sets = buffsweep.parse_tier_sets(simc_dir, ptr=ptr)
    item_sets = None
    if sets is not None and not buffsweep.sets_for_tier(sets, tier):
        logging.warning("simc ships no set bonus labelled %s; anchoring gear alone", tier)
        sets = None
    if sets is not None:
        item_sets = gearanchor.parse_item_sets(simc_dir, ptr=ptr)
    return (sets, item_sets)


def _target_for(prepared, simc_dir: Path, tier: str, found: list, profile, ptr: bool):
    """The anchor target for one profile's class, derived once per class and cached.

    Only the set *name* and the zeroed tokens vary by class; the item level and the
    tally do not. Deriving per profile re-ran the whole tier tally once per profile.
    """
    from . import gearanchor

    sets, item_sets = prepared
    cache = _target_for.__dict__.setdefault("cache", {})
    key = (tier, profile.wow_class)
    if key not in cache:
        cache[key] = gearanchor.derive_target(
            found, tier, sets, item_sets, wow_class=profile.wow_class
        )
    return cache[key]


def _unsearched_entry(context, targets: int, gearanchor, reason: str | None = None):
    """A build no search covered, published as exactly that.

    ``searched: false`` and ``best: null`` are different sentences from ``searched:
    true`` and ``best: null``, and the site says something different for each -- "nobody
    has looked" against "somebody looked and found nothing". Collapsing them is the
    failure the display contract is shaped to prevent.
    """
    from . import computedbuilds

    caveats = list(context.caveats)
    if context.blocked:
        caveats.append(f"No search ran for this build: {context.blocked}")
    if reason:
        caveats.append(f"The search did not complete: {reason}")
    return computedbuilds.SpecEntry(
        build_id=context.profile.id,
        scenario="patchwerk",
        targets=targets,
        searched=False,
        simc=None,
        best=None,
        runner_up=None,
        anchor=gearanchor.display_json(context.anchor, profile=context.profile.id),
        caveats=caveats,
        repaired_seed=bool(context.repair and context.repair.ok),
    )


def _head_to_head(simc, context, settings, outcome, args, buildsearchrun, buildsearch):
    """Measure simc's own build and the search's winner in one field, at full precision.

    One invocation for both sides. Two profilesets in one run return an exact
    difference; two numbers from two runs at two iteration counts do not, and the tie
    band computed over them would be describing precisions that were never compared.
    """
    from . import talenttree

    field = []
    simc_key = "simcbuild"
    if context.profile.talent_hash and context.repair is None:
        original = context.profile.talent_hash
    elif context.repair is not None and context.repair.repaired_hash:
        original = context.repair.repaired_hash
    else:
        original = None

    if original:
        try:
            loadout = talenttree.decode_loadout(original, context.nodes)
        except talenttree.TalentDecodeError:
            original = None
        else:
            field.append(
                buildsearch.Candidate(
                    key=simc_key,
                    label=context.profile.display_name,
                    origin=buildsearch.ORIGIN_SIMC,
                    loadout=loadout,
                    talent_hash=original,
                )
            )
    for candidate, _ in outcome.ranked:
        field.append(candidate)
    if not field:
        return ({}, None, None), None

    measured = buildsearchrun.measure(
        simc, context, settings, field, args.iterations, args.targets, args.timeout
    )
    row = buildsearchrun.calibration_row(context, outcome, measured, simc_key) if original else None

    # The same two contenders again on simc's OWN kit, which is what the ranking should
    # use instead of projecting the anchored margin onto the published DPS (#72). Only
    # simc's build and the winner are re-measured: the runner-up is never ranked with,
    # so paying for it would buy nothing.
    #
    # Skipped in a blind run. `--calibrate` publishes nothing, and its `simc` candidate
    # is the scrambled build rather than simc's, so a shipped-gear margin taken there
    # would answer a question nobody asked at 33 seconds a build.
    from . import computedbuilds

    shipped: dict | None = None
    winner = outcome.ranked[0][0] if outcome.ranked else None
    # `getattr` rather than `args.shipped_gear`: callers that build their own Namespace
    # (the end-to-end test does, and it is the one that caught this) would otherwise
    # raise AttributeError deep inside a run. The default is True because that is the
    # safe direction -- a missing flag costs 33 seconds, where defaulting to False would
    # silently publish the projection this exists to replace.
    if (
        original
        and winner is not None
        and not args.calibrate
        and getattr(args, "shipped_gear", True)
    ):
        on_shipped = buildsearchrun.measure(
            simc,
            context,
            settings,
            [field[0], winner],
            args.iterations,
            args.targets,
            args.timeout,
            gear=(),
        )
        shipped = computedbuilds.shipped_json(on_shipped.get(simc_key), on_shipped.get(winner.key))
        if shipped is None:
            logging.warning(
                "%s: the shipped-gear head-to-head did not measure; the row falls back "
                "to the projection",
                context.profile.id,
            )
        else:
            logging.info(
                "%s: on simc's own gear %+.2f%%",
                context.profile.id,
                shipped["margin"] * 100,
            )

    # ``(what was measured, which of them is simc's, the shipped-gear block)``, then the
    # calibration row. The simc candidate travels with the measurements rather than
    # being rebuilt by the caller: in a blind run ``context.seeds[0]`` is the
    # *scrambled* build, and a caller reaching for it there would publish it under
    # simc's name.
    return (measured, field[0] if original else None, shipped), row


def _entry_for(context, outcome, head_to_head, args, computedbuilds, gearanchor):
    """One published row: simc's side, ours, the runner-up, the anchor and the caveats.

    The simc side is the candidate ``_head_to_head`` actually measured, not one rebuilt
    here. Rebuilding it read ``context.seeds[0]``, which in a **blind** run is the
    scrambled build rather than simc's -- harmless today because only the hash is
    published, and exactly the kind of thing that stops being harmless the moment
    another field is added.
    """
    measured, simc_candidate, shipped = head_to_head
    hero = context.profile.hero_label
    simc_side = None
    if simc_candidate is not None and simc_candidate.key in measured:
        simc_side = computedbuilds.contender_json(
            simc_candidate, measured[simc_candidate.key], hero_talent=hero
        )

    sides = [
        computedbuilds.contender_json(
            candidate, measured[candidate.key], hero_talent=hero, outcome=outcome
        )
        for candidate, _ in outcome.ranked
        if candidate.key in measured
    ]
    sides.sort(key=lambda side: -side["dps"])

    caveats = list(context.caveats) + outcome.caveats()
    if args.calibrate:
        caveats.append(
            "Blind run: the search began from a scrambled build and never saw simc's "
            "own choices. The node set is inherited and is not part of the claim."
        )
    return computedbuilds.SpecEntry(
        build_id=context.profile.id,
        scenario="patchwerk",
        targets=args.targets,
        searched=True,
        simc=simc_side,
        best=sides[0] if sides else None,
        runner_up=sides[1] if len(sides) > 1 else None,
        anchor=gearanchor.display_json(context.anchor, profile=context.profile.id),
        caveats=caveats,
        repaired_seed=bool(context.repair and context.repair.ok),
        shipped=shipped,
    )


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


def cmd_harvest_builds(args: argparse.Namespace) -> int:
    from . import harvest

    return harvest.cmd_harvest_builds(args)


def cmd_progress_hours(args: argparse.Namespace) -> int:
    """Measure progress hours per boss: attempts-until-first-kill, per guild.

    Answers the owner's chart question -- medians per boss, stacked per season --
    with the metric he chose over the two cheaper proxies. See ``progresshours``
    for why ``duration`` on a ranking row is not it.

    Budgeted rather than hoped: the pass reads the point counter before and after,
    honours ``--point-ceiling`` BEFORE sending, and stops cleanly with everything
    it has. A zero delta reports as UNMEASURED, never as free.
    """
    import json as _json

    # `harvest` owns the PTR/live twin rule and its refusals. Imported rather than
    # re-derived: a second copy of "strip the leading 5 and check the name" is exactly
    # the duplication this repo warns about, and the wrong copy files a season's
    # progression under the wrong boss with nothing downstream able to tell.
    from . import harvest, progresshours
    from .warcraftlogs import Credentials, WarcraftLogsClient, WarcraftLogsError

    tiers = _json.loads(
        (Path(__file__).parent / "data" / "fight_profiles.json").read_text(encoding="utf-8")
    )["tiers"]
    block = tiers.get(args.tier)
    if block is None:
        logging.error("no tier %r in fight_profiles.json (have %s)", args.tier, ", ".join(tiers))
        return 1
    encounters = block.get("encounters") or []
    if args.encounter:
        encounters = [e for e in encounters if int(e.get("encounterId") or 0) == args.encounter]
    if not encounters:
        logging.error("no encounter to measure for %s", args.tier)
        return 1

    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        logging.error("%s", exc)
        return 1

    bosses: list[progresshours.BossProgress] = []
    with WarcraftLogsClient(credentials) as client:
        before = client.rate_limit()
        limit = float(before.get("limitPerHour") or 0)
        start = float(before.get("pointsSpentThisHour") or 0)

        # `rate_limit()` is deliberately uncached (`warcraftlogs.rate_limit`), so every
        # call is a real HTTP query AND is counted in the ledger. Called once per guild
        # it dominated the run: the 20-guild MID1 pass sent 432 queries of which **180
        # -- 42% -- were budget polls**, and 232 did the work. The ceiling still has to
        # be checked BEFORE sending, so this polls once per boss and re-uses the reading
        # across that boss's guilds. Worst case it overshoots by one boss's guilds
        # rather than by one guild, which the 0.5-0.8 ceiling has ample room for.
        budget = {"spent": start}

        def refresh_budget() -> None:
            if limit:
                budget["spent"] = float(client.rate_limit().get("pointsSpentThisHour") or start)

        def over_ceiling() -> bool:
            if not limit:
                return False
            return budget["spent"] >= limit * args.point_ceiling

        for order, entry in enumerate(encounters, start=1):
            encounter_id = int(entry["encounterId"])
            boss = progresshours.BossProgress(
                encounter_id=encounter_id,
                name=entry.get("name") or str(encounter_id),
                order=order,
                difficulty=args.difficulty,
            )
            bosses.append(boss)
            # The ranking is PAGED, because one page holds 50 rows and the sample is
            # no longer smaller than that. Paging is not an optional extra at
            # `--guilds 50`: `usable[: args.guilds]` cannot return more than a page
            # holds, so without this a request for 200 would silently deliver 50 and
            # `guildsSeen` would record it as if that were the request.
            ranked: list[progresshours.RankedKill] = []
            ranking_failed = False
            pages = max(1, min(args.rankings_pages, progresshours.RANKING_MAX_PAGE))
            for page in range(1, pages + 1):
                try:
                    payload = client.query(
                        progresshours.PROGRESS_RANKINGS_QUERY,
                        {"e": encounter_id, "d": args.difficulty, "p": page},
                        label=f"progress:{encounter_id}:p{page}",
                    )
                except WarcraftLogsError as exc:
                    logging.warning("%s: %s", boss.name, exc)
                    if page == 1:
                        ranking_failed = True
                    break
                page_rows, missing = progresshours.ranking_rows(payload)
                # The WHOLE page is counted before the sample is cut: the page is
                # already paid for, and a prefix scan would report its null-id share
                # as the page's.
                boss.rows_without_guild += missing
                ranked.extend(page_rows)
                if len(page_rows) + missing < progresshours.RANKING_PAGE_SIZE:
                    break
                if len(ranked) >= args.guilds:
                    break
            if ranking_failed:
                boss.refused["ranking-error"] = boss.refused.get("ranking-error", 0) + 1
                continue

            # The zone is DERIVED from the encounter, never taken as 0. `zoneID: 0`
            # is not a narrower question: Warcraft Logs accepted it on 2026-08-26 and
            # answered with each guild's reports across all content, so every number
            # that run produced was scoped to the wrong thing while looking fine.
            zone_id = int(block.get("zoneId") or 0) or int(args.zone or 0)
            wcl_name = None
            if not zone_id:
                try:
                    payload = client.query(
                        progresshours.ENCOUNTER_ZONE_QUERY,
                        {"e": encounter_id},
                        label=f"zone:{encounter_id}",
                    )
                    zone_id = progresshours.encounter_zone(payload) or 0
                    # Fetched all along and discarded. It is what verifies the twin
                    # substitution below, so it costs no extra query.
                    wcl_name = progresshours.encounter_name(payload)
                except WarcraftLogsError as exc:
                    logging.warning("%s: zone lookup failed: %s", boss.name, exc)
                    zone_id = 0

            # A tier seeded from a PTR zone carries `5xxxx` encounter ids, and those
            # return NO progress-ranking rows at all -- measured on 2026-08-27, all
            # eight MID2 bosses, at Mythic AND at Heroic, 0 of 0 guilds each. So an
            # empty ranking on such an id is not a fact about the season; it is the
            # wrong address. `harvest.resolve_encounter` already owns this rule and
            # its refusals, and is reused rather than re-derived: a twin is taken
            # only when Warcraft Logs gives both ids the SAME NAME, because filing a
            # season's progression under the wrong boss is undetectable downstream.
            if not ranked:

                def _lookup(twin_id: int) -> str | None:
                    try:
                        return progresshours.encounter_name(
                            client.query(
                                progresshours.ENCOUNTER_ZONE_QUERY,
                                {"e": twin_id},
                                label=f"twin:{twin_id}",
                            )
                        )
                    except WarcraftLogsError:
                        return None

                choice = harvest.choose_encounter_id(encounter_id, wcl_name, False, _lookup)
                boss.read_as = choice.reason
                logging.info("%s: %s", boss.name, choice.reason)
                if choice.substituted and choice.used:
                    encounter_id = int(choice.used)
                    zone_id = 0
                    try:
                        payload = client.query(
                            progresshours.ENCOUNTER_ZONE_QUERY,
                            {"e": encounter_id},
                            label=f"zone:{encounter_id}",
                        )
                        zone_id = progresshours.encounter_zone(payload) or 0
                    except WarcraftLogsError as exc:
                        logging.warning("%s: twin zone lookup failed: %s", boss.name, exc)
                    for page in range(1, pages + 1):
                        try:
                            payload = client.query(
                                progresshours.PROGRESS_RANKINGS_QUERY,
                                {"e": encounter_id, "d": args.difficulty, "p": page},
                                label=f"progress:{encounter_id}:p{page}",
                            )
                        except WarcraftLogsError as exc:
                            logging.warning("%s: %s", boss.name, exc)
                            break
                        page_rows, missing = progresshours.ranking_rows(payload)
                        boss.rows_without_guild += missing
                        ranked.extend(page_rows)
                        if len(page_rows) + missing < progresshours.RANKING_PAGE_SIZE:
                            break
                        if len(ranked) >= args.guilds:
                            break

            if not zone_id:
                logging.warning("%s: no zone could be resolved; refusing the boss", boss.name)
                boss.refused["no-zone"] = boss.refused.get("no-zone", 0) + 1
                continue
            boss.zone_id = zone_id
            refresh_budget()

            sample = ranked[: args.guilds]
            boss.sample_short_of_request = len(sample) < args.guilds

            for entry_kill in sample:
                guild_id = entry_kill.guild_id
                # `guildsSeen` is incremented HERE rather than set to the sample size
                # up front. A ceiling stop mid-boss would otherwise publish "50 guilds
                # seen" above three rows -- a fraction of a run presented as the whole,
                # which is this repository's signature defect.
                boss.guilds_seen += 1

                # ── Screen 1: is the guild's first kill backed by a log at all? ──
                # `fromlog == 0` means Warcraft Logs holds no log behind it, so a
                # `reports(guildID:)` walk CANNOT find that kill and any kill it does
                # find is a later one. Refusing costs nothing and SAVES the whole
                # report walk; not refusing publishes a farm night as a progression.
                if entry_kill.from_log is None:
                    # Fail closed. A row stating neither field is a schema change, and
                    # reading absence as "passed" would switch both screens off while
                    # every published number still looked healthy.
                    boss.refused["ranking-row-unscreened"] = (
                        boss.refused.get("ranking-row-unscreened", 0) + 1
                    )
                    boss.record(guild_id, "ranking-row-unscreened", 0)
                    continue
                if entry_kill.from_log:
                    boss.kills_from_log += 1
                else:
                    boss.kills_not_from_log += 1
                    boss.refused["unlogged-kill"] = boss.refused.get("unlogged-kill", 0) + 1
                    boss.record(guild_id, "unlogged-kill", 0)
                    continue

                if over_ceiling():
                    logging.warning("point ceiling reached; stopping with what is measured")
                    return _write_progress_hours(args, bosses, client, start, limit)
                reports: list[dict] = []
                seen_codes: set[str] = set()
                duplicates = 0
                failed = False
                truncated = False
                for page in range(1, args.max_pages + 1):
                    try:
                        payload = client.query(
                            progresshours.GUILD_PULLS_QUERY,
                            {
                                "g": guild_id,
                                "z": zone_id,
                                "e": encounter_id,
                                "d": args.difficulty,
                                "page": page,
                            },
                            label=f"pulls:{encounter_id}",
                        )
                    except WarcraftLogsError as exc:
                        logging.warning("guild %s: %s", guild_id, exc)
                        boss.refused["error"] = boss.refused.get("error", 0) + 1
                        failed = True
                        break
                    listing = (payload.get("reportData") or {}).get("reports") or {}
                    for report in listing.get("data") or []:
                        # Keyed on the report code, because paging is not a snapshot: a
                        # listing that shifts between page 1 and page 2 can hand back the
                        # same report twice, and `pull_time` would then count its fights
                        # twice -- inflating `attempts` and `hours` with nothing to show
                        # that it happened. A report with no code cannot be deduplicated
                        # and is kept, since dropping it would lose real pulls.
                        code = report.get("code")
                        if code is not None and code in seen_codes:
                            duplicates += 1
                            continue
                        if code is not None:
                            seen_codes.add(code)
                        reports.append(report)
                    if not listing.get("has_more_pages"):
                        break
                else:
                    # Ran out of pages before the listing ran out of reports. The
                    # guild's FIRST kill may be older than anything fetched, so the
                    # window cannot support the claim and is refused.
                    truncated = True

                if truncated:
                    # Counted, never summed. Same rule as `no-kill`: a partial window
                    # published as the answer is a floor wearing a measurement's
                    # clothes. Raising --max-pages is the fix, and the count says so.
                    boss.refused["truncated"] = boss.refused.get("truncated", 0) + 1
                    boss.record(guild_id, "truncated", len(reports), duplicates)
                    continue
                if failed:
                    # Already counted as `error`. Falling through would count the
                    # SAME guild again as `no-fights`, which is what made both live
                    # runs report 12 of each on a 12-guild sample. `refused` is the
                    # denominator for "cost per usable number", and doubling it
                    # biases the extrapolation toward looking affordable.
                    boss.record(guild_id, "error", len(reports), duplicates)
                    continue
                # ── Screen 2: the kill we found must BE the ranked first kill. ──
                # Without the ranked time the walk finds *a* kill and calls it the
                # first one, which is wrong in two ways it cannot see: the real first
                # kill on an unlogged night (so this is a farm kill weeks later, with
                # every pull before it counted as progression toward a kill that had
                # already happened), or a log holding farm nights only.
                answer = progresshours.pull_time(
                    reports, encounter_id, args.difficulty, entry_kill.kill_time_ms
                )
                if answer.ms is None:
                    if answer.reason:
                        boss.refused[answer.reason] = boss.refused.get(answer.reason, 0) + 1
                    boss.record(guild_id, answer.reason or "unknown", answer.reports, duplicates)
                else:
                    hours = answer.ms / progresshours.MS_PER_HOUR
                    boss.hours.append(hours)
                    boss.attempts.append(answer.attempts)
                    boss.record(
                        guild_id,
                        "measured",
                        answer.reports,
                        duplicates,
                        hours,
                        answer.attempts,
                        answer,
                    )

            logging.info(
                "%s: %d/%d guild(s) measured, median %s h",
                boss.name,
                len(boss.hours),
                boss.guilds_seen,
                "n/a" if not boss.hours else f"{progresshours.median(boss.hours):.2f}",
            )

        return _write_progress_hours(args, bosses, client, start, limit)


def _progress_screen_totals(bosses) -> dict:
    """What the two completeness screens removed, across the whole pass.

    Published because the screens change what the number MEANS, not merely how many
    rows survive: every guild counted here is one the metric is unanswerable for, and
    a reader has to be able to size that population without re-deriving it from the
    per-guild rows. `unscreenedRows` being non-zero is a schema alarm rather than a
    property of the guilds -- it means a ranking row carried neither field.
    """
    keys = {
        "unloggedKill": "unlogged-kill",
        "killTooLate": "kill-too-late",
        "killTooEarly": "kill-too-early",
        "unscreenedRows": "ranking-row-unscreened",
    }
    totals = {name: 0 for name in keys}
    for boss in bosses:
        for name, reason in keys.items():
            totals[name] += boss.refused.get(reason, 0)
    totals["killsFromLog"] = sum(b.kills_from_log for b in bosses)
    totals["killsNotFromLog"] = sum(b.kills_not_from_log for b in bosses)
    return totals


def _write_progress_hours(args, bosses, client, start: float, limit: float) -> int:
    """Write the document, with the cost stated as measured or UNMEASURED."""
    import json as _json

    from . import progresshours

    after = client.rate_limit()
    spent = float(after.get("pointsSpentThisHour") or 0) - start
    cost: dict = {
        "limitPerHour": limit or None,
        "queries": len(client.ledger.entries) if hasattr(client.ledger, "entries") else None,
    }
    # A counter that did not move is the ABSENCE of a measurement, not a cost of
    # zero. This project printed "0 points for a nine-boss pass" exactly once.
    cost["pointsSpent"] = round(spent, 1) if spent > 0 else "UNMEASURED"
    # Points are not the only budget. Whatever Warcraft Logs says about a REQUEST
    # ceiling arrives in the response headers, which nothing here read until now, so
    # a pass could sit inside 18,000 points and hit a limit it never measured -- and
    # the 429 handler would have called that "the hourly point budget is spent".
    # Recorded, not enforced: that such a header exists is not established from here.
    ledger = getattr(client, "ledger", None)
    cost["requestsSent"] = getattr(ledger, "requests_sent", None)
    headers = dict(getattr(ledger, "request_headers", {}) or {})
    if headers:
        cost["rateLimitHeaders"] = headers

    document = {
        "tier": args.tier,
        "difficulty": args.difficulty,
        "difficultyName": progresshours.DIFFICULTY_NAMES.get(args.difficulty, str(args.difficulty)),
        "note": (
            "Progress time is the sum of every attempt up to and including a guild's "
            "first kill, per boss. A boss nobody could be measured for carries null, "
            "never zero, and a season total is refused unless every boss has one. "
            "Guilds whose first kill is not backed by a log are refused before any "
            "report is read, and a logged kill that does not match the ranked kill "
            "time is refused, so a published figure is a LOWER BOUND: a raid night "
            "that was never uploaded is invisible to this and to every other reader "
            "of the Warcraft Logs API. Read medianNightsObserved beside "
            "medianSpanDays -- few nights across a wide span is what a partial "
            "observation looks like from outside."
        ),
        # Not a caveat in prose: a reader that never reads `note` still has to be
        # able to tell that this is a floor.
        "metricIsFloor": True,
        # How large the population is that the metric cannot address, as a number
        # rather than as a line in a log nobody kept.
        "screens": _progress_screen_totals(bosses),
        "guildsRequested": args.guilds,
        "bosses": [boss.to_json() for boss in bosses],
        "seasonTotalHours": progresshours.stacked_total(bosses),
        "cost": cost,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    print(f"points: {cost['pointsSpent']}")
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
    """Record which hero tree every build of a tier plays.

    Every spec plays a hero tree; simc's profile name states it for most builds and
    abbreviates or omits it for the rest. This decodes each profile's talent hash
    against simc's trait table and names the sub-tree it selects from simc's own
    ``__trait_sub_tree_data``, then writes the result to the checked-in data file
    `profiles.discover` reads.

    Derived rather than hand-typed, and it needs no compiled simc -- a sparse
    checkout of ``engine/dbc/generated`` and ``profiles`` is the whole input -- so a
    new tier needs a re-run and not an edit.
    """
    from . import herotrees

    simc_dir = Path(args.simc_source)
    profiles_dir = Path(args.profiles) if args.profiles else simc_dir / "profiles"
    tier = _resolve_tier(profiles_dir, args.tier)

    # `tiers.json` outlives simc's profile directories -- the publish job loops over
    # published tiers, and simc deletes an old tier's profiles eventually -- so a tier
    # with nothing to read is a normal state and not an error. Same tolerance
    # `build_index` already has.
    if not (profiles_dir / tier).is_dir():
        logging.warning(
            "simc no longer ships %s under %s; leaving its recorded hero trees alone",
            tier,
            profiles_dir,
        )
        return 0

    # Which trait table -- live or PTR -- has to match the one the dataset was built
    # against, or the node stream desynchronises and the decode quietly describes a
    # different tree. The published manifest is where that is recorded, so it is read
    # from there rather than assumed; `--ptr/--no-ptr` overrides it for a tier that
    # has never been published.
    ptr = args.ptr
    if ptr is None:
        manifest_path = Path(args.out) / tier / "index.json"
        if manifest_path.is_file():
            ptr = simc_runner.manifest_used_ptr_data(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        else:
            logging.info("no manifest at %s; reading simc's live trait table", manifest_path)
            ptr = simc_runner.USES_PTR_DATA

    result = herotrees.resolve_tier(
        profiles_dir,
        tier,
        simc_dir,
        ptr=ptr,
        dps_only=not args.include_tanks,
    )
    for name, tree in sorted(result.resolved.items()):
        print(f"  {name:<44} -> {tree}")
    for name, carried, canonical in sorted(result.renamed):
        # Expected for an abbreviation; a finding if the two name different trees.
        print(f"  ! {name:<44} profile says {carried!r}, simc's table says {canonical!r}")
    for name, reason in sorted(result.unresolved):
        logging.warning("could not name the hero tree of %s: %s", name, reason)

    if not result.resolved:
        # Loud, and **not** a failure unless somebody is gating on it. This runs in a
        # `for tier in ...; done` loop in the publish job under `bash -e`, so a
        # non-zero exit here aborts that job before the commit step and discards a
        # whole night's simulations -- over a data file whose absence costs only the
        # canonical name, since every build keeps whatever its own profile said.
        # `--strict` is the gate for anyone who wants one.
        logging.error(
            "%s: nothing resolved -- check that %s carries engine/dbc/generated",
            tier,
            simc_dir,
        )
        return 1 if args.strict else 0
    if args.write:
        path = herotrees.write_overrides(tier, result.resolved)
        print(f"wrote {path}")
    else:
        print("(dry run; pass --write to record this)")
    print(
        f"{tier}: named {len(result.resolved)} builds, "
        f"{len(result.unresolved)} unresolved, {len(result.renamed)} renamed"
    )
    # A profile whose talent hash no longer decodes is a fact about that profile --
    # simc's disabled Havoc profiles are two of them today -- and it costs only the
    # canonical name, since the build keeps whatever its own profile name said. So
    # reporting is the point, and this is a failure only when used as a gate.
    return 1 if result.unresolved and args.strict else 0


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
        recorded = simc_runner.manifest_used_ptr_data(
            json.loads(manifest_path.read_text(encoding="utf-8"))
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
    # Stated, not discovered. The predecessor read it out of a manifest under --out,
    # which in a sharded run is a fresh scratch directory, so it was never anything
    # but False; and the flag it read (`simc.ptr`) is simc's SC_USE_PTR compile
    # constant rather than the data source the sims used. Same spelling as
    # `gear-anchor`, which reads the same two tables. See `_tier_set_reference`.
    p_build.add_argument(
        "--ptr",
        action=argparse.BooleanOptionalAction,
        default=simc_runner.USES_PTR_DATA,
        help="read simc's PTR item and set-bonus tables when checking each build's "
        "tier-set state. The default tracks which client data this pipeline's sims "
        "actually read, which is the live one",
    )
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

    p_search = sub.add_parser(
        "build-search",
        help="search for talent builds, calibrate the search, publish what passes",
    )
    p_search.add_argument("--tier", default="latest")
    p_search.add_argument("--profiles", default=".work/simc/profiles")
    p_search.add_argument(
        "--simc-source",
        default=".work/simc",
        help="simc checkout, for the trait table and the item/set tables",
    )
    p_search.add_argument("--simc", default=None, help="path to the simc binary")
    p_search.add_argument("--out", default="web/public/data")
    p_search.add_argument("--build", default=None, help="only builds whose id contains this")
    p_search.add_argument("--targets", type=int, default=1)
    p_search.add_argument(
        "--iterations", type=int, default=buildsearch_final(), help="final-round iterations"
    )
    p_search.add_argument("--breadth", type=int, default=24, help="neighbours generated per seed")
    p_search.add_argument(
        "--climb-steps",
        type=int,
        default=buildsearch_climb(),
        help="how many real one-edit improvements the opening phase will chase; 0 disables",
    )
    p_search.add_argument(
        "--seed", type=int, default=0, help="PRNG seed; the run is reproducible from it"
    )
    p_search.add_argument("--threads", type=int, default=0)
    p_search.add_argument("--timeout", type=int, default=3600)
    p_search.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="1 collapses the schedule to a single round at --iterations (for smoke runs)",
    )
    p_search.add_argument(
        "--calibrate",
        action="store_true",
        help=(
            "blind run: every choice node is scrambled before the first round, so the "
            "search never sees simc's own choices. Publishes nothing unless "
            "--write-calibration is also given; exits 2 when the gate fails."
        ),
    )
    p_search.add_argument(
        "--write-calibration",
        action="store_true",
        help="publish the document from a calibration run as well (for inspection)",
    )
    p_search.add_argument(
        "--no-shipped-gear",
        dest="shipped_gear",
        action="store_false",
        help=(
            "skip the second head-to-head on simc's own kit. On by default: without it "
            "the site has to project the anchored margin onto the published DPS, which "
            "was measured on 2026-08-26 to be wrong by 2.52 points on one of twelve "
            "marked builds. Costs one extra invocation per build (~33s measured)."
        ),
    )
    p_search.add_argument(
        "--harvest",
        default=None,
        help="a harvested-builds.json, whose builds become seeds labelled as harvested",
    )
    p_search.add_argument("--ptr", action="store_true", default=True)
    p_search.add_argument("--no-ptr", dest="ptr", action="store_false")
    p_search.add_argument(
        "--plan",
        action="store_true",
        help="report which builds are searchable and where each seed comes from, and stop",
    )
    p_search.set_defaults(func=cmd_build_search)

    p_proj = sub.add_parser(
        "projection-check",
        help="does a talent gain measured on the gear anchor hold on simc's own kit?",
    )
    p_proj.add_argument("--tier", default="latest")
    p_proj.add_argument("--profiles", default=".work/simc/profiles")
    p_proj.add_argument("--simc-source", default=".work/simc")
    p_proj.add_argument("--simc", default=None, help="path to the simc binary")
    p_proj.add_argument("--out", default="web/public/data")
    p_proj.add_argument(
        "--document",
        default=None,
        help="computed-builds.json to read; defaults to <out>/<tier>/computed-builds.json",
    )
    p_proj.add_argument("--build", default=None, help="only builds whose id contains this")
    p_proj.add_argument(
        "--limit",
        type=int,
        default=0,
        help="check only the N largest published margins. 0 = every marked build",
    )
    p_proj.add_argument("--targets", type=int, default=1)
    p_proj.add_argument("--iterations", type=int, default=3000)
    p_proj.add_argument("--threads", type=int, default=0)
    p_proj.add_argument("--timeout", type=int, default=3600)
    p_proj.add_argument("--report", default=None, help="write the run's findings here as JSON")
    p_proj.add_argument("--ptr", action="store_true", default=True)
    p_proj.add_argument("--no-ptr", dest="ptr", action="store_false")
    p_proj.set_defaults(func=cmd_projection_check)

    p_hours = sub.add_parser(
        "progress-hours",
        help="measure progress hours per boss (attempts until first kill), per guild",
    )
    p_hours.add_argument("--tier", default="MID1", help="tier in fight_profiles.json")
    p_hours.add_argument(
        "--encounter", type=int, default=0, help="one encounter id, or 0 for the whole tier"
    )
    p_hours.add_argument(
        "--zone", type=int, default=0, help="Warcraft Logs zone id, when the tier states none"
    )
    p_hours.add_argument(
        "--difficulty", type=int, default=5, help="3 Normal, 4 Heroic, 5 Mythic. Default 5"
    )
    p_hours.add_argument(
        "--guilds",
        type=int,
        default=50,
        # 50 rather than 12, and the argument is the STABILITY of the median rather
        # than the budget. At 20 the measured samples were 8-16 guilds per boss, and
        # screen 1 removes another 10-20% of those before a report is fetched. 50
        # lands at roughly 20-35 measured guilds, above the `quartiles` floor with
        # room to spare. It is also exactly one ranking page, so the common case
        # still costs one ranking query per boss.
        help="guilds sampled per boss",
    )
    p_hours.add_argument(
        "--max-pages", type=int, default=4, help="report pages per guild before giving up"
    )
    p_hours.add_argument(
        "--rankings-pages",
        type=int,
        default=3,
        # One page is 50 rows, so --guilds above 50 is unreachable without this. Three
        # pages is 150 guilds' worth of headroom at no cost when the sample fits in
        # one: the loop stops as soon as it has enough, or as soon as a page comes
        # back short.
        help="progress-ranking pages to read per boss (50 guilds each)",
    )
    p_hours.add_argument(
        "--point-ceiling",
        type=float,
        # 0.7 rather than 0.5. At 50 guilds over nine bosses the arithmetic on the one
        # real measurement (261 queries / 3,895.1 points for 9 x 20, run 32990582509)
        # puts a pass near 55% of an hourly budget, and 0.5 stops it just short of
        # finishing. There is no resume, so a ceiling stop discards the whole pass --
        # which makes a too-low ceiling the more expensive mistake here, not the safer
        # one. Screen 1 refunds a further 10-20% by refusing before the report walk.
        default=0.7,
        help="stop before spending this share of the hourly budget",
    )
    p_hours.add_argument("--out", default="progress-hours.json", help="where to write the document")
    p_hours.set_defaults(func=cmd_progress_hours)

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

    p_anchor = sub.add_parser(
        "gear-anchor",
        help="the normalized kit a computed build of a tier would wear",
    )
    p_anchor.add_argument("--tier", default="latest")
    p_anchor.add_argument("--profiles", default="simc/profiles")
    p_anchor.add_argument(
        "--simc-source", default="simc", help="a simc checkout, for engine/dbc/generated"
    )
    p_anchor.add_argument("--profile", help="only profiles whose filename contains this")
    p_anchor.add_argument(
        "--options", action="store_true", help="print the simc option lines themselves"
    )
    p_anchor.add_argument("--ptr", action="store_true")
    p_anchor.set_defaults(func=cmd_gear_anchor)

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

    p_extra = sub.add_parser(
        "extra-builds",
        help="materialise the builds this project supplies for missing (spec, hero tree) cells",
    )
    p_extra.add_argument("--tier", default="latest")
    p_extra.add_argument("--simc-source", default="simc")
    p_extra.add_argument(
        "--out", default=None, help="destination directory (default: the tier's profile dir)"
    )
    p_extra.add_argument("--write", action="store_true")
    p_extra.add_argument(
        "--strict", action="store_true", help="exit non-zero when any cell is refused"
    )
    p_extra.set_defaults(func=cmd_extra_builds)

    p_unvalidated = sub.add_parser(
        "unvalidated",
        help="list or write out the profiles simc wrote and left commented out",
    )
    p_unvalidated.add_argument("--tier", default="latest")
    p_unvalidated.add_argument("--simc-source", default="simc")
    p_unvalidated.add_argument(
        "--out",
        help="directory to write the profiles into (default: the tier's own profile directory)",
    )
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

    p_harvest = sub.add_parser(
        "harvest-builds",
        help="collect the talent builds and gear real players killed a boss with, "
        "from Warcraft Logs (needs credentials)",
    )
    from . import harvest

    harvest.add_arguments(p_harvest)
    p_harvest.set_defaults(func=cmd_harvest_builds)

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
        help="name the hero tree every build of a tier plays, from its talent hash "
        "and simc's own hero tree table, and record it for the dataset",
    )
    add_common(p_hero)
    # A profiles path is optional here: the one input is a simc checkout, and its
    # profiles directory is inside it.
    p_hero.set_defaults(profiles=None)
    p_hero.add_argument(
        "--simc-source",
        default="simc",
        help="a simc checkout (profiles + engine/dbc/generated). No binary needed",
    )
    p_hero.add_argument(
        "--ptr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="read simc's PTR trait table (default: whatever the published manifest "
        "for this tier says the dataset was built against)",
    )
    p_hero.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="where the published dataset lives, for reading the live/PTR flag",
    )
    p_hero.add_argument(
        "--write", action="store_true", help="record the result in the checked-in data file"
    )
    p_hero.add_argument(
        "--strict", action="store_true", help="exit non-zero when any profile is unresolved"
    )
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
