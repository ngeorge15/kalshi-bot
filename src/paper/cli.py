"""Credential-free CLI for replay and forward paper observations."""
import argparse
import dataclasses
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from src.paper.broker import PaperBroker, utc
from src.paper.config import PaperConfig


DEFAULT_DB = "data/paper/paper.db"


def _now() -> datetime:
    """Current UTC time. A thin seam so tests can inject a fixed instant."""
    return datetime.now(timezone.utc)


def _parse_bracket(raw: str) -> tuple[float | None, float | None]:
    """Parse a `--bracket LOW:HIGH` value into `(lower_bound_f, upper_bound_f)`.

    Either side may be empty for an open tail (``":69.5"``, ``"69.5:72.5"``,
    ``"72.5:"``). A negative bound must be passed as ``--bracket=-5:0``:
    argparse otherwise treats the leading ``-`` as the start of another
    option.

    Raises:
        ValueError: If `raw` is not exactly one `:`-separated pair of numbers
            or blanks.
    """
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"--bracket must be LOW:HIGH (either side may be empty), got {raw!r}")

    def _side(text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"--bracket bound must be a number or empty, got {text!r} in {raw!r}") from None

    return _side(parts[0]), _side(parts[1])


def _watchlist_scaffold_command(args) -> int:
    """Handle `watchlist-scaffold`. Offline: never touches the experiment database."""
    from src.paper.watchlist import scaffold

    brackets = [_parse_bracket(raw) for raw in args.bracket]
    entries = scaffold(args.station, args.date, brackets, _now(), utc_offset_hours=args.utc_offset_hours)
    rendered = json.dumps(entries, indent=2, allow_nan=False)
    if args.output:
        output = Path(args.output)
        # Do not accidentally overwrite the experiment database.
        if output.resolve() == Path(args.db).resolve():
            raise ValueError("Watchlist output cannot overwrite the paper database")
        if output.exists() and not args.force:
            raise ValueError(f"{output} already exists; pass --force to overwrite")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n")
    print(rendered)
    return 0


def _watchlist_validate_command(args) -> int:
    """Handle `watchlist-validate`. Offline: never touches the experiment database."""
    from src.paper.watchlist import bracket_coverage, validate

    watchlist = json.loads(Path(args.path).read_text())
    result = validate(watchlist, _now())
    payload = {
        "ok": result.ok,
        "errors": [dataclasses.asdict(finding) for finding in result.errors],
        "warnings": [dataclasses.asdict(finding) for finding in result.warnings],
        "coverage": bracket_coverage(watchlist) if isinstance(watchlist, list) else {},
    }
    print(json.dumps(payload, indent=2, allow_nan=False))
    return 0 if result.ok else 2


def _watchlist_explain_command(args) -> int:
    """Handle `watchlist-explain`. Offline: never touches the experiment database."""
    from src.paper.watchlist import explain

    watchlist = json.loads(Path(args.path).read_text())
    print(explain(watchlist))
    return 0


# Offline watchlist-authoring subcommands. They never construct a PaperBroker
# or require an initialized experiment database -- see their dispatch in
# main(), which runs before the "database must exist" check below.
_WATCHLIST_DISPATCH = {
    "watchlist-scaffold": _watchlist_scaffold_command,
    "watchlist-validate": _watchlist_validate_command,
    "watchlist-explain": _watchlist_explain_command,
}


def replay(broker: PaperBroker, path: str) -> dict:
    """Apply JSONL events in file order; reruns resume by stable event IDs."""
    counts: dict[str, int] = {}
    with open(path) as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Each event must be a JSON object")
                result = broker.process(event)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            status = result.get("status", "settled")
            counts[status] = counts.get(status, 0) + 1
    return {"processed_or_resumed": counts, "report": broker.report()}



