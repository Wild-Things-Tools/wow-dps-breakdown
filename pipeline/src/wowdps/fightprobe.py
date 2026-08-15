"""``wowdps fight-probe``: ask Warcraft Logs what one boss fight actually looks like.

This is the exploratory half of the fight-profile work. It answers a question that
cannot be answered from a development checkout -- the API credentials are GitHub
Actions secrets -- so the command is built to be run in CI
(``.github/workflows/fight-probe.yml``) and to dump everything it saw as an
artifact, rather than to quietly write a dataset.

The route from a boss to its logs
---------------------------------
Ranking entries carry the ``report.code`` and ``report.fightID`` of the parse they
came from, so ``worldData.encounter(id).characterRankings`` is already a list of
logs to read. No report search, no guild lookup, one query per encounter.

That route has a bias worth stating in the output rather than in a footnote: the
top of the rankings is the top of the world. Those pulls are shorter than a typical
guild's, may skip a phase, and are exactly the pulls where adds die fastest. A
fight profile built from them describes *speed-kill shape*, which is not what the
median parse the site compares against experienced. Sampling further down the
rankings is a page parameter away; knowing that it matters is the point.

Cost control, because points are the binding constraint
-------------------------------------------------------
Warcraft Logs meters by points per hour and does not publish the cost function.
Three things follow, all implemented here:

* **Measure, do not predict.** Every query asks for ``rateLimitData`` alongside its
  payload, and the run is bracketed by two standalone readings, so the report ends
  with what the pass actually cost.
* **Stop before the wall.** ``--point-ceiling`` aborts the pass when the hour's
  budget is more than a given fraction spent (Warcraft Logs' own advice is 0.8).
  An aborted pass writes what it has, the same way the gear sweep does.
* **Cache everything.** Responses are stored on disk by query and variables, so
  re-running the probe to iterate on the extraction costs nothing. This is what
  makes it reasonable to run the probe once in CI, download the cache, and work
  offline against it.

The event volume problem
------------------------
Enemy damage-taken events are the only source that sees *every* target -- an add
that never casts and never dies still gets hit -- and they are also the largest
event stream a 20-player Mythic pull produces. A five-minute fight can run past
what a few pages hold. So the fetch is paginated, bounded, and reports truncation
rather than pretending a partial timeline is a whole one; ``--no-damage-events``
falls back to casts, which is far cheaper and misses adds nobody's log recorded a
cast for.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import fightdataset, fightextract, fightprofile
from .warcraftlogs import (
    Credentials,
    WarcraftLogsClient,
    WarcraftLogsError,
    select_report_fights,
)

log = logging.getLogger(__name__)

#: Event streams the probe can fetch, and what each one is for. Names are the
#: API's ``EventDataType`` values; hostility is Enemies throughout, because every
#: question here is about the things being fought rather than the raid.
EVENT_STREAMS = {
    "damage": ("DamageTaken", "when each enemy instance was being fought"),
    "casts": ("Casts", "a cheaper presence signal: enemies that cast something"),
    "deaths": ("Deaths", "when each enemy instance died"),
    "buffs": ("Buffs", "auras the encounter puts on its own enemies"),
    "debuffs": ("Debuffs", "auras players put on enemies"),
}

#: Enough to answer every structural question in one pass. ``casts`` is left out
#: because ``damage`` subsumes it as a presence signal and doubles the cost.
DEFAULT_STREAMS = ("damage", "deaths", "buffs", "debuffs")

#: Warcraft Logs' own guidance for a client that wants to keep working: stop at
#: 80% of the hour's points rather than discovering the limit as a 429.
DEFAULT_POINT_CEILING = 0.8


class PointBudgetExhausted(RuntimeError):
    """Raised to end a pass cleanly when the hourly point budget is nearly spent."""


@dataclass
class ProbeSettings:
    encounter_ids: tuple[int, ...]
    difficulty: int
    metric: str
    reports: int
    rankings_page: int
    #: How the sampled kills are chosen: "first" (earliest kills, alike and at the
    #: intended tuning) or "top" (the rankings' own damage order, i.e. speed kills).
    order: str
    #: How many ranking pages to gather before choosing, so the earliest kills are in
    #: the pool WCL sorts by damage rather than by date.
    rankings_pages: int
    streams: tuple[str, ...]
    events_limit: int
    max_pages: int
    point_ceiling: float
    significant_share: float


def _check_budget(client: WarcraftLogsClient, ceiling: float) -> None:
    ledger = client.ledger
    if not ledger.limit_per_hour or ledger.last_reading is None:
        return
    if ledger.last_reading / ledger.limit_per_hour > ceiling:
        raise PointBudgetExhausted(
            f"{ledger.last_reading:.0f} of {ledger.limit_per_hour} points spent this "
            f"hour (ceiling {ceiling:.0%}); stopping with what has been collected"
        )


def probe_encounter(
    client: WarcraftLogsClient,
    encounter_id: int,
    settings: ProbeSettings,
) -> tuple[fightextract.EncounterObservation, str | None]:
    """Sample a handful of logged fights of one encounter and describe their shape.

    Returns the observation and, if the point budget ran out part-way, the reason.
    The observation comes back either way: two fights read before the budget ran out
    are two fights' worth of evidence, and throwing them away to raise an exception
    would mean paying for them twice.
    """
    # Gather several ranking pages so the earliest kills are actually in the pool:
    # WCL sorts rankings by damage, so the first kills sit deep in the list, not on
    # page one. The pages are cheap -- the per-fight event streams are the cost -- so
    # one extra page than strictly needed is a rounding error against a probe run.
    pages = settings.rankings_pages if settings.order == "first" else 1
    gathered = []
    for page in range(settings.rankings_page, settings.rankings_page + max(pages, 1)):
        _check_budget(client, settings.point_ceiling)
        gathered.append(
            client.encounter_rankings(
                encounter_id,
                difficulty=settings.difficulty,
                metric=settings.metric,
                page=page,
            )
        )
    encounter = gathered[0]
    pairs = select_report_fights(gathered, settings.reports, order=settings.order)
    observation = fightextract.EncounterObservation(
        encounter_id=encounter_id,
        encounter_name=str(encounter.get("name") or encounter_id),
        difficulty=settings.difficulty,
    )
    if not pairs:
        log.warning("encounter %d: rankings carried no report codes", encounter_id)
        return observation, None

    for code, fight_id in pairs:
        try:
            _check_budget(client, settings.point_ceiling)
            fight = _probe_fight(client, code, fight_id, encounter_id, settings)
        except PointBudgetExhausted as exc:
            return observation, str(exc)
        except WarcraftLogsError as exc:
            log.warning("  %s fight %d: %s", code, fight_id, exc)
            continue
        if fight:
            observation.fights.append(fight)
            log.info(
                "  %s#%d: %.0fs, %d players, %g targets peak, %d add group(s), %d aura(s)",
                code,
                fight_id,
                fight.duration,
                fight.players,
                fight.significant_timeline.peak,
                len(fight.adds),
                len(fight.auras),
            )
    return observation, None


def _probe_fight(
    client: WarcraftLogsClient,
    code: str,
    fight_id: int,
    encounter_id: int,
    settings: ProbeSettings,
) -> fightextract.FightObservation | None:
    report = client.fight_structure(code, encounter_id, settings.difficulty)
    fights = report.get("fights") or []
    fight = next((f for f in fights if f.get("id") == fight_id), None)
    if fight is None:
        log.warning("  %s: fight %d not in the report's %s fights", code, fight_id, encounter_id)
        return None

    master = report.get("masterData") or {}
    actors = master.get("actors") or []
    actor_names = {
        a["id"]: a.get("name") or str(a["id"]) for a in actors if isinstance(a.get("id"), int)
    }
    actor_game_ids = {
        a["id"]: a["gameID"]
        for a in actors
        if isinstance(a.get("id"), int) and isinstance(a.get("gameID"), int)
    }
    friendly_ids = fightextract.friendly_source_ids(actors)
    ability_names = {
        a["gameID"]: a.get("name") or str(a["gameID"])
        for a in (master.get("abilities") or [])
        if isinstance(a.get("gameID"), int)
    }
    phase_metadata = _phase_metadata(report, encounter_id)

    start_ms, end_ms = float(fight.get("startTime") or 0), float(fight.get("endTime") or 0)
    collected: dict[str, list[dict]] = {}
    truncated = False
    for stream in settings.streams:
        data_type, _ = EVENT_STREAMS[stream]
        _check_budget(client, settings.point_ceiling)
        events, cut = client.fight_events(
            code,
            fight_id,
            data_type=data_type,
            hostility="Enemies",
            start_ms=start_ms,
            end_ms=end_ms,
            limit=settings.events_limit,
            max_pages=settings.max_pages,
        )
        collected[stream] = events
        truncated = truncated or cut

    presence = collected.get("damage") or collected.get("casts") or []
    aura_events = (collected.get("buffs") or []) + (collected.get("debuffs") or [])

    _check_budget(client, settings.point_ceiling)
    damage_table = client.fight_table(code, fight_id, "DamageDone", view_by="Target")
    player_table = client.fight_table(code, fight_id, "DamageDone", view_by="Source")

    return fightextract.observe_fight(
        report_code=code,
        fight=fight,
        damage_events=presence,
        death_events=collected.get("deaths") or [],
        aura_events=aura_events,
        phase_metadata=phase_metadata,
        actor_names=actor_names,
        actor_game_ids=actor_game_ids,
        ability_names=ability_names,
        damage_table=damage_table,
        player_table=player_table,
        truncated=truncated,
        friendly_ids=friendly_ids,
        significant_share=settings.significant_share,
    )


def _phase_metadata(report: dict, encounter_id: int) -> list[dict]:
    """``report.phases`` is per encounter; pick out this one's phase names."""
    for entry in report.get("phases") or []:
        if isinstance(entry, dict) and entry.get("encounterID") == encounter_id:
            return [p for p in (entry.get("phases") or []) if isinstance(p, dict)]
    return []


