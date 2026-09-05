"""Performance snapshot: the evaluator's data contract (R10.7).

This is the single structure Phase 6 reads to diagnose the bot (R11.3), so its
shape is an interface, not an implementation detail.  Two consequences:

1. **It is versioned.**  ``schema_version`` lets the evaluator's prompt and
   parsing evolve without silently misreading an older snapshot.
2. **It never enumerates market types or models.**  Every breakdown is a
   dictionary keyed by whatever is actually in the database (D5-07), so a new
   sport appears in the evaluator's view with no change here or in Phase 6.

R12.4 caps the evaluator's context at roughly 3000 tokens and forbids raw data
dumps, so the snapshot carries aggregates and a short trade tail -- never the
full ledger.

Usage:
    from src.analytics.snapshot import generate_snapshot, write_snapshot

    snap = generate_snapshot(db)
    path = write_snapshot(db, "data/reports/snapshot.json")
"""

import json
import logging
import os
from datetime import datetime, timezone

from src.analytics import edge_decay, performance

logger = logging.getLogger(__name__)

# Bump when the snapshot's shape changes in a way the evaluator must notice.
SNAPSHOT_SCHEMA_VERSION = 1

# Number of most recent settled trades included verbatim (R12.2 asks for the
# last 20; kept small to respect R12.4's context budget).
RECENT_TRADES = 20

# Edge buckets, in cents, for win-rate-by-edge (R11.3).
EDGE_BUCKETS: tuple[tuple[int, int | None], ...] = (
    (0, 3),
    (3, 5),
    (5, 8),
    (8, 12),
    (12, None),
)


def win_rate_by_edge_bucket(db, market_type: str | None = None) -> list[dict]:
    """Win rate grouped by the edge claimed at entry (R11.3).

    The shape the evaluator needs to spot the classic failure: large claimed
    edges performing *worse* than small ones, which means big edges are model
    error rather than opportunity.

    Returns:
        One dict per bucket with ``label``, ``edge_lower``, ``edge_upper``, ``n``,
        ``wins``, ``losses``, ``win_rate`` and ``total_pnl_cents``. Empty buckets
        report ``n`` 0 and a ``None`` win rate.
    """
    rows = performance._settled_predictions(db, market_type=market_type)
    out: list[dict] = []

    for lower, upper in EDGE_BUCKETS:
        if upper is None:
            selected = [r for r in rows if r["edge_cents"] is not None and r["edge_cents"] >= lower]
            label = f"{lower}c+"
        else:
            selected = [
                r for r in rows
                if r["edge_cents"] is not None and lower <= r["edge_cents"] < upper
            ]
            label = f"{lower}-{upper}c"

        wins = sum(1 for r in selected if r["pnl_cents"] > 0)
        losses = sum(1 for r in selected if r["pnl_cents"] < 0)
        decided = wins + losses
        out.append(
            {
                "label": label,
                "edge_lower": lower,
                "edge_upper": upper,
                "n": len(selected),
                "wins": wins,
                "losses": losses,
                "win_rate": (wins / decided) if decided else None,
                "total_pnl_cents": sum(r["pnl_cents"] for r in selected),
            }
        )
    return out


def recent_trades(db, limit: int = RECENT_TRADES) -> list[dict]:
    """The most recent settled trades, newest first (R12.2).

    Deliberately compact -- the evaluator gets enough to sanity-check the
    aggregates without a raw ledger dump (R12.4).
    """
    rows = performance._settled_predictions(db)
    tail = rows[-limit:][::-1]
    return [
        {
            "ticker": r["ticker"],
            "market_type": r["market_type"],
            "model_name": r["model_name"],
            "predicted_prob": round(r["predicted_prob"], 4),
            "result": "yes" if r["label"] == 1 else "no",
            "edge_cents": r["edge_cents"],
            "pnl_cents": r["pnl_cents"],
            "settled_at": r["settled_at"],
        }
        for r in tail
    ]


def model_versions(db) -> dict[str, dict]:
    """Active model versions and their stored metrics, keyed by model name.

    Reads the ``model_versions`` table rather than ``versions.json`` because
    R11.1 confines the evaluator to SQLite. Returns an empty dict when nothing
    has been recorded yet -- a bootstrapping project, not an error.
    """
    rows = db.fetchall(
        "SELECT model_name, version, metrics_json, training_data_hash, created_at "
        "FROM model_versions WHERE is_active = 1 ORDER BY model_name"
    )
    out: dict[str, dict] = {}
    for r in rows:
        try:
            metrics = json.loads(r["metrics_json"]) if r["metrics_json"] else {}
        except json.JSONDecodeError:
            logger.warning("Unparseable metrics_json for %s v%s", r["model_name"], r["version"])
            metrics = {}
        out[r["model_name"]] = {
            "version": r["version"],
            "metrics": metrics,
            "training_data_hash": r["training_data_hash"],
            "created_at": r["created_at"],
        }
    return out