def _protocol_command(args, broker: PaperBroker) -> dict:
    """Run one predeclaration subcommand.

    Evaluation uses the lower bound of the event-clustered interval as the
    primary metric, not the point estimate: a predeclared threshold should be
    cleared by the conservative end of the interval, otherwise a wide interval
    around a flattering point estimate would pass.

    Returns:
        A JSON-serialisable dict for printing.

    Raises:
        ValueError: If the named model has no scored markets, or the interval
            cannot be estimated because fewer than two events have settled.
    """
    from src.paper import protocol as protocol_module

    if args.command == "protocol-declare":
        declared = protocol_module.declare(**json.loads(Path(args.config).read_text()))
        protocol_module.save(declared, args.output)
        return {"declared": True, "path": str(args.output), "content_hash": declared.content_hash}

    saved = protocol_module.load(args.protocol)
    if args.command == "protocol-verify":
        checked = protocol_module.verify(saved, args.protocol)
        return {"valid": checked.valid, "message": checked.message,
                "mismatched_fields": list(checked.mismatched_fields)}

    scores = [s for s in broker.report()["scores"]
              if s["model_name"] == args.model and str(s["model_version"]) == str(args.model_version)]
    if not scores:
        raise ValueError(f"No scored markets for {args.model} version {args.model_version}")
    clustered = scores[0]["event_clustered"]
    if clustered["ci_low"] is None:
        raise ValueError(
            f"Cannot evaluate: {clustered['n_events']} settled event(s) is too few to estimate an "
            "interval. Collect more independent events before drawing a conclusion.")
    verdict = protocol_module.evaluate(saved, protocol_module.Results(
        n_events=clustered["n_events"], primary_metric_value=clustered["ci_low"],
        secondary_metric_values={"point_estimate": clustered["brier_improvement"]}))
    return {"state": verdict.state.value, "explanation": verdict.explanation,
            "protocol_hash": verdict.protocol_hash, "n_events": verdict.n_events,
            "primary_metric_value": verdict.primary_metric_value,
            "decision_threshold": verdict.decision_threshold,
            "secondary_metric_values": verdict.secondary_metric_values}


