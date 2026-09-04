"""Seed the database with a deterministic simulated week of paper trading.

Produces linked ``predictions`` -> ``trades`` -> ``outcomes`` rows plus a
``price_observations`` series per ticker, with *known* biases planted per market
type so tests can assert that the analytics layer actually recovers them.

The planted patterns are not hand-written P&L numbers.  Each trade's outcome is
drawn against a hidden ``true_p``, and its P&L follows arithmetically from the
fill price and that outcome.  A market type loses money because its model is
miscalibrated, exactly as it would in production -- so metrics computed from the
seeded rows are internally consistent rather than merely plausible.

Ground truth (see :data:`MARKET_PROFILES`):

===============  ==========  ================================================
market_type      Domain      Planted pattern
===============  ==========  ================================================
games            NBA         Well calibrated, mildly profitable
props            NBA         Overconfident by 8% -- eats its own edge
temperature      Weather     Well calibrated, profitable
precipitation    Weather     Overconfident by 15% -- consistently losing
cbb_games        (third)     Small sample; proves nothing assumes {nba, weather}
===============  ==========  ================================================

Usage:
    python -m scripts.seed_test_data --db data/test_seed.db --days 7 --seed 42

    from scripts.seed_test_data import seed_database
    manifest = seed_database(db, n_days=7, seed=42)
"""

import argparse
import logging
import random
import sys
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Fraction of tickers whose edge persists to settlement rather than decaying.
# 05-03's edge-decay analysis needs both populations present to be meaningful.
PERSISTENT_EDGE_FRACTION = 0.25

# Hours-to-close at which each ticker's price series is sampled.
OBSERVATION_HOURS = (72.0, 48.0, 24.0, 12.0, 6.0, 2.0, 0.5)


class MarketProfile:
    """Ground-truth generation parameters for one market type.

    Args:
        market_type: Free-form market type tag stored on every row.  Deliberately
            not validated against an enum -- see D5-07.
        model_name: Name recorded on ``predictions.model_name``.
        ticker_fmt: ``str.format`` template taking ``date`` and ``n``.
        calibration_bias: Amount added to the hidden true probability to produce
            the model's stated probability.  Positive means overconfident.
        weight: Relative share of generated trades.
    """

    def __init__(
        self,
        market_type: str,
        model_name: str,
        ticker_fmt: str,
        calibration_bias: float,
        weight: float,
    ) -> None:
        self.market_type = market_type
        self.model_name = model_name
        self.ticker_fmt = ticker_fmt
        self.calibration_bias = calibration_bias
        self.weight = weight


MARKET_PROFILES: tuple[MarketProfile, ...] = (
    MarketProfile("games", "nba_game", "KXNBAGAME-{date}-{n:03d}", 0.00, 0.30),
    MarketProfile("props", "nba_props", "KXNBAPTS-{date}-{n:03d}", 0.08, 0.25),
    MarketProfile("temperature", "weather_temp", "KXHIGHNY-{date}-{n:03d}", 0.00, 0.20),
    MarketProfile("precipitation", "weather_precip", "KXRAINCHI-{date}-{n:03d}", 0.15, 0.15),
    MarketProfile("cbb_games", "cbb_game", "KXCBBGAME-{date}-{n:03d}", 0.00, 0.10),
)


def _clip(value: float, low: float, high: float) -> float:
    """Clamp *value* into ``[low, high]``."""
    return max(low, min(high, value))


def _pick_profile(rng: random.Random) -> MarketProfile:
    """Draw a market profile according to its weight."""
    roll = rng.random() * sum(p.weight for p in MARKET_PROFILES)
    cumulative = 0.0
    for profile in MARKET_PROFILES:
        cumulative += profile.weight
        if roll <= cumulative:
            return profile
    return MARKET_PROFILES[-1]


def _table_is_populated(db, table: str) -> bool:
    """Return True if *table* already holds at least one row."""
    row = db.fetchone(f"SELECT COUNT(*) AS cnt FROM {table}")
    return bool(row and row["cnt"] > 0)