# --------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------


def render(observation: fightextract.EncounterObservation, profile) -> list[str]:
    """The human-readable dump. This is the actual deliverable of a probe run."""
    lines: list[str] = []
    add = lines.append

    codes = ", ".join(sorted({fight.report_code for fight in observation.fights})) or "-"
    add(f"=== {observation.encounter_name} (encounter {observation.encounter_id}) ===")
    add(f"fights sampled: {len(observation.fights)} across reports {codes}")
    if not observation.fights:
        add("nothing to report: no fights were read")
        return lines

    add("")
    add("-- what the logs say ------------------------------------------------------")
    for label, spread in (
        ("raid size (fight.size)", observation.raid_size),
        ("players listed", observation.players),
        ("kill time (s)", observation.duration),
        ("targets, time-weighted mean", observation.mean_targets),
        ("targets, peak concurrent", observation.peak_targets),
        ("share of fight at peak count", observation.peak_share),
        ("active-time fraction (median player)", observation.uptime),
    ):
        if spread is None:
            add(f"  {label:<38} not measured")
        elif spread.agrees:
            add(f"  {label:<38} {spread.median:g}  (every fight agreed, n={spread.n})")
        else:
            add(
                f"  {label:<38} {spread.median:g}  "
                f"(range {spread.low:g}-{spread.high:g}, n={spread.n})"
            )

    add("")
    add("-- enemies, by share of damage taken --------------------------------------")
    add(f"  {'npc':<32} {'inst':>5} {'first':>7} {'life':>7} {'cadence':>8} {'share':>7}  pull")
    for entry in observation.pooled_adds():
        add(
            f"  {entry['name'][:32]:<32} "
            f"{_med(entry['instances']):>5} "
            f"{_med(entry['firstSeen']):>7} "
            f"{_med(entry['lifetime']):>7} "
            f"{_med(entry['cadence']):>8} "
            f"{_pct(entry['damageShare']):>7}  "
            f"{'yes' if entry['presentAtPull'] else 'no'}"
        )

    phases = observation.pooled_phases()
    add("")
    add("-- phases -----------------------------------------------------------------")
    if not phases:
        add("  none: this encounter has no phase transitions in the API")
    for phase in phases:
        kind = " (intermission)" if phase["isIntermission"] else ""
        add(
            f"  {phase['id']}. {phase['name']}{kind}: starts {_med(phase['start'])}s, "
            f"lasts {_med(phase['duration'])}s, seen in {phase['seenInFights']} fight(s)"
        )

    add("")
    add("-- which enemy the priority target stands for ------------------------------")
    add("   nothing in the API says 'this one is the boss'. These are nominations.")
    for nomination in sorted({fight.priority.evidence for fight in observation.fights}):
        named = sorted(
            {
                fight.priority.name or "nothing"
                for fight in observation.fights
                if fight.priority.evidence == nomination
            }
        )
        add(f"  {', '.join(named)}: {nomination}")

    auras = observation.pooled_auras()
    add("")
    add("-- auras on enemies (seen in 2+ fights) -----------------------------------")
    add("   what an aura DOES is not in the API. These are windows, not magnitudes.")
    if not auras:
        add("  none")
    for aura in auras:
        add(
            f"  {aura['ability'][:34]:<34} id={aura['abilityId']:<8} "
            f"start {_med(aura['start'])}s  lasts {_med(aura['duration'])}s  "
            f"targets={aura['distinctTargets']}  fights={aura['seenInFights']}  "
            # Only the sourced windows can be tested against the raid's actor ids;
            # a column of 0/n means this list is filtered on its target test alone.
            f"sourced={aura.get('sourced', 0)}/{aura['applications']}"
            + ("  [some windows truncated]" if aura["anyTruncated"] else "")
        )
        # The answer to "which of the three does the amplification sit on",
        # printed where somebody reading a probe run will meet it.
        for carrier in aura.get("carriedBy") or []:
            add(
                f"      carried by {carrier['name']} ({carrier['role']}), "
                f"{carrier['applications']} application(s) across "
                f"{carrier['seenInFights']} fight(s)"
            )

    warnings = sorted({w for fight in observation.fights for w in fight.warnings})
    if warnings or any(fight.truncated for fight in observation.fights):
        add("")
        add("-- caveats ----------------------------------------------------------------")
        for warning in warnings:
            add(f"  {warning}")
        if any(fight.truncated for fight in observation.fights):
            add("  at least one event fetch hit its page limit: the tail of that fight is missing")

    if profile is not None:
        add("")
        add("-- profile vs measurement -------------------------------------------------")
        add("   a disagreement here means the extraction is wrong, not that the profile is")
        add(f"  {'fact':<46} {'profile':>12} {'measured':>12}  provenance / note")
        for row in profile.compare_to_measurement(observation):
            add(
                f"  {row['fact'][:46]:<46} {_cell(row['profile']):>12} "
                f"{_cell(row['measured']):>12}  {row['provenance']}; {row['note']}"
            )
        add("")
        add("-- what these measurements could become ------------------------------------")
        add("   nothing is written by this command. `wowdps fight-promote --write` does")
        add("   that, and it never overwrites a fact a person asserted.")
        promotions = fightprofile.plan_promotions(profile, observation)
        if not promotions:
            add("  nothing measured that this profile could take")
        for promotion in promotions:
            add(
                f"  [{'PROMOTE' if promotion.eligible else 'hold   '}] "
                f"{promotion.label}: {promotion.summary}"
            )
            add(f"            {promotion.reason}")

        plan = profile.to_plan()
        add("")
        add("-- the simc scenario this profile produces --------------------------------")
        add(
            f"  desired_targets={plan.targets}  max_time={plan.max_time}  "
            f"(no fight_style, on purpose)"
        )
        for option in plan.options:
            add(f"  {option}")
        for missing in plan.unrepresented:
            add(f"  NOT MODELLED: {missing}")

    return lines


