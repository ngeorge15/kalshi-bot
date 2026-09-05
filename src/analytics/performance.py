"""Performance metrics over settled trades (R10.1, R10.2, R10.3).

Every metric in this module is derived from one join helper,
:func:`_settled_predictions`, which pairs each settled ``outcomes`` row with the
prediction that produced it.  Keeping the join in one place matters because it
has a fallback path (D5-06): ``outcomes.prediction_id`` is nullable, so rows
written before that link existed resolve by ticker instead.

Market types are never enumerated in this module.  They are discovered with
``SELECT DISTINCT`` so a new domain -- another sport, a new weather product --
appears in every breakdown without a code change (D5-07).

Scalar metrics reuse :mod:`src.validation.metrics` rather than recomputing them,
so the analytics layer and the walk-forward validator cannot drift apart.

Usage:
    from src.analytics.performance import brier_by_market_type, sharpe_ratio

    breakdown = brier_by_market_type(db)
    sharpe = sharpe_ratio(db)
"""

import logging
from datetime import datetime, timezone

import numpy as np

from src.validation.metrics import accuracy, brier_score, calibration_error

logger = logging.getLogger(__name__)

# Rolling windows reported by default (R10.1).
DEFAULT_ROLLING_WINDOWS = (50, 100)

# Trading days per year, used to annualise the daily P&L Sharpe ratio (D5-05).
TRADING_DAYS_PER_YEAR = 252

# Correlated-subquery fallback used when outcomes.prediction_id is NULL (D5-06).
# Takes the most recent prediction for the same ticker.
_FALLBACK = """(SELECT px.{col} FROM predictions px
                WHERE px.ticker = o.ticker
                ORDER BY px.created_at DESC LIMIT 1)"""

_SETTLED_SQL = f"""
SELECT
    o.ticker                                                   AS ticker,
    o.market_type                                              AS market_type,
    o.result                                                   AS result,
    o.settled_at                                               AS settled_at,
    o.pnl_cents                                                AS pnl_cents,
    COALESCE(p.predicted_prob, {_FALLBACK.format(col="predicted_prob")}) AS predicted_prob,
    COALESCE(p.model_name,     {_FALLBACK.format(col="model_name")})     AS model_name,
    COALESCE(p.edge_cents,     {_FALLBACK.format(col="edge_cents")})     AS edge_cents
FROM outcomes o
LEFT JOIN predictions p ON p.id = o.prediction_id
"""