def main(argv: list[str] | None = None) -> int:
    """Run a local paper command; there is intentionally no live mode."""
    parser = argparse.ArgumentParser(description="Local paper trading. Never sends exchange orders.")
    parser.add_argument("--db", default=DEFAULT_DB, help="Separate paper SQLite file")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an experiment with immutable settings")
    init.add_argument("--config", help="PaperConfig fields in a JSON file")
    run = commands.add_parser("replay", help="Apply a JSONL tape (restart-safe)")
    run.add_argument("events")
    commands.add_parser("event", help="Apply one JSON event from stdin")
    observe = commands.add_parser("observe", help="One pass of read-only public weather observations")
    observe.add_argument("watchlist", help="Reviewed market specifications and availability in JSON")
    observe.add_argument("--scheduled", action="store_true",
                         help="Only observe tickers due since the last attempt, and journal the pass")
    observe.add_argument("--interval-seconds", type=int, default=3600,
                         help="Minimum gap between attempts on the same ticker")
    coverage = commands.add_parser("coverage", help="Report collection coverage and gaps")
    coverage.add_argument("watchlist")
    coverage.add_argument("--interval-seconds", type=int, default=3600)
    report = commands.add_parser("report", help="Print accounting and paired Brier scores")
    report.add_argument("--output", help="Also save the JSON report")
    health = commands.add_parser("health", help="Report input-data health for the experiment")
    health.add_argument("--watchlist", help="Reviewed market specifications, to check eligibility")
    declare = commands.add_parser("protocol-declare", help="Predeclare an evaluation protocol before collecting data")
    declare.add_argument("--config", required=True, help="Protocol fields in a JSON file")
    declare.add_argument("--output", required=True, help="Where to save the declared protocol")
    check = commands.add_parser("protocol-verify", help="Check a saved protocol was not edited after declaration")
    check.add_argument("protocol")
    assess = commands.add_parser("protocol-evaluate", help="Compare results against a predeclared protocol")
    assess.add_argument("protocol")
    assess.add_argument("--model", required=True, help="model_name to evaluate")
    assess.add_argument("--model-version", required=True, help="model_version to evaluate")

    scaffold_cmd = commands.add_parser(
        "watchlist-scaffold",
        help="Generate placeholder watchlist entries for one station/date (offline, no experiment needed)")
    scaffold_cmd.add_argument("station", help="Station code, e.g. KNYC")
    scaffold_cmd.add_argument("date", help="Local target date, ISO 8601 (YYYY-MM-DD)")
    scaffold_cmd.add_argument(
        "--bracket", action="append", required=True, dest="bracket", metavar="LOW:HIGH",
        help="Bracket bounds in Fahrenheit, e.g. '69.5:72.5'. Either side may be empty for an open "
             "tail (':69.5' or '72.5:'). Repeat --bracket for multiple brackets. A negative bound "
             "must be passed as --bracket=-5:0, or argparse treats it as another option.")
    scaffold_cmd.add_argument("--utc-offset-hours", type=int, default=None,
                              help="Override the station's default local-standard UTC offset")
    scaffold_cmd.add_argument("--output", help="Also write the JSON result to this path")
    scaffold_cmd.add_argument("--force", action="store_true", help="Overwrite --output if it already exists")

    validate_cmd = commands.add_parser(
        "watchlist-validate",
        help="Validate a watchlist JSON file's structure and consistency (offline, no experiment needed)")
    validate_cmd.add_argument("path")

    explain_cmd = commands.add_parser(
        "watchlist-explain",
        help="Print a human-readable watchlist summary for review (offline, no experiment needed)")
    explain_cmd.add_argument("path")

    args = parser.parse_args(argv)
    try:
        if args.command in _WATCHLIST_DISPATCH:
            # Offline authoring tools: dispatched before the database-exists
            # check below, and they never construct a PaperBroker.
            return _WATCHLIST_DISPATCH[args.command](args)
        if args.command != "init" and not Path(args.db).is_file():
            raise ValueError("Initialize the paper experiment first with 'init'")
        config = None
        if args.command == "init":
            settings = json.loads(Path(args.config).read_text()) if args.config else {}
            if "allowed_market_types" in settings:
                settings["allowed_market_types"] = tuple(settings["allowed_market_types"])
            config = PaperConfig(**settings)
        broker = PaperBroker(args.db, config)
        if args.command == "replay":
            if broker.config.run_kind == "forward":
                raise ValueError("Use 'event' for forward observations; replay is not forward evidence")
            result = replay(broker, args.events)
        elif args.command == "observe":
            watchlist = json.loads(Path(args.watchlist).read_text())
            if args.scheduled:
                from src.paper.schedule import run_scheduled_pass
                from datetime import datetime, timezone
                result = run_scheduled_pass(broker, watchlist, datetime.now(timezone.utc),
                                            interval_seconds=args.interval_seconds)
            else:
                from src.paper.observe import observe_once
                result = observe_once(broker, watchlist)
        elif args.command == "coverage":
            from src.paper.schedule import coverage_report
            from datetime import datetime, timezone
            result = coverage_report(broker.db_path, json.loads(Path(args.watchlist).read_text()),
                                     datetime.now(timezone.utc), args.interval_seconds)
        elif args.command == "event":
            result = broker.process(json.load(sys.stdin))
        elif args.command == "health":
            from src.paper.health import health_report
            watchlist = json.loads(Path(args.watchlist).read_text()) if args.watchlist else []
            # The experiment clock, not wall time, so health is reproducible.
            report_at = broker.report()["last_event_at"]
            if not report_at:
                raise ValueError("No events processed yet; there is nothing to check")
            result = health_report(broker.db_path, watchlist, utc(report_at),
                                   broker.config.max_quote_age_seconds,
                                   broker.config.max_forecast_age_seconds)
        elif args.command in ("protocol-declare", "protocol-verify", "protocol-evaluate"):
            result = _protocol_command(args, broker)
        else:
            result = broker.report()
        rendered = json.dumps(result, indent=2, allow_nan=False)
        if args.command == "report" and args.output:
            output = Path(args.output)
            # Do not accidentally overwrite the experiment database.
            if output.resolve() == Path(args.db).resolve():
                raise ValueError("Report output cannot overwrite the paper database")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n")
        print(rendered)
        observed = result.get("results", []) if isinstance(result, dict) else result
        if args.command == "observe" and any(item["status"] == "error" for item in observed):
            return 2
        if args.command == "protocol-verify" and not result["valid"]:
            return 2
        if args.command == "protocol-evaluate" and result["state"] == "invalid_protocol":
            return 2
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Paper command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