def feature_importances(db) -> dict[str, dict]:
    """Latest feature importances plus drift, keyed by model name (R10.4)."""
    out: dict[str, dict] = {}
    for model_name in performance.GBM_MODEL_STORES:
        history = performance.get_importance_history(db, model_name)
        if not history:
            continue
        out[model_name] = {
            "version": history[-1]["model_version"],
            "importances": history[-1]["importances"],
            "drift": performance.importance_drift(db, model_name)["drift"],
        }
    return out


def recent_improvements(db, limit: int = 10) -> list[dict]:
    """Recently applied improvements and how they fared (R12.2).

    Lets the evaluator see the outcome of its own past proposals, which R11.13's
    self-tracking depends on. Empty until Phase 6 starts writing them.
    """
    return db.fetchall(
        """SELECT type, target, description, current_value, proposed_value,
                  status, applied_at, post_apply_trades, post_apply_pnl_cents,
                  validated_on_n_samples, created_at
           FROM improvements
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )


def generate_snapshot(
    db,
    include_decay: bool = True,
    include_config: bool = True,
) -> dict:
    """Build the full performance snapshot the evaluator consumes (R10.7).

    Rebuilds ``daily_pnl`` first so the Sharpe ratio and P&L curve reflect every
    settled outcome rather than whatever was last written.

    Args:
        db: :class:`~src.db.database.Database` instance.
        include_decay: Include the edge-decay section (R10.6).
        include_config: Include current trading config. Skipped silently when
            the config singleton is unavailable (missing credentials in tests).

    Returns:
        A JSON-serialisable dict. Every breakdown is keyed by discovered market
        types and model names, never a fixed set (D5-07).
    """
    performance.recompute_all_daily_pnl(db)

    overall = performance.brier_summary(db)
    snapshot: dict = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": {
            **overall,
            **performance.win_rate(db),
            "sharpe_ratio": performance.sharpe_ratio(db),
            "drawdown": performance.drawdown(db),
            "edge_on_wins_vs_losses": performance.edge_on_wins_vs_losses(db),
        },
        "rolling": performance.rolling_brier(db),
        "by_market_type": {
            "brier": performance.brier_by_market_type(db),
            "pnl": performance.pnl_by_market_type(db),
        },
        "by_model": performance.brier_by_model(db),
        "calibration": {
            "bins": performance.calibration_bins(db),
            "by_market_type": {
                mt: performance.calibration_bins(db, market_type=mt)
                for mt in performance.distinct_market_types(db)
            },
        },
        "win_rate_by_edge_bucket": win_rate_by_edge_bucket(db),
        "model_versions": model_versions(db),
        "feature_importances": feature_importances(db),
        "recent_trades": recent_trades(db),
        "recent_improvements": recent_improvements(db),
    }

    if include_decay:
        snapshot["edge_decay"] = edge_decay.summarize_decay(db)

    if include_config:
        snapshot["config"] = _current_config()

    return snapshot


def _current_config() -> dict | None:
    """Return the active trading config, or None if it cannot be loaded.

    The config singleton is ``None`` when Kalshi credentials are absent, which is
    the normal state in tests. A missing config must not stop a snapshot.
    """
    try:
        from src.config import config

        if config is None:
            return None
        return {
            "environment": config.env,
            "risk": config.risk,
            "markets": config.markets,
        }
    except Exception as exc:  # noqa: BLE001 - config is optional context
        logger.debug("Config unavailable for snapshot: %s", exc)
        return None


def write_snapshot(db, out_path: str = "data/reports/snapshot.json", **kwargs) -> str:
    """Generate a snapshot and write it as pretty-printed JSON.

    Args:
        db: Database instance.
        out_path: Destination path. Parent directories are created.
        **kwargs: Passed through to :func:`generate_snapshot`.

    Returns:
        The path written.
    """
    snapshot = generate_snapshot(db, **kwargs)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=False)
    logger.info("Wrote performance snapshot to %s", out_path)
    return out_path