def _settled_predictions(
    db,
    market_type: str | None = None,
    model_name: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Return settled outcomes paired with the prediction that produced them.

    The single source of truth for the ``predictions`` <-> ``outcomes`` join
    (D5-06).  Prefers ``outcomes.prediction_id``; falls back to the most recent
    prediction for the same ticker when that link is absent.  Outcomes with no
    resolvable prediction are dropped -- they carry no probability, so no
    accuracy metric can be computed from them -- and counted in a debug log.

    Args:
        db: :class:`~src.db.database.Database` instance.
        market_type: Optional exact-match filter.
        model_name: Optional exact-match filter.
        since: Optional ISO 8601 lower bound on ``settled_at`` (inclusive).

    Returns:
        Rows ordered oldest-first by ``settled_at``, each with keys ``ticker``,
        ``market_type``, ``model_name``, ``predicted_prob``, ``label``
        (1 for a 'yes' settlement, else 0), ``edge_cents``, ``pnl_cents`` and
        ``settled_at``.
    """
    conditions: list[str] = []
    params: list = []
    if market_type is not None:
        conditions.append("o.market_type = ?")
        params.append(market_type)
    if since is not None:
        conditions.append("o.settled_at >= ?")
        params.append(since)

    sql = _SETTLED_SQL
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY o.settled_at ASC, o.id ASC"

    rows = db.fetchall(sql, tuple(params))

    resolved: list[dict] = []
    unresolved = 0
    for row in rows:
        if row["predicted_prob"] is None:
            unresolved += 1
            continue
        # model_name filtering happens here rather than in SQL because the
        # column may come from either the join or the fallback subquery.
        if model_name is not None and row["model_name"] != model_name:
            continue
        resolved.append(
            {
                "ticker": row["ticker"],
                "market_type": row["market_type"],
                "model_name": row["model_name"],
                "predicted_prob": float(row["predicted_prob"]),
                "label": 1 if row["result"] == "yes" else 0,
                "edge_cents": row["edge_cents"],
                "pnl_cents": row["pnl_cents"],
                "settled_at": row["settled_at"],
            }
        )

    if unresolved:
        logger.debug(
            "%d settled outcome(s) had no resolvable prediction and were excluded",
            unresolved,
        )
    return resolved


def _arrays(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Split resolved rows into predicted-probability and label arrays."""
    probs = np.array([r["predicted_prob"] for r in rows], dtype=float)
    labels = np.array([r["label"] for r in rows], dtype=float)
    return probs, labels


def distinct_market_types(db) -> list[str]:
    """Return every market type present in ``outcomes``, sorted.

    Discovered rather than hardcoded so new domains appear automatically (D5-07).
    """
    rows = db.fetchall("SELECT DISTINCT market_type FROM outcomes ORDER BY market_type")
    return [r["market_type"] for r in rows]


def distinct_models(db) -> list[str]:
    """Return every model name present in ``predictions``, sorted."""
    rows = db.fetchall("SELECT DISTINCT model_name FROM predictions ORDER BY model_name")
    return [r["model_name"] for r in rows]


# ----------------------------------------------------------------------
# R10.1 -- Brier scores
# ----------------------------------------------------------------------


def brier_summary(
    db,
    market_type: str | None = None,
    model_name: str | None = None,
    since: str | None = None,
) -> dict:
    """Brier score, accuracy, ECE and calibration drift over settled trades.

    Args:
        db: Database instance.
        market_type: Optional filter.
        model_name: Optional filter.
        since: Optional ISO 8601 lower bound on ``settled_at``.

    Returns:
        Dict with ``n``, ``brier``, ``accuracy``, ``calibration_error``,
        ``mean_predicted_prob``, ``actual_frequency`` and ``calibration_drift``
        (mean predicted minus realised frequency; positive means overconfident).
        All metrics are ``None`` when ``n`` is 0 -- an empty slice is not an
        error, it just has no score.
    """
    rows = _settled_predictions(db, market_type, model_name, since)
    if not rows:
        return {
            "n": 0,
            "brier": None,
            "accuracy": None,
            "calibration_error": None,
            "mean_predicted_prob": None,
            "actual_frequency": None,
            "calibration_drift": None,
        }

    probs, labels = _arrays(rows)
    mean_pred = float(probs.mean())
    actual = float(labels.mean())
    return {
        "n": len(rows),
        "brier": brier_score(probs, labels),
        "accuracy": accuracy(probs, labels),
        "calibration_error": calibration_error(probs, labels),
        "mean_predicted_prob": mean_pred,
        "actual_frequency": actual,
        "calibration_drift": mean_pred - actual,
    }


def brier_by_market_type(db, since: str | None = None) -> dict[str, dict]:
    """Per-market-type Brier summary, keyed by market type (R10.1)."""
    return {
        mt: brier_summary(db, market_type=mt, since=since)
        for mt in distinct_market_types(db)
    }


def brier_by_model(db, since: str | None = None) -> dict[str, dict]:
    """Per-model Brier summary, keyed by model name (R10.1)."""
    return {
        name: brier_summary(db, model_name=name, since=since)
        for name in distinct_models(db)
    }


def rolling_brier(
    db,
    windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS,
    market_type: str | None = None,
    model_name: str | None = None,
) -> dict[str, dict]:
    """Brier score over the most recent N settled trades, per window (R10.1).

    Args:
        db: Database instance.
        windows: Window sizes. Defaults to last 50 and last 100.
        market_type: Optional filter.
        model_name: Optional filter.

    Returns:
        Dict keyed ``"last_50"``, ``"last_100"``, ... Each value carries ``n``
        (which may be smaller than the window if fewer trades have settled),
        ``brier``, ``accuracy`` and ``calibration_drift``.  A window with no
        trades reports ``n`` 0 and ``None`` metrics rather than raising.
    """
    rows = _settled_predictions(db, market_type, model_name)
    out: dict[str, dict] = {}
    for window in windows:
        recent = rows[-window:]
        key = f"last_{window}"
        if not recent:
            out[key] = {"n": 0, "brier": None, "accuracy": None, "calibration_drift": None}
            continue
        probs, labels = _arrays(recent)
        out[key] = {
            "n": len(recent),
            "brier": brier_score(probs, labels),
            "accuracy": accuracy(probs, labels),
            "calibration_drift": float(probs.mean() - labels.mean()),
        }
    return out


# ----------------------------------------------------------------------
# R10.2 -- Calibration / reliability
# ----------------------------------------------------------------------


def calibration_bins(
    db,
    n_bins: int = 10,
    market_type: str | None = None,
    model_name: str | None = None,
) -> list[dict]:
    """Reliability-diagram data: predicted probability vs realised frequency.

    Returns the binned data rather than an image (D5-04) so the snapshot
    generator and the tests consume the same numbers the plot would draw.

    Args:
        db: Database instance.
        n_bins: Number of equal-width bins across [0, 1].
        market_type: Optional filter.
        model_name: Optional filter.

    Returns:
        One dict per bin, always ``n_bins`` long so the shape is stable for
        plotting. Empty bins carry ``count`` 0 with ``None`` for
        ``mean_predicted`` and ``actual_frequency``. Keys: ``bin_lower``,
        ``bin_upper``, ``mean_predicted``, ``actual_frequency``, ``count``.
    """
    rows = _settled_predictions(db, market_type, model_name)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[dict] = []

    if not rows:
        probs = np.array([])
        labels = np.array([])
    else:
        probs, labels = _arrays(rows)

    for i in range(n_bins):
        lower, upper = float(edges[i]), float(edges[i + 1])
        if len(probs) == 0:
            mask = np.array([], dtype=bool)
        elif i == n_bins - 1:
            # Final bin is closed on the right so p == 1.0 is not dropped.
            mask = (probs >= lower) & (probs <= upper)
        else:
            mask = (probs >= lower) & (probs < upper)

        count = int(mask.sum())
        bins.append(
            {
                "bin_lower": lower,
                "bin_upper": upper,
                "mean_predicted": float(probs[mask].mean()) if count else None,
                "actual_frequency": float(labels[mask].mean()) if count else None,
                "count": count,
            }
        )
    return bins


# ----------------------------------------------------------------------
# R10.3 -- P&L analytics
# ----------------------------------------------------------------------


def pnl_curve(db, market_type: str | None = None) -> list[dict]:
    """Cumulative realised P&L over settled outcomes, oldest first (R10.3).

    Returns:
        One point per settled outcome with ``settled_at``, ``ticker``,
        ``market_type``, ``pnl_cents`` and running ``cumulative_pnl_cents``.
    """
    rows = _settled_predictions(db, market_type)
    curve: list[dict] = []
    running = 0
    for r in rows:
        running += r["pnl_cents"]
        curve.append(
            {
                "settled_at": r["settled_at"],
                "ticker": r["ticker"],
                "market_type": r["market_type"],
                "pnl_cents": r["pnl_cents"],
                "cumulative_pnl_cents": running,
            }
        )
    return curve


def win_rate(db, market_type: str | None = None) -> dict:
    """Win/loss counts over settled trades (R10.3).

    A settlement with exactly zero P&L counts as neither a win nor a loss but is
    included in ``n``.

    Returns:
        Dict with ``n``, ``wins``, ``losses``, ``win_rate`` (``None`` when there
        are no decided trades) and ``total_pnl_cents``.
    """
    rows = _settled_predictions(db, market_type)
    wins = sum(1 for r in rows if r["pnl_cents"] > 0)
    losses = sum(1 for r in rows if r["pnl_cents"] < 0)
    decided = wins + losses
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / decided) if decided else None,
        "total_pnl_cents": sum(r["pnl_cents"] for r in rows),
    }


