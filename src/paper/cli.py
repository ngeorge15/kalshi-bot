"""Credential-free CLI for replay and forward paper observations."""
import argparse
import json
from pathlib import Path
import sys

from src.paper.broker import PaperBroker
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
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Paper command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
