"""Credential-free CLI for replay and forward paper observations."""
import argparse
import json
from pathlib import Path
import sys

from src.paper.broker import PaperBroker, utc
from src.paper.config import PaperConfig


DEFAULT_DB = "data/paper/paper.db"


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
    args = parser.parse_args(argv)
    try:
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
            from src.paper.observe import observe_once
            result = observe_once(broker, json.loads(Path(args.watchlist).read_text()))
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
        if args.command == "observe" and any(item["status"] == "error" for item in result):
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