def edge_on_wins_vs_losses(db, market_type: str | None = None) -> dict:
    """Mean edge claimed on winning trades vs losing ones (R10.3).

    If the model has genuine skill, the edge it claimed should be no smaller on
    winners than on losers.  A materially higher mean edge on losers is a signal
    that large claimed edges are actually model error.

    Returns:
        Dict with ``mean_edge_on_wins``, ``mean_edge_on_losses``, ``n_wins``,
        ``n_losses`` and ``edge_gap`` (wins minus losses). Means and gap are
        ``None`` when that side has no trades.
    """
    rows = _settled_predictions(db, market_type)
    win_edges = [r["edge_cents"] for r in rows if r["pnl_cents"] > 0 and r["edge_cents"] is not None]
    loss_edges = [r["edge_cents"] for r in rows if r["pnl_cents"] < 0 and r["edge_cents"] is not None]

    mean_wins = float(np.mean(win_edges)) if win_edges else None
    mean_losses = float(np.mean(loss_edges)) if loss_edges else None
    gap = (mean_wins - mean_losses) if (mean_wins is not None and mean_losses is not None) else None

    return {
        "mean_edge_on_wins": mean_wins,
        "mean_edge_on_losses": mean_losses,
        "n_wins": len(win_edges),
        "n_losses": len(loss_edges),
        "edge_gap": gap,
    }


