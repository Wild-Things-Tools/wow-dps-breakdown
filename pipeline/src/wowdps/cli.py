"""Command line entry point: ``wowdps <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import dataset, profiles, scenarios, simc_runner
from .scenarios import SimSettings

DEFAULT_OUT = Path("web/public/data")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )


def _resolve_tier(profiles_dir: Path, requested: str | None) -> str:
    if requested and requested != "latest":
        return requested
    return profiles.latest_tier(profiles_dir)


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
    out_dir = Path(args.out)

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
    failed = sum(len(r.errors) for r in results)
    logging.info("wrote %s (%d specs, %d failed cells)", manifest, len(results), failed)
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    shard_dirs = [Path(p) for p in args.shards]
    missing = [p for p in shard_dirs if not p.is_dir()]
    if missing:
        logging.error("shard directories not found: %s", missing)
        return 1
    dataset.merge_shards(shard_dirs, Path(args.out))
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
    p_build.add_argument("--target-error", type=float, default=0.2)
    p_build.add_argument("--max-iterations", type=int, default=30000)
    p_build.add_argument("--threads", type=int, default=0)
    p_build.add_argument("--timeout", type=int, default=1800)
    p_build.set_defaults(func=cmd_build)

    p_merge = sub.add_parser("merge", help="merge sharded dataset directories")
    p_merge.add_argument("shards", nargs="+", help="shard directories to merge")
    p_merge.add_argument("--out", default=str(DEFAULT_OUT))
    p_merge.set_defaults(func=cmd_merge)

    p_verify = sub.add_parser(
        "verify", help="cross-check sim output against Warcraft Logs rankings"
    )
    p_verify.add_argument("--data", default=str(DEFAULT_OUT), help="dataset directory")
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
