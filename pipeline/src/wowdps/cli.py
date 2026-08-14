"""Command line entry point: ``wowdps <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import (
    dataset,
    equipment,
    fightdataset,
    fightprofile,
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
            raise KeyError(
                f"no boss in {tier} has a fight profile with anything asserted or "
                f"measured in it, so there is no boss scenario to run. "
                f"`wowdps fight-probe` measures them; `wowdps fight-promote` writes "
                f"a measurement into a profile."
            )

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

    manifest = dataset.write_manifest(
        out_dir, results, selected_scenarios, tier, simc_meta, settings
    )
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
    path = fightdataset.write_fights(Path(args.out) / args.tier, document)
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
    import json

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

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"tier": tier, "specs": [r.to_json() for r in results]}, indent=1) + "\n",
            encoding="utf-8",
        )
        logging.info("wrote %s (%d spec/target combinations)", out, len(results))
    return 0 if results else 1


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
    p_fights.set_defaults(func=cmd_fights)

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
    p_talents.add_argument("--out", help="also write the results as JSON")
    p_talents.set_defaults(func=cmd_talents)

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