def pnl_by_market_type(db) -> dict[str, dict]:
    """Per-market-type P&L and win rate, keyed by market type (R10.3)."""
    return {mt: win_rate(db, market_type=mt) for mt in distinct_market_types(db)}


def drawdown(db, market_type: str | None = None) -> dict:
    """Maximum peak-to-trough decline of the cumulative P&L curve (R11.3).

    Returns:
        Dict with ``max_drawdown_cents`` (a non-negative magnitude), ``peak_cents``,
        ``trough_cents`` and ``current_drawdown_cents``. All zero on an empty curve.
    """
    curve = pnl_curve(db, market_type)
    if not curve:
        return {
            "max_drawdown_cents": 0,
            "peak_cents": 0,
            "trough_cents": 0,
            "current_drawdown_cents": 0,
        }

    peak = curve[0]["cumulative_pnl_cents"]
    max_dd = 0
    dd_peak = peak
    dd_trough = peak
    for point in curve:
        value = point["cumulative_pnl_cents"]
        if value > peak:
            peak = value
        decline = peak - value
        if decline > max_dd:
            max_dd = decline
            dd_peak = peak
            dd_trough = value

    final = curve[-1]["cumulative_pnl_cents"]
    running_peak = max(p["cumulative_pnl_cents"] for p in curve)
    return {
        "max_drawdown_cents": int(max_dd),
        "peak_cents": int(dd_peak),
        "trough_cents": int(dd_trough),
        "current_drawdown_cents": int(running_peak - final),
    }


def sharpe_ratio(
    db,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    capital_cents: int | None = None,
) -> float | None:
    """Annualised Sharpe ratio of the daily realised P&L series (R10.3, D5-05).

    Uses daily returns rather than per-trade returns: binary contracts have
    varying hold times, so a per-trade Sharpe has no consistent period and is
    not comparable across market types.

    Reads the ``daily_pnl`` table, so call :func:`recompute_daily_pnl` first if
    outcomes have settled since it was last built.

    Args:
        db: Database instance.
        periods_per_year: Annualisation factor. Defaults to 252 trading days.
        capital_cents: If given, daily P&L is divided by this to produce a true
            fractional return.  Sharpe is scale-invariant, so this changes
            nothing numerically -- it is accepted for interpretability only.

    Returns:
        The annualised ratio, or ``None`` when there are fewer than two days of
        data or the daily P&L series has zero variance (an undefined ratio, not
        an infinite one).
    """
    rows = db.fetchall("SELECT realized_pnl_cents FROM daily_pnl ORDER BY date ASC")
    if len(rows) < 2:
        logger.debug("Sharpe undefined: %d day(s) of P&L history", len(rows))
        return None

    returns = np.array([r["realized_pnl_cents"] for r in rows], dtype=float)
    if capital_cents:
        returns = returns / float(capital_cents)

    std = returns.std(ddof=1)
    if std == 0:
        logger.debug("Sharpe undefined: daily P&L has zero variance")
        return None

    return float(returns.mean() / std * np.sqrt(periods_per_year))


# ----------------------------------------------------------------------
# daily_pnl maintenance -- the table nothing previously populated
# ----------------------------------------------------------------------


def recompute_daily_pnl(db, date_str: str | None = None) -> dict:
    """Rebuild the ``daily_pnl`` row for one date from settled outcomes.

    ``Ledger.update_daily_pnl`` is an upsert with no caller and no aggregator;
    this supplies the aggregation that R10.3 (Sharpe, P&L curve) and R10.8
    (daily report) both depend on.  Writes through the ledger so the upsert SQL
    lives in exactly one place.

    Unrealised P&L is always written as 0: outcomes rows are settled by
    definition, and open-position marking belongs to the trading engine.

    Args:
        db: Database instance.
        date_str: ISO date ``YYYY-MM-DD``. Defaults to today (UTC).

    Returns:
        The row written, as :meth:`Ledger.get_daily_pnl` would return it.
    """
    from src.trading.ledger import Ledger

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    row = db.fetchone(
        """SELECT
               COALESCE(SUM(pnl_cents), 0)                     AS realized,
               COUNT(*)                                        AS total,
               COALESCE(SUM(pnl_cents > 0), 0)                 AS wins,
               COALESCE(SUM(pnl_cents < 0), 0)                 AS losses
           FROM outcomes
           WHERE DATE(settled_at) = ?""",
        (date_str,),
    )

    ledger = Ledger(db)
    ledger.update_daily_pnl(
        date_str=date_str,
        realized_pnl_cents=int(row["realized"]),
        unrealized_pnl_cents=0,
        total_trades=int(row["total"]),
        winning_trades=int(row["wins"]),
        losing_trades=int(row["losses"]),
    )
    return ledger.get_daily_pnl(date_str)