def _seed_price_observations(
    conn,
    rng: random.Random,
    ticker: str,
    market_type: str,
    model_prob: float,
    entry_price_cents: int,
    close_at: datetime,
) -> int:
    """Write one ticker's price series, decaying or persisting toward close.

    A decaying ticker's market price drifts toward the model's probability, so
    the edge shrinks to roughly zero by settlement.  A persistent ticker holds
    its mispricing.

    Returns:
        Number of observation rows written.
    """
    persists = rng.random() < PERSISTENT_EDGE_FRACTION
    model_price_cents = model_prob * 100.0
    initial_edge = model_price_cents - entry_price_cents

    written = 0
    for hours in OBSERVATION_HOURS:
        # Decay factor goes 1.0 -> 0.0 as hours_to_close goes 72 -> 0.
        decay = hours / OBSERVATION_HOURS[0]
        remaining = initial_edge if persists else initial_edge * decay
        # Small symmetric noise so the series is not a perfect analytic curve.
        remaining += rng.uniform(-1.0, 1.0)

        mid = _clip(model_price_cents - remaining, 1.0, 99.0)
        spread = rng.uniform(1.0, 3.0)
        bid = int(round(_clip(mid - spread / 2.0, 1.0, 99.0)))
        ask = int(round(_clip(mid + spread / 2.0, 1.0, 99.0)))
        observed_at = close_at - timedelta(hours=hours)

        conn.execute(
            """INSERT INTO price_observations
            (ticker, market_type, observed_at, yes_bid_cents, yes_ask_cents,
             mid_price_cents, model_prob, edge_cents, hours_to_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                market_type,
                observed_at.isoformat(),
                bid,
                ask,
                int(round(mid)),
                model_prob,
                int(round(model_price_cents - mid)),
                hours,
            ),
        )
        written += 1
    return written


def seed_database(
    db,
    n_days: int = 7,
    seed: int = 42,
    trades_per_day: int = 12,
) -> dict:
    """Seed *db* with a simulated week of paper trading.

    Uses only a local :class:`random.Random` instance -- never the global RNG or
    ``numpy.random`` -- so a given ``seed`` reproduces the database exactly.

    Args:
        db: :class:`~src.db.database.Database` instance.
        n_days: Number of days to spread trades across, ending today (UTC).
        seed: RNG seed.
        trades_per_day: Trades generated per simulated day.

    Returns:
        A ground-truth manifest: total counts plus, per market type, the planted
        ``calibration_bias``, the realised outcome frequency, the mean predicted
        probability, and total P&L in cents.  Tests assert against this.
    """
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    per_type: dict[str, dict] = {
        p.market_type: {
            "market_type": p.market_type,
            "model_name": p.model_name,
            "calibration_bias": p.calibration_bias,
            "n_trades": 0,
            "sum_predicted_prob": 0.0,
            "n_yes_outcomes": 0,
            "pnl_cents": 0,
        }
        for p in MARKET_PROFILES
    }

    n_observations = 0
    counter = 0

    with db.transaction() as conn:
        for day_offset in range(n_days - 1, -1, -1):
            day = now - timedelta(days=day_offset)
            date_tag = day.strftime("%y%b%d").upper()

            for _ in range(trades_per_day):
                counter += 1
                profile = _pick_profile(rng)

                # Hidden truth the outcome is actually drawn from.
                true_p = rng.uniform(0.25, 0.85)
                predicted_prob = _clip(true_p + profile.calibration_bias, 0.02, 0.98)

                # We only trade where we perceive an edge, so the fill sits below our
                # stated probability by that many cents.
                edge_cents = rng.randint(3, 9)
                market_price_cents = int(round(_clip(predicted_prob * 100 - edge_cents, 1, 99)))

                ticker = profile.ticker_fmt.format(date=date_tag, n=counter)
                quantity = float(rng.randint(5, 25))

                created_at = (day - timedelta(hours=rng.uniform(3.0, 10.0))).isoformat()
                settled_at = day.isoformat()

                prediction_id = conn.execute(
                    """INSERT INTO predictions
                    (ticker, market_type, model_name, model_version, predicted_prob,
                     market_price_cents, edge_cents, features_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ticker,
                        profile.market_type,
                        profile.model_name,
                        1,
                        predicted_prob,
                        market_price_cents,
                        edge_cents,
                        "",
                        created_at,
                    ),
                ).lastrowid

                trade_id = conn.execute(
                    """INSERT INTO trades
                    (order_id, ticker, market_type, side, action, price_cents, quantity,
                     status, model_prob, market_price_cents, edge_cents, confidence,
                     created_at, filled_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"SEED-{counter:05d}",
                        ticker,
                        profile.market_type,
                        "yes",
                        "buy",
                        market_price_cents,
                        quantity,
                        "executed",
                        predicted_prob,
                        market_price_cents,
                        edge_cents,
                        "high" if edge_cents >= 7 else "medium",
                        created_at,
                        created_at,
                        "seeded",
                    ),
                ).lastrowid

                # Outcome drawn against the hidden truth, not the stated probability.
                hit = rng.random() < true_p
                result = "yes" if hit else "no"
                settlement_price_cents = 100 if hit else 0
                # Long YES: payoff is 100 on a win, 0 on a loss, against the fill price.
                pnl_cents = int(round((settlement_price_cents - market_price_cents) * quantity))

                conn.execute(
                    """INSERT INTO outcomes
                    (ticker, market_type, result, settlement_price_cents, trade_id,
                     prediction_id, pnl_cents, settled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ticker,
                        profile.market_type,
                        result,
                        settlement_price_cents,
                        trade_id,
                        prediction_id,
                        pnl_cents,
                        settled_at,
                    ),
                )

                n_observations += _seed_price_observations(
                    conn,
                    rng,
                    ticker,
                    profile.market_type,
                    predicted_prob,
                    market_price_cents,
                    day,
                )

                bucket = per_type[profile.market_type]
                bucket["n_trades"] += 1
                bucket["sum_predicted_prob"] += predicted_prob
                bucket["n_yes_outcomes"] += int(hit)
                bucket["pnl_cents"] += pnl_cents

    total_trades = sum(b["n_trades"] for b in per_type.values())
    for bucket in per_type.values():
        n = bucket["n_trades"]
        bucket["mean_predicted_prob"] = bucket["sum_predicted_prob"] / n if n else 0.0
        bucket["actual_frequency"] = bucket["n_yes_outcomes"] / n if n else 0.0
        del bucket["sum_predicted_prob"]

    manifest = {
        "seed": seed,
        "n_days": n_days,
        "total_trades": total_trades,
        "total_predictions": total_trades,
        "total_outcomes": total_trades,
        "total_price_observations": n_observations,
        "market_types": sorted(per_type),
        "by_market_type": per_type,
    }
    logger.info(
        "Seeded %d trades across %d market types (%d price observations)",
        total_trades,
        len(per_type),
        n_observations,
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_test_data",
        description="Seed a deterministic simulated week of paper trading.",
    )
    parser.add_argument("--db", default="data/test_seed.db", help="SQLite path")
    parser.add_argument("--days", type=int, default=7, help="Days to simulate")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--trades-per-day", type=int, default=12)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Seed even if the trades table already holds rows",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from src.db.database import Database

    db = Database(args.db)

    if _table_is_populated(db, "trades") and not args.force:
        logger.error(
            "%s already contains trades. Refusing to seed without --force.",
            args.db,
        )
        sys.exit(1)

    manifest = seed_database(
        db,
        n_days=args.days,
        seed=args.seed,
        trades_per_day=args.trades_per_day,
    )

    print(f"\nSeeded {args.db}")
    print(f"  trades:            {manifest['total_trades']}")
    print(f"  price observations:{manifest['total_price_observations']:>4}")
    print()
    print(f"{'market_type':<16}{'n':>5}{'bias':>8}{'pred':>8}{'actual':>8}{'pnl($)':>10}")
    print("-" * 55)
    for mt in manifest["market_types"]:
        b = manifest["by_market_type"][mt]
        print(
            f"{mt:<16}{b['n_trades']:>5}{b['calibration_bias']:>8.2f}"
            f"{b['mean_predicted_prob']:>8.3f}{b['actual_frequency']:>8.3f}"
            f"{b['pnl_cents'] / 100:>10.2f}"
        )


if __name__ == "__main__":
    main()
