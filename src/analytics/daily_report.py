"""Daily performance report CLI (R10.8).

Prints today's trades, P&L, model accuracy and the per-market-type breakdown.
Presentation only -- every number comes from :mod:`src.analytics.performance`,
:mod:`src.analytics.edge_decay` and :mod:`src.analytics.snapshot`, so the report
and the evaluator can never disagree about what happened.

Market types are rendered in whatever order the database yields them; nothing
here knows which sports exist (D5-07).

Usage:
    python -m src.analytics.daily_report
    python -m src.analytics.daily_report --date 2026-09-01
    python -m src.analytics.daily_report --snapshot data/reports/snapshot.json
    python -m src.analytics.daily_report --plot data/reports/calibration.png
"""

import argparse
import logging
from datetime import datetime, timezone

from src.analytics import edge_decay, performance, snapshot

logger = logging.getLogger(__name__)

# Width of the rendered report, in characters.
WIDTH = 74


def _cents(value: int | float | None) -> str:
    """Format integer cents as a signed dollar string."""
    if value is None:
        return "n/a"
    return f"${value / 100:+,.2f}"


def _num(value: float | None, places: int = 4) -> str:
    """Format an optional float, rendering None as 'n/a' rather than 'None'."""
    return "n/a" if value is None else f"{value:.{places}f}"


def _pct(value: float | None) -> str:
    """Format an optional 0-1 fraction as a percentage."""
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _rule(char: str = "-") -> str:
    return char * WIDTH


def build_report(db, date_str: str | None = None) -> str:
    """Build the daily report as text (R10.8).

    Separated from printing so tests can assert on content and callers can route
    it somewhere other than stdout.

    Args:
        db: :class:`~src.db.database.Database` instance.
        date_str: ISO date ``YYYY-MM-DD``. Defaults to today (UTC).

    Returns:
        The rendered report.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Rebuild every day, not just the requested one: the ALL TIME section's
    # Sharpe ratio reads the whole daily_pnl series, and rebuilding a single row
    # would leave it with too few points to compute and silently report n/a.
    performance.recompute_all_daily_pnl(db)
    day = performance.recompute_daily_pnl(db, date_str)

    lines: list[str] = [
        _rule("="),
        f"KALSHI BOT — DAILY REPORT — {date_str}".center(WIDTH),
        _rule("="),
        "",
        "TODAY",
        _rule(),
        f"  Trades settled     {day['total_trades']}",
        f"  Won / lost         {day['winning_trades']} / {day['losing_trades']}",
        f"  Realized P&L       {_cents(day['realized_pnl_cents'])}",
        "",
    ]

    overall = performance.brier_summary(db)
    wins = performance.win_rate(db)
    lines += [
        "ALL TIME",
        _rule(),
        f"  Settled trades     {overall['n']}",
        f"  Cumulative P&L     {_cents(wins['total_pnl_cents'])}",
        f"  Win rate           {_pct(wins['win_rate'])}",
        f"  Brier score        {_num(overall['brier'])}",
        f"  Accuracy           {_pct(overall['accuracy'])}",
        f"  Calibration error  {_num(overall['calibration_error'])}",
        f"  Calibration drift  {_num(overall['calibration_drift'])}"
        "   (+ = overconfident)",
        f"  Sharpe (daily)     {_num(performance.sharpe_ratio(db), 2)}",
        f"  Max drawdown       {_cents(performance.drawdown(db)['max_drawdown_cents'])}",
        "",
    ]

    rolling = performance.rolling_brier(db)
    lines += ["ROLLING BRIER", _rule()]
    for key, stats in rolling.items():
        label = key.replace("_", " ")
        lines.append(
            f"  {label:<18} {_num(stats['brier'])}   (n={stats['n']})"
        )
    lines.append("")

    lines += [
        "BY MARKET TYPE",
        _rule(),
        f"  {'market type':<16}{'n':>5}{'brier':>9}{'drift':>9}{'win%':>8}{'P&L':>13}",
    ]
    brier_by_type = performance.brier_by_market_type(db)
    pnl_by_type = performance.pnl_by_market_type(db)
    for market_type in sorted(brier_by_type):
        b = brier_by_type[market_type]
        p = pnl_by_type[market_type]
        lines.append(
            f"  {market_type:<16}{b['n']:>5}{_num(b['brier']):>9}"
            f"{_num(b['calibration_drift'], 3):>9}{_pct(p['win_rate']):>8}"
            f"{_cents(p['total_pnl_cents']):>13}"
        )
    if not brier_by_type:
        lines.append("  (no settled trades yet)")
    lines.append("")

    by_model = performance.brier_by_model(db)
    if by_model:
        lines += [
            "BY MODEL",
            _rule(),
            f"  {'model':<22}{'n':>5}{'brier':>9}{'accuracy':>11}",
        ]
        for model_name in sorted(by_model):
            m = by_model[model_name]
            lines.append(
                f"  {model_name:<22}{m['n']:>5}{_num(m['brier']):>9}"
                f"{_pct(m['accuracy']):>11}"
            )
        lines.append("")

    buckets = snapshot.win_rate_by_edge_bucket(db)
    if any(b["n"] for b in buckets):
        lines += [
            "WIN RATE BY CLAIMED EDGE",
            _rule(),
            f"  {'edge':<10}{'n':>6}{'win%':>9}{'P&L':>13}",
        ]
        for b in buckets:
            lines.append(
                f"  {b['label']:<10}{b['n']:>6}{_pct(b['win_rate']):>9}"
                f"{_cents(b['total_pnl_cents']):>13}"
            )
        lines.append("")

    persistence = edge_decay.edge_persistence(db)
    if persistence["n_tickers"]:
        lines += [
            "EDGE DECAY",
            _rule(),
            f"  Markets tracked    {persistence['n_tickers']}",
            f"  Edge persisted     {_pct(persistence['persistence_rate'])}",
            f"  Mean edge retained {_pct(persistence['mean_retained_fraction'])}",
            "",
        ]

    lines.append(_rule("="))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.analytics.daily_report",
        description="Daily performance summary with per-market-type breakdown (R10.8).",
    )
    parser.add_argument("--db", default="data/kalshi_bot.db", help="SQLite path")
    parser.add_argument("--date", help="ISO date YYYY-MM-DD (default: today UTC)")
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const="data/reports/snapshot.json",
        metavar="PATH",
        help="Also write the evaluator's JSON snapshot",
    )
    parser.add_argument(
        "--plot",
        nargs="?",
        const="data/reports/calibration.png",
        metavar="PATH",
        help="Also write the reliability diagram PNG",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    from src.db.database import Database

    db = Database(args.db)
    print(build_report(db, args.date))

    if args.snapshot:
        path = snapshot.write_snapshot(db, args.snapshot)
        print(f"\nSnapshot written to {path}")

    if args.plot:
        path = performance.render_reliability_diagram(
            performance.calibration_bins(db), args.plot
        )
        print(f"Reliability diagram written to {path}")


if __name__ == "__main__":
    main()