def recompute_all_daily_pnl(db) -> list[dict]:
    """Rebuild every ``daily_pnl`` row that has settled outcomes.

    Returns:
        The rebuilt rows, oldest first.
    """
    dates = db.fetchall(
        "SELECT DISTINCT DATE(settled_at) AS d FROM outcomes ORDER BY d ASC"
    )
    rebuilt = [recompute_daily_pnl(db, r["d"]) for r in dates if r["d"]]
    logger.info("Rebuilt %d daily_pnl row(s)", len(rebuilt))
    return rebuilt


# ----------------------------------------------------------------------
# R10.4 -- Feature importance, tracked over time
# ----------------------------------------------------------------------

# Only these six stores hold a scikit-learn estimator.  The weather models are
# not ML models at all: WeatherTempModel fits an additive bias correction and
# WeatherPrecipModel blends a historical base rate with the NWS probability of
# precipitation.  Neither exposes feature_importances_, so R10.4 does not apply
# to them.  Listed explicitly rather than discovered because the mapping from a
# store name to its feature-name list lives in the model modules.
GBM_MODEL_STORES = (
    "nba_game",
    "nba_totals",
    "nba_props_pts",
    "nba_props_reb",
    "nba_props_ast",
    "nba_props_3pm",
)


def gbm_feature_names(model_name: str) -> list[str]:
    """Return the ordered feature names for a GBM-backed model store.

    Args:
        model_name: One of :data:`GBM_MODEL_STORES`.

    Returns:
        Feature names in the order the model's feature vector packs them.

    Raises:
        ValueError: If *model_name* is not a GBM-backed store.
    """
    if model_name == "nba_game":
        from src.models.nba_game import FEATURE_NAMES

        return list(FEATURE_NAMES)
    if model_name == "nba_totals":
        from src.models.nba_totals import FEATURE_NAMES

        return list(FEATURE_NAMES)
    if model_name.startswith("nba_props_"):
        from src.models.nba_props import FEATURE_NAMES

        prop_type = model_name[len("nba_props_") :]
        if prop_type not in FEATURE_NAMES:
            raise ValueError(f"Unknown prop type in model name: {model_name}")
        return list(FEATURE_NAMES[prop_type])
    raise ValueError(
        f"{model_name} is not a GBM-backed model store. Valid: {GBM_MODEL_STORES}"
    )


def extract_feature_importances(model, feature_names: list[str]) -> dict[str, float]:
    """Pull per-feature importances out of a fitted estimator (R10.4).

    Models are persisted as ``CalibratedClassifierCV(GradientBoostingClassifier,
    cv=3)``, so the importances live on three separate inner estimators -- one
    per calibration fold.  They are averaged, which is the standard reading of a
    cross-fitted ensemble's importance.

    Args:
        model: A fitted ``CalibratedClassifierCV``, or any estimator exposing
            ``feature_importances_`` directly.
        feature_names: Names in feature-vector order.

    Returns:
        Mapping of feature name to mean importance, ordered as *feature_names*.

    Raises:
        ValueError: If no importances can be found, or their count does not
            match *feature_names* -- a silent mismatch would mislabel every
            feature, which is worse than failing.
    """
    inner = getattr(model, "calibrated_classifiers_", None)
    if inner:
        per_fold = [
            clf.estimator.feature_importances_
            for clf in inner
            if hasattr(getattr(clf, "estimator", None), "feature_importances_")
        ]
        if not per_fold:
            raise ValueError(
                "CalibratedClassifierCV inner estimators expose no feature_importances_"
            )
        importances = np.mean(np.vstack(per_fold), axis=0)
    elif hasattr(model, "feature_importances_"):
        importances = np.asarray(model.feature_importances_, dtype=float)
    else:
        raise ValueError(
            f"{type(model).__name__} exposes no feature_importances_ and is not "
            "a CalibratedClassifierCV"
        )

    if len(importances) != len(feature_names):
        raise ValueError(
            f"Feature count mismatch: model has {len(importances)} importances, "
            f"{len(feature_names)} names supplied"
        )
    return {name: float(value) for name, value in zip(feature_names, importances)}