def _med(spread: dict | None) -> str:
    if not spread or spread.get("median") is None:
        return "-"
    return f"{spread['median']:g}"


def _pct(spread: dict | None) -> str:
    if not spread or spread.get("median") is None:
        return "-"
    return f"{spread['median'] * 100:.1f}%"


def _cell(value) -> str:
    return "-" if value is None else f"{value}"


# --------------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------------


def cmd_fight_probe(args: argparse.Namespace) -> int:
    try:
        credentials = Credentials.from_env()
    except WarcraftLogsError as exc:
        log.error("%s", exc)
        return 1

    # argparse's `append` extends a default rather than replacing it, so the
    # default list lives here instead of in add_argument.
    streams = tuple(args.stream or DEFAULT_STREAMS)
    if args.no_damage_events:
        streams = tuple(name for name in streams if name != "damage")
        if "casts" not in streams:
            streams = ("casts", *streams)

    settings = ProbeSettings(
        encounter_ids=tuple(args.encounter or ()),
        difficulty=args.difficulty,
        metric=args.metric,
        reports=args.reports,
        rankings_page=args.page,
        order=args.order,
        rankings_pages=args.rankings_pages,
        streams=streams,
        events_limit=args.events_limit,
        max_pages=args.max_pages,
        point_ceiling=args.point_ceiling,
        significant_share=args.significant_share,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache) if args.cache else out_dir / "cache"

    profiles = fightprofile.load_profiles(
        args.tier, Path(args.profiles_file) if args.profiles_file else None
    )

    observations: list[fightextract.EncounterObservation] = []
    transcript: list[str] = []
    aborted: str | None = None

    with WarcraftLogsClient(credentials, cache_dir=cache_dir) as client:
        before = client.rate_limit()
        log.info(
            "point budget: %s of %s spent this hour, resets in %ss",
            before.get("pointsSpentThisHour"),
            before.get("limitPerHour"),
            before.get("pointsResetIn"),
        )

        try:
            encounter_ids = settings.encounter_ids or _current_zone_encounters(client)
        except WarcraftLogsError as exc:
            log.error("%s", exc)
            return 1

        for encounter_id in encounter_ids:
            log.info("probing encounter %d", encounter_id)
            observation, aborted = probe_encounter(client, encounter_id, settings)
            observations.append(observation)
            transcript.extend(render(observation, profiles.get(encounter_id)))
            transcript.append("")
            if aborted:
                log.warning("%s", aborted)
                break

        after = client.rate_limit()
        ledger = client.ledger.to_json()

    payload = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": args.tier,
        "difficulty": settings.difficulty,
        "metric": settings.metric,
        "reportsPerEncounter": settings.reports,
        "rankingsPage": settings.rankings_page,
        "order": settings.order,
        "eventStreams": list(settings.streams),
        "significantDamageShare": settings.significant_share,
        "sampling": (
            "The earliest kills of the boss, taken by kill date across the gathered "
            "ranking pages -- long kills at the intended tuning, whose timings are "
            "alike, which is what lets an aggregate across them mean something."
            if settings.order == "first"
            else "The top of the encounter's rankings: speed-kill shaped, shorter "
            "than a typical pull and with adds dying faster."
        ),
        "cost": ledger,
        "pointsBefore": before,
        "pointsAfter": after,
        "abortedBecause": aborted,
        "encounters": [observation.to_json() for observation in observations],
    }

    json_path = out_dir / f"fight-probe-{args.tier}.json"
    json_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")

    text = "\n".join(transcript)
    text_path = out_dir / f"fight-probe-{args.tier}.txt"
    text_path.write_text(text + "\n", encoding="utf-8")
    print(text)

    log.info("wrote %s and %s", json_path, text_path)

    if args.publish:
        # The same builder `wowdps fights` uses, fed the payload that was just
        # written. Publishing straight from the pass is what a CI run needs; the
        # offline command exists so the artifact can be re-published, and the
        # extraction argued with, without paying for the queries twice.
        document = fightdataset.build_document(args.tier, profiles, payload)
        published = fightdataset.write_fights(Path(args.publish) / args.tier, document)
        log.info(
            "published %s (%d encounters, %d measured)",
            published,
            document["coverage"]["encounters"],
            document["coverage"]["measured"],
        )

    _report_cost(ledger, len(observations))
    return 0 if not aborted else 2


