"""Command line entry point: ``wowdps <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import dataset, equipment, gearsweep, profiles, scenarios, simc_runner
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


def cmd_build(args: argparse.Namespace) -> int:
    profiles_dir = Path(args.profiles)
    tier = _resolve_tier(profiles_dir, args.tier)
    simc = simc_runner.find_simc(args.simc)
    # Datasets are namespaced by tier: a tier is a different game state, not a filter.
    out_root = Path(args.out)
    out_dir = out_root / tier

    selected_scenarios = (
        [scenarios.get(s) for s in args.scenario]
        if args.scenario
        else list(scenarios.ALL_SCENARIOS)
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
        choices=sorted(scenarios.BY_ID),
        help="limit to one scenario (repeatable)",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