def record_feature_importances(
    db,
    model_name: str,
    model_version: int,
    model,
    feature_names: list[str] | None = None,
) -> dict[str, float]:
    """Persist a model version's feature importances to SQLite (R10.4, D5-02).

    Written to the ``feature_importance`` table so the Phase 6 evaluator can read
    them over SQL (R11.1 confines it to SQLite reads).  The table's
    ``UNIQUE(model_name, model_version, feature_name)`` makes re-recording the
    same version idempotent via ``INSERT OR REPLACE``.

    Args:
        db: Database instance.
        model_name: Model store name.
        model_version: Version these importances belong to.
        model: The fitted estimator.
        feature_names: Defaults to :func:`gbm_feature_names` for *model_name*.

    Returns:
        The recorded name-to-importance mapping.
    """
    if feature_names is None:
        feature_names = gbm_feature_names(model_name)

    importances = extract_feature_importances(model, feature_names)
    recorded_at = datetime.now(timezone.utc).isoformat()

    with db.transaction() as conn:
        for name, value in importances.items():
            conn.execute(
                """INSERT OR REPLACE INTO feature_importance
                (model_name, model_version, feature_name, importance, recorded_at)
                VALUES (?, ?, ?, ?, ?)""",
                (model_name, model_version, name, value, recorded_at),
            )

    logger.info(
        "Recorded %d feature importances for %s v%d",
        len(importances),
        model_name,
        model_version,
    )
    return importances


def record_model_version(
    db,
    model_name: str,
    version: int,
    parameters: dict | None = None,
    metrics: dict | None = None,
    training_data_hash: str = "",
    is_active: bool = True,
) -> None:
    """Mirror a ModelStore version into the ``model_versions`` table (D5-02).

    ``ModelStore`` persists to ``versions.json``, which remains authoritative for
    loading artifacts.  This copies the metadata into SQL because the Phase 6
    evaluator cannot read that file (R11.1).  The table has been in the schema
    since v1 with nothing writing to it.

    Marking a version active clears the flag on that model's other versions.
    """
    import json

    if is_active:
        db.execute(
            "UPDATE model_versions SET is_active = 0 WHERE model_name = ?",
            (model_name,),
        )
    db.execute(
        """INSERT OR REPLACE INTO model_versions
        (model_name, version, parameters_json, metrics_json,
         training_data_hash, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            model_name,
            version,
            json.dumps(parameters or {}),
            json.dumps(metrics or {}),
            training_data_hash,
            1 if is_active else 0,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_importance_history(db, model_name: str) -> list[dict]:
    """Feature importances for a model across versions, oldest version first.

    This is the "tracked over time" half of R10.4 -- a feature whose importance
    collapses between versions is exactly the drift the evaluator should notice.

    Returns:
        One dict per version with ``model_version``, ``recorded_at`` and
        ``importances`` (name to value).
    """
    rows = db.fetchall(
        """SELECT model_version, feature_name, importance, recorded_at
           FROM feature_importance
           WHERE model_name = ?
           ORDER BY model_version ASC, feature_name ASC""",
        (model_name,),
    )
    by_version: dict[int, dict] = {}
    for r in rows:
        entry = by_version.setdefault(
            r["model_version"],
            {
                "model_version": r["model_version"],
                "recorded_at": r["recorded_at"],
                "importances": {},
            },
        )
        entry["importances"][r["feature_name"]] = r["importance"]
    return [by_version[v] for v in sorted(by_version)]


def importance_drift(db, model_name: str) -> dict:
    """Change in each feature's importance between the two latest versions.

    Returns:
        Dict with ``from_version``, ``to_version`` and ``drift`` (name to
        delta).  ``drift`` is empty and the versions ``None`` when fewer than
        two versions have been recorded.  Features present in only one version
        are reported against 0.
    """
    history = get_importance_history(db, model_name)
    if len(history) < 2:
        return {"from_version": None, "to_version": None, "drift": {}}

    previous, latest = history[-2], history[-1]
    names = set(previous["importances"]) | set(latest["importances"])
    return {
        "from_version": previous["model_version"],
        "to_version": latest["model_version"],
        "drift": {
            name: latest["importances"].get(name, 0.0)
            - previous["importances"].get(name, 0.0)
            for name in sorted(names)
        },
    }