def _report_cost(ledger: dict, encounters: int) -> None:
    """State what the pass cost, or that the counter refused to say.

    ``pointsSpentThisHour`` did not move at all on the first real run -- first and
    last reading identical, so the delta was exactly 0. Printing "0 points for a
    nine-boss pass" out of that would be the worst kind of wrong: a number that
    reads as a measurement and is actually the absence of one, inviting somebody
    to conclude the API is free. Either the counter lags behind the responses it
    rides on, or the hour had just reset. Both mean *unmeasured*, and the raw
    readings are printed so the next run can tell which.
    """
    spent = ledger.get("pointsSpentThisRun")
    limit = ledger.get("limitPerHour")
    if spent is None:
        log.info("cost: no rate-limit reading came back, so this pass is unmeasured")
        return

    if spent <= 0:
        log.info(
            "cost: UNMEASURED -- the hourly counter did not move (readings %s -> %s of %s). "
            "That is not the same as free; treat the cost of a full pass as unknown "
            "until a run moves it.",
            ledger.get("firstReading"),
            ledger.get("lastReading"),
            limit,
        )
        return

    per_encounter = spent / encounters if encounters else spent
    log.info(
        "cost: %.1f points for %d encounter(s) = %.1f each; a nine-boss pass "
        "would be about %.0f of %s points",
        spent,
        encounters,
        per_encounter,
        per_encounter * 9,
        limit,
    )


def _current_zone_encounters(client: WarcraftLogsClient) -> list[int]:
    """Every encounter in the newest unfrozen zone -- the same choice ``verify`` makes."""
    live = [zone for zone in client.zones() if not zone.get("frozen")]
    if not live:
        raise WarcraftLogsError("no unfrozen zone found; pass --encounter explicitly")
    newest = live[-1]
    log.info("using zone %s", newest.get("name"))
    return [e["id"] for e in newest.get("encounters", []) if isinstance(e.get("id"), int)]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--encounter",
        type=int,
        action="append",
        help="encounter id to probe (repeatable); default is every boss in the newest zone",
    )
    parser.add_argument("--tier", default="MID2", help="tier the fight profiles are read from")
    parser.add_argument("--difficulty", type=int, default=5, help="5 = Mythic, 4 = Heroic")
    parser.add_argument("--metric", default="dps", help="ranking metric used to pick logs")
    parser.add_argument(
        "--reports",
        type=int,
        default=30,
        help="how many distinct kills to read per encounter (default 30); each one "
        "costs several event queries, so this is the main cost dial. A larger sample "
        "is what makes the aggregate 'how many targets up when' band mean something",
    )
    parser.add_argument(
        "--order",
        choices=("first", "top"),
        default="first",
        help="which kills to sample: 'first' (the earliest kills, alike and at the "
        "intended tuning -- the default) or 'top' (the rankings' damage order, i.e. "
        "speed kills)",
    )
    parser.add_argument(
        "--rankings-pages",
        type=int,
        default=8,
        help="how many ranking pages to gather before choosing the first kills; WCL "
        "sorts by damage not date, so the earliest kills sit deep in the list",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="first rankings page to gather from (default 1)",
    )
    parser.add_argument(
        "--stream",
        action="append",
        default=None,
        choices=sorted(EVENT_STREAMS),
        help="event stream to fetch (repeatable); default is damage, deaths, buffs, debuffs",
    )
    parser.add_argument(
        "--no-damage-events",
        action="store_true",
        help="use enemy casts instead of enemy damage-taken as the presence signal: "
        "far cheaper, and blind to adds nobody logged a cast for",
    )
    parser.add_argument(
        "--events-limit", type=int, default=10000, help="events per page (100-10000)"
    )
    parser.add_argument("--max-pages", type=int, default=3, help="pages per event stream per fight")
    parser.add_argument(
        "--point-ceiling",
        type=float,
        default=DEFAULT_POINT_CEILING,
        help="abort once this fraction of the hourly point budget is spent",
    )
    parser.add_argument(
        "--significant-share",
        type=float,
        default=fightextract.DEFAULT_SIGNIFICANT_SHARE,
        help="an enemy under this share of the fight's damage is present but not a target",
    )
    parser.add_argument("--out", default="fight-probe", help="output directory")
    parser.add_argument(
        "--cache",
        help="response cache directory (default <out>/cache); a warm cache costs no points",
    )
    parser.add_argument("--profiles-file", help="alternative fight profile file")
    parser.add_argument(
        "--publish",
        help="also write <DIR>/<tier>/fights.json, the dataset the site's Fights view "
        "draws; omit to leave the run as an artifact and publish later with "
        "`wowdps fights --probe`",
    )
    parser.set_defaults(func=cmd_fight_probe)
