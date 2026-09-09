"""Retrospective NBA model-skill backtest: walk-forward against real outcomes.

Scope, stated plainly so it cannot be misread later:

This module answers "does the NBA model have predictive skill at all?" by
scoring it, walk-forward, against real historical game outcomes and against
two baselines: a constant home-win-rate predictor (:func:`always_home`) and
an ELO-difference-only predictor (:func:`elo_only`). A model that cannot beat
``elo_only`` has learned nothing beyond team strength.

This module does **not** answer "does the model beat the market?" There is
no historical Kalshi/sportsbook odds source in this repository today --
``nba_api`` supplies game results, not betting lines. ``run_backtest``
therefore defaults ``market_prices`` to ``None`` and reports
``market_comparison: None`` with an explicit note that no market comparison
was performed, rather than silently omitting it or standing in a baseline
for a market. When a real odds CSV exists, :func:`load_market_prices` reads
it and ``run_backtest(..., market_prices=...)`` turns on a paired,
event-clustered model-vs-market Brier comparison with no other code change.

Temporal integrity is the hard requirement throughout: games are sorted by
``game_date`` ascending, ELO is updated strictly in that order, and a game's
features (including the ELO-implied probability used for ``elo_only``) are
always computed *before* that game's result updates the ELO tracker -- the
same no-leakage pattern
:meth:`~src.models.nba_game.NBAGameModel.build_training_data` uses. Splitting
into train/test windows is delegated entirely to
:class:`~src.validation.walk_forward.WalkForwardValidator`; this module does
not implement its own splitter. All scalar metrics reuse
:mod:`src.validation.metrics` (Brier score, accuracy, calibration error), and
confidence intervals reuse :mod:`src.paper.uncertainty`'s event-clustered
bootstrap so a handful of highly-correlated games can't be over-counted as
independent evidence -- though for NBA (one game, one event) this is expected
to matter far less than it does for weather brackets, which is exactly why
both an event_key=game_id and an event_key=game_date clustering are reported
side by side rather than assumed.

Usage::

    from src.analytics.backtest import run_backtest, load_market_prices

    result = run_backtest(games)  # games: see module docstring row shape below
    print(result["aggregate"]["brier_improvement_vs_elo_only"])
    print(result["market_comparison"])  # None -- no odds source supplied

    prices = load_market_prices("data/nba_odds_2023-24.csv")
    result = run_backtest(games, market_prices=prices)
    print(result["market_comparison"]["by_game_id"])

Each game dict is expected to carry the ``nba_game_results`` row shape:
``game_id``, ``game_date`` (an ISO-8601 string, so lexicographic sort ==
chronological sort), ``home_team_id``, ``away_team_id``, ``home_pts``,
``away_pts``, ``home_win`` (0/1), ``season``.
"""

import csv
import logging
import math
from typing import Any, Callable, Optional

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier

from src.data.nba.teams import EloTracker
from src.models.nba_game import FEATURE_NAMES
from src.paper.uncertainty import cluster_bootstrap_ci
from src.validation.metrics import accuracy, brier_score, calibration_error
from src.validation.walk_forward import WalkForwardValidator

logger = logging.getLogger(__name__)

# WalkForwardValidator's own default -- reused so this module doesn't invent
# a second convention for "how many test windows."
DEFAULT_N_SPLITS = 4

# Bumped from WalkForwardValidator's own default of 50: the default model
# fits a CalibratedClassifierCV(cv=CALIBRATION_CV_FOLDS), which needs enough
# games of the minority class per training window to form real folds instead
# of degenerating on the very first window.
DEFAULT_MIN_TRAIN_SIZE = 100

# Mirrors NBAGameModel.train()'s defaults so the harness's "model" reflects
# the production model's own hyperparameters rather than inventing new ones.
MODEL_N_ESTIMATORS = 100
MODEL_MAX_DEPTH = 3
MODEL_LEARNING_RATE = 0.1
MODEL_RANDOM_STATE = 42  # Determinism requirement; mirrors NBAGameModel.train.
CALIBRATION_CV_FOLDS = 3  # Mirrors NBAGameModel.train's CalibratedClassifierCV(cv=3).

# sklearn's StratifiedKFold cannot form fewer than 2 folds; below that,
# calibration is meaningless anyway.
MIN_CV_FOLDS = 2

# Mirrors src.paper.uncertainty's own bootstrap defaults so a caller who
# skips these arguments gets the same statistical convention everywhere.
DEFAULT_N_RESAMPLES = 2000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_SEED = 0

# Relative difference in confidence-interval width, between clustering by
# game_id and clustering by whole-slate game_date, above which the two are
# reported as "materially different" rather than "as expected, similar."
CLUSTERING_MATERIAL_DIFFERENCE_THRESHOLD = 0.20

# A market probability is a probability: must be finite and in [0, 1].
MIN_MARKET_PROB = 0.0
MAX_MARKET_PROB = 1.0

ModelFitFn = Callable[[np.ndarray, np.ndarray], Any]
ModelPredictFn = Callable[[Any, np.ndarray, np.ndarray], np.ndarray]


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def base_rate(games: list[dict[str, Any]]) -> float:
    """Return the empirical home-win rate over *games*.

    This is the training-window quantity that :func:`always_home` turns into
    a per-game baseline prediction. Kept as its own function (rather than
    inlined) so the "what rate was used" question always has one answer.

    Args:
        games: Game dicts, each with a ``home_win`` key (0/1 or bool).

    Returns:
        The fraction of *games* with ``home_win`` truthy, as a float in
        ``[0.0, 1.0]``.

    Raises:
        ValueError: If *games* is empty -- there is no rate to report.
    """
    if not games:
        raise ValueError("base_rate requires at least one game")
    return float(np.mean([1.0 if g["home_win"] else 0.0 for g in games]))


def always_home(game: dict[str, Any], home_win_rate: float) -> float:
    """Return the constant home-win-rate baseline prediction for *game*.

    *game* is accepted (and ignored) so this has the same
    ``(game, ...) -> float`` shape as :func:`elo_only`, and can be mapped
    over a list of games uniformly with the other baselines.

    Args:
        game: A single game dict (unused; kept for interface symmetry).
        home_win_rate: The training-window home-win rate, as returned by
            :func:`base_rate` on the games *before* this one.

    Returns:
        *home_win_rate*, unchanged.
    """
    del game  # Intentionally unused -- see docstring.
    return home_win_rate


def elo_only(game: dict[str, Any], tracker: EloTracker) -> float:
    """Return P(home team wins) implied by ELO difference alone.

    Uses :class:`~src.data.nba.teams.EloTracker`'s own logistic formula and
    ``HOME_ADVANTAGE`` constant, so this baseline reflects exactly the same
    ELO convention the production model's ``home_elo``/``away_elo`` features
    are built from -- it isolates what those two features alone are worth.

    Args:
        game: A dict with ``home_team_id`` and ``away_team_id`` keys.
        tracker: An :class:`EloTracker` holding ratings as of just before
            *game* (the caller is responsible for not having updated it with
            *game*'s own result yet -- see the module docstring on temporal
            integrity).

    Returns:
        Probability in ``[0.0, 1.0]`` that the home team wins.
    """
    home_elo = tracker.get_elo(game["home_team_id"]) + EloTracker.HOME_ADVANTAGE
    away_elo = tracker.get_elo(game["away_team_id"])
    return EloTracker.expected_outcome(home_elo, away_elo)


# ---------------------------------------------------------------------------
# Default pluggable model (mirrors NBAGameModel's training approach)
# ---------------------------------------------------------------------------


class _ConstantProbabilityModel:
    """Fallback "model" for a training window with only one outcome class.

    ``CalibratedClassifierCV`` cannot be fit when a training window has seen
    only wins or only losses (no minority class to hold out a fold on). This
    stands in with the single observed rate rather than raising and aborting
    the whole backtest over one degenerate window.
    """

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        n = len(X)
        p = self.probability
        return np.column_stack([np.full(n, 1.0 - p), np.full(n, p)])


def _default_fit(X_train: np.ndarray, y_train: np.ndarray) -> Any:
    """Fit a GBM + Platt calibration, mirroring NBAGameModel.train().

    Falls back to :class:`_ConstantProbabilityModel` when *y_train* has only
    one class -- see that class's docstring.
    """
    y_train = np.asarray(y_train)
    unique_classes = np.unique(y_train)
    if len(unique_classes) < 2:
        rate = float(y_train.mean()) if len(y_train) else 0.5
        logger.warning(
            "Training window has a single outcome class (n=%d); falling back "
            "to a constant predictor at rate=%.3f instead of fitting a GBM.",
            len(y_train), rate,
        )
        return _ConstantProbabilityModel(rate)

    minority_count = int(np.bincount(y_train.astype(int)).min())
    cv_folds = max(MIN_CV_FOLDS, min(CALIBRATION_CV_FOLDS, minority_count))

    gbm = GradientBoostingClassifier(
        n_estimators=MODEL_N_ESTIMATORS,
        max_depth=MODEL_MAX_DEPTH,
        learning_rate=MODEL_LEARNING_RATE,
        random_state=MODEL_RANDOM_STATE,
    )
    calibrated = CalibratedClassifierCV(gbm, method="sigmoid", cv=cv_folds)
    calibrated.fit(X_train, y_train)
    return calibrated


def _default_predict(model: Any, X_test: np.ndarray, y_test: np.ndarray) -> np.ndarray:
    """Predict P(home wins) from a fitted model. Ignores *y_test*.

    *y_test* is part of :data:`ModelPredictFn`'s signature only so that test
    doubles (an oracle, an inverted model) can see the truth they are
    deliberately cheating with; the default, realistic predictor never
    looks at it -- doing so would be leakage.
    """
    del y_test  # Unused by a real model; see docstring.
    return model.predict_proba(X_test)[:, 1]


# ---------------------------------------------------------------------------
# Feature assembly (no leakage: features before the ELO update they precede)
# ---------------------------------------------------------------------------


def _sort_and_validate_games(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort *games* by ``game_date`` ascending and assert the result is ordered.

    Args:
        games: Game dicts with a ``game_date`` key (ISO-8601 string).

    Returns:
        A new list, sorted ascending by ``game_date``.

    Raises:
        AssertionError: If the sorted output is not itself non-decreasing --
            this can only happen if ``game_date`` values are not mutually
            comparable, and it must be caught before any ELO or walk-forward
            logic runs on top of a false ordering assumption.
    """
    sorted_games = sorted(games, key=lambda g: g["game_date"])
    dates = [g["game_date"] for g in sorted_games]
    assert dates == sorted(dates), (
        "games are not temporally orderable by game_date after sorting; "
        "check for mixed/uncomparable date representations"
    )
    return sorted_games


def _build_features(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the feature matrix, labels, and ELO-only baseline, in one pass.

    A single :class:`EloTracker` is advanced through *games* in chronological
    order exactly once. For game ``i``, ``tracker.get_elo`` is read (for both
    the feature row and :func:`elo_only`) strictly *before* ``tracker.update``
    is called with game ``i``'s result -- so neither the model's ELO features
    nor the ``elo_only`` baseline for game ``i`` can see game ``i``'s own
    outcome, or any later game's outcome. This holds regardless of where a
    later walk-forward train/test split falls, because ELO is an online,
    rolling statistic, not something fit per-window.

    Args:
        games: Temporally-sorted game dicts (see module docstring for shape).

    Returns:
        Dict with ``X`` (shape ``(n, len(FEATURE_NAMES))``), ``y`` (shape
        ``(n,)``, 0/1), ``elo_prob`` (shape ``(n,)``, the pre-game
        :func:`elo_only` probability), ``game_ids`` (list[str]), and
        ``game_dates`` (list[str]).
    """
    tracker = EloTracker()
    n = len(games)
    X = np.empty((n, len(FEATURE_NAMES)), dtype=float)
    y = np.empty(n, dtype=int)
    elo_prob = np.empty(n, dtype=float)
    game_ids: list[str] = []
    game_dates: list[str] = []

    for i, g in enumerate(games):
        home_id, away_id = g["home_team_id"], g["away_team_id"]
        home_elo = tracker.get_elo(home_id)
        away_elo = tracker.get_elo(away_id)

        # Feature order matches src.models.nba_game.FEATURE_NAMES. Rest-day
        # and net-rating features are not available for historical replay
        # (nba_game_results carries no schedule/team-stats snapshot per
        # game); held at the same neutral placeholders
        # NBAGameModel.build_training_data uses for the identical reason.
        X[i] = [home_elo, away_elo, 1.0, 1.0, 1.0, 0.0, 0.0]
        y[i] = 1 if g["home_win"] else 0
        elo_prob[i] = elo_only(g, tracker)  # Pre-update: no leakage.
        game_ids.append(g["game_id"])
        game_dates.append(g["game_date"])

        margin = abs(g["home_pts"] - g["away_pts"])
        if g["home_win"]:
            tracker.update(home_id, away_id, margin)
        else:
            tracker.update(away_id, home_id, margin)

    return {"X": X, "y": y, "elo_prob": elo_prob, "game_ids": game_ids, "game_dates": game_dates}


def _make_test_fn(predict_fn: ModelPredictFn) -> Callable[[Any, np.ndarray, np.ndarray], dict]:
    """Wrap *predict_fn* into the ``(model, X_test, y_test) -> dict`` shape
    :class:`~src.validation.walk_forward.WalkForwardValidator` expects, also
    returning the raw per-game probabilities so the caller can build
    event-clustered rows without re-predicting.
    """

    def test_fn(model: Any, X_test: np.ndarray, y_test: np.ndarray) -> dict[str, Any]:
        probs = np.asarray(predict_fn(model, X_test, y_test), dtype=float)
        return {
            "brier_score": brier_score(probs, y_test),
            "accuracy": accuracy(probs, y_test),
            "calibration_error": calibration_error(probs, y_test),
            "probs": probs,
        }

    return test_fn


def _ci_width(ci_result: dict[str, Any]) -> Optional[float]:
    """Return ``ci_high - ci_low`` from a `cluster_bootstrap_ci` result, or
    ``None`` if either bound is undefined."""
    if ci_result.get("ci_low") is None or ci_result.get("ci_high") is None:
        return None
    return ci_result["ci_high"] - ci_result["ci_low"]


# ---------------------------------------------------------------------------
# Market seam -- inert until a real odds source exists
# ---------------------------------------------------------------------------


def load_market_prices(path: str) -> dict[str, float]:
    """Read a ``game_id,market_yes_probability`` CSV of real market odds.

    This is the only supported way to get market probabilities into
    :func:`run_backtest`. There is no synthetic or approximated substitute:
    if this function has not been called with a real odds file, the market
    seam stays off.

    Args:
        path: Path to a CSV file with a header row containing at least
            ``game_id`` and ``market_yes_probability`` columns.

    Returns:
        Dict mapping ``game_id`` to its validated market-implied probability
        that the home team wins (yes).

    Raises:
        ValueError: If the required columns are missing, or if any
            ``market_yes_probability`` value is not a finite number in
            ``[0.0, 1.0]``.
    """
    prices: dict[str, float] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        if "game_id" not in fieldnames or "market_yes_probability" not in fieldnames:
            raise ValueError(
                f"{path}: expected columns 'game_id,market_yes_probability', "
                f"got {fieldnames!r}"
            )
        for row_num, row in enumerate(reader):
            game_id = row["game_id"]
            raw_prob = row["market_yes_probability"]
            try:
                prob = float(raw_prob)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path} row {row_num}: market_yes_probability={raw_prob!r} is not a number"
                ) from exc
            if not math.isfinite(prob) or not (MIN_MARKET_PROB <= prob <= MAX_MARKET_PROB):
                raise ValueError(
                    f"{path} row {row_num}: market_yes_probability must be finite and within "
                    f"[{MIN_MARKET_PROB}, {MAX_MARKET_PROB}], got {prob!r}"
                )
            prices[game_id] = prob
    return prices


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def run_backtest(
    games: list[dict[str, Any]],
    n_splits: int = DEFAULT_N_SPLITS,
    min_train_size: int = DEFAULT_MIN_TRAIN_SIZE,
    fit_fn: Optional[ModelFitFn] = None,
    predict_fn: Optional[ModelPredictFn] = None,
    market_prices: Optional[dict[str, float]] = None,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Walk-forward backtest of an NBA model against baselines (and, optionally, a market).

    For each walk-forward test window (see
    :class:`~src.validation.walk_forward.WalkForwardValidator`): fit *fit_fn*
    on the training games only, predict with *predict_fn* plus
    :func:`always_home` and :func:`elo_only` for every game in the test
    window, and score all three with :mod:`src.validation.metrics`. See the
    module docstring for the temporal-integrity guarantees this relies on.

    Args:
        games: Temporally-sorted (or sortable) game dicts; see module
            docstring for the required shape.
        n_splits: Number of walk-forward test windows.
        min_train_size: Minimum number of games in the first training window.
        fit_fn: ``(X_train, y_train) -> model``. Defaults to a GBM + Platt
            calibration mirroring ``NBAGameModel.train()``. Overridable so
            tests (and future candidate models) can substitute a stub.
        predict_fn: ``(model, X_test, y_test) -> probs``. Defaults to
            ``model.predict_proba(X_test)[:, 1]``, ignoring ``y_test``.
            Overridable for the same reason as *fit_fn* -- an oracle or
            inverted stub for sanity tests must see ``y_test`` even though a
            real model never should.
        market_prices: Optional ``{game_id: market_yes_probability}`` map
            from :func:`load_market_prices`. When ``None`` (the default), no
            market comparison is attempted -- see ``market_comparison`` in
            the return value.
        n_resamples: Bootstrap resamples for every clustered confidence
            interval computed (see :mod:`src.paper.uncertainty`).
        confidence: Confidence-interval coverage, e.g. ``0.95``.
        seed: Seed for the bootstrap's local ``numpy.random.Generator`` --
            never numpy's global RNG. Determinism is a hard requirement here.

    Returns:
        A dict with:

        - ``scope``: str, restating in-band what this result does and does
          not establish (skill vs. baselines; not "beats the market" unless
          *market_prices* was supplied).
        - ``n_games_total``: int, total games passed in (before windowing).
        - ``windows``: list of per-window dicts, each with ``window``,
          ``train_size``, ``test_size``, ``n_games``, ``train_end_date``,
          ``test_start_date``, ``test_end_date``, ``always_home_rate_used``,
          ``{model,always_home,elo_only}_{brier,accuracy,calibration_error}``,
          and ``brier_improvement_vs_{always_home,elo_only}`` (baseline Brier
          minus model Brier -- positive means the model is better).
        - ``aggregate``: the same metric keys, computed over every
          out-of-sample game across all windows concatenated, plus
          ``n_windows`` and ``n_games``.
        - ``clustering_sensitivity``: dict comparing the model against
          ``elo_only`` (the closest thing to a "market" this repo has) via
          :func:`~src.paper.uncertainty.cluster_bootstrap_ci`, clustered both
          ``by_game_id`` (one game, one event) and ``by_slate_date`` (a whole
          day's games as one event), with a ``materially_different`` flag
          and explanatory ``note``.
        - ``market_comparison``: ``None`` when *market_prices* was not
          supplied, or a dict with ``n_games_matched``, ``by_game_id``, and
          ``by_slate_date`` clustered comparisons when it was.
        - ``market_comparison_note``: str explaining what, if anything, was
          computed for ``market_comparison`` and why.

    Raises:
        ValueError: If *games* is empty, or if *n_splits*/*min_train_size*
            leave no walk-forward window to evaluate.
    """
    if not games:
        raise ValueError("run_backtest requires at least one game")

    fit_fn = fit_fn or _default_fit
    predict_fn = predict_fn or _default_predict

    sorted_games = _sort_and_validate_games(games)
    built = _build_features(sorted_games)
    X, y = built["X"], built["y"]
    elo_prob_all: np.ndarray = built["elo_prob"]
    game_ids_all: list[str] = built["game_ids"]
    game_dates_all: list[str] = built["game_dates"]

    validator = WalkForwardValidator(n_splits=n_splits, min_train_size=min_train_size)
    test_fn = _make_test_fn(predict_fn)

    windows: list[dict[str, Any]] = []
    all_model_probs: list[np.ndarray] = []
    all_elo_probs: list[np.ndarray] = []
    all_home_probs: list[np.ndarray] = []
    all_labels: list[np.ndarray] = []
    rows_by_game: list[dict[str, Any]] = []
    rows_by_date: list[dict[str, Any]] = []
    market_rows_by_game: list[dict[str, Any]] = []
    market_rows_by_date: list[dict[str, Any]] = []
    n_market_matched = 0

    for raw in validator.validate(X, y, fit_fn, test_fn):
        test_start = raw["test_start_idx"]
        test_end = test_start + raw["test_size"]
        train_end = raw["train_end_idx"] + 1  # train_end_idx is inclusive.

        # Temporal integrity check made concrete: every training game's date
        # must be <= every test game's date in this window. Sorted input and
        # a contiguous expanding-window split guarantee this; assert it so a
        # future refactor that breaks the invariant fails loudly here rather
        # than silently leaking the future into training.
        assert game_dates_all[train_end - 1] <= game_dates_all[test_start], (
            f"window {raw['window']}: training window reaches past its test window "
            f"({game_dates_all[train_end - 1]!r} > {game_dates_all[test_start]!r})"
        )

        train_games = sorted_games[:train_end]
        home_rate = base_rate(train_games)

        y_test = y[test_start:test_end]
        model_probs = np.asarray(raw["metrics"]["probs"], dtype=float)
        elo_probs = elo_prob_all[test_start:test_end]
        home_probs = np.full(len(y_test), home_rate, dtype=float)
        ids_test = game_ids_all[test_start:test_end]
        dates_test = game_dates_all[test_start:test_end]

        model_brier = brier_score(model_probs, y_test)
        model_acc = accuracy(model_probs, y_test)
        model_cal = calibration_error(model_probs, y_test)

        home_brier = brier_score(home_probs, y_test)
        home_acc = accuracy(home_probs, y_test)
        home_cal = calibration_error(home_probs, y_test)

        elo_brier = brier_score(elo_probs, y_test)
        elo_acc = accuracy(elo_probs, y_test)
        elo_cal = calibration_error(elo_probs, y_test)

        windows.append({
            "window": raw["window"],
            "train_size": raw["train_size"],
            "test_size": raw["test_size"],
            "n_games": raw["test_size"],
            "train_end_date": game_dates_all[train_end - 1],
            "test_start_date": dates_test[0],
            "test_end_date": dates_test[-1],
            "always_home_rate_used": home_rate,
            "model_brier": model_brier,
            "model_accuracy": model_acc,
            "model_calibration_error": model_cal,
            "always_home_brier": home_brier,
            "always_home_accuracy": home_acc,
            "always_home_calibration_error": home_cal,
            "elo_only_brier": elo_brier,
            "elo_only_accuracy": elo_acc,
            "elo_only_calibration_error": elo_cal,
            "brier_improvement_vs_always_home": home_brier - model_brier,
            "brier_improvement_vs_elo_only": elo_brier - model_brier,
        })

        all_model_probs.append(model_probs)
        all_elo_probs.append(elo_probs)
        all_home_probs.append(home_probs)
        all_labels.append(y_test)

        for gid, gdate, mp, ep, actual in zip(ids_test, dates_test, model_probs, elo_probs, y_test):
            m_brier = float((mp - actual) ** 2)
            e_brier = float((ep - actual) ** 2)
            rows_by_game.append({"event_key": gid, "model_brier": m_brier, "market_brier": e_brier})
            rows_by_date.append({"event_key": gdate, "model_brier": m_brier, "market_brier": e_brier})
            if market_prices is not None and gid in market_prices:
                mk_brier = float((market_prices[gid] - actual) ** 2)
                market_rows_by_game.append(
                    {"event_key": gid, "model_brier": m_brier, "market_brier": mk_brier}
                )
                market_rows_by_date.append(
                    {"event_key": gdate, "model_brier": m_brier, "market_brier": mk_brier}
                )
                n_market_matched += 1

    if not windows:
        raise ValueError(
            "run_backtest produced no walk-forward windows; supply more games or "
            "lower n_splits/min_train_size"
        )

    model_probs_all = np.concatenate(all_model_probs)
    elo_probs_all_test = np.concatenate(all_elo_probs)
    home_probs_all = np.concatenate(all_home_probs)
    labels_all = np.concatenate(all_labels)

    aggregate: dict[str, Any] = {
        "n_windows": len(windows),
        "n_games": int(len(labels_all)),
        "model_brier": brier_score(model_probs_all, labels_all),
        "model_accuracy": accuracy(model_probs_all, labels_all),
        "model_calibration_error": calibration_error(model_probs_all, labels_all),
        "always_home_brier": brier_score(home_probs_all, labels_all),
        "always_home_accuracy": accuracy(home_probs_all, labels_all),
        "always_home_calibration_error": calibration_error(home_probs_all, labels_all),
        "elo_only_brier": brier_score(elo_probs_all_test, labels_all),
        "elo_only_accuracy": accuracy(elo_probs_all_test, labels_all),
        "elo_only_calibration_error": calibration_error(elo_probs_all_test, labels_all),
    }
    aggregate["brier_improvement_vs_always_home"] = (
        aggregate["always_home_brier"] - aggregate["model_brier"]
    )
    aggregate["brier_improvement_vs_elo_only"] = (
        aggregate["elo_only_brier"] - aggregate["model_brier"]
    )

    ci_by_game = cluster_bootstrap_ci(rows_by_game, n_resamples=n_resamples, confidence=confidence, seed=seed)
    ci_by_date = cluster_bootstrap_ci(rows_by_date, n_resamples=n_resamples, confidence=confidence, seed=seed)
    width_by_game = _ci_width(ci_by_game)
    width_by_date = _ci_width(ci_by_date)
    materially_different: Optional[bool] = None
    if width_by_game is not None and width_by_date is not None and width_by_game > 0:
        materially_different = (
            abs(width_by_date - width_by_game) / width_by_game
            > CLUSTERING_MATERIAL_DIFFERENCE_THRESHOLD
        )

    clustering_sensitivity = {
        "comparison_baseline": "elo_only",
        "by_game_id": ci_by_game,
        "by_slate_date": ci_by_date,
        "ci_width_by_game_id": width_by_game,
        "ci_width_by_slate_date": width_by_date,
        "materially_different": materially_different,
        "note": (
            "Compares the model's Brier improvement over elo_only -- not a market; none is "
            "available -- clustered two ways: one event per game (event_key=game_id) and one "
            "event per whole slate (event_key=game_date). NBA games are close to independent, "
            "so these are expected to be similar, unlike correlated weather brackets; both are "
            "reported so that expectation is demonstrated, not assumed."
        ),
    }

    if market_prices is None:
        market_comparison = None
        market_comparison_note = (
            "No market comparison was performed because no odds source was supplied. This "
            "repository has no historical NBA betting-line data; run_backtest measured model "
            "skill against baselines (always_home, elo_only) only. Pass market_prices "
            "(see load_market_prices) once a real odds source exists to enable a model-vs-market "
            "comparison."
        )
    elif n_market_matched == 0:
        market_comparison = {"n_games_matched": 0, "by_game_id": None, "by_slate_date": None}
        market_comparison_note = (
            "market_prices was supplied but none of its game_id values matched an "
            "out-of-sample game evaluated by this backtest; no comparison was computed."
        )
    else:
        market_comparison = {
            "n_games_matched": n_market_matched,
            "by_game_id": cluster_bootstrap_ci(
                market_rows_by_game, n_resamples=n_resamples, confidence=confidence, seed=seed
            ),
            "by_slate_date": cluster_bootstrap_ci(
                market_rows_by_date, n_resamples=n_resamples, confidence=confidence, seed=seed
            ),
        }
        market_comparison_note = (
            f"Paired, event-clustered model-vs-market Brier comparison computed over "
            f"{n_market_matched} out-of-sample games with supplied odds."
        )

    return {
        "scope": (
            "This backtest measures whether the NBA model has predictive skill against "
            "walk-forward baselines (always_home, elo_only) on real historical game outcomes. "
            "It does NOT establish that the model beats real market prices -- that requires "
            "market_prices from an actual odds source, which is absent by default."
        ),
        "n_games_total": len(sorted_games),
        "windows": windows,
        "aggregate": aggregate,
        "clustering_sensitivity": clustering_sensitivity,
        "market_comparison": market_comparison,
        "market_comparison_note": market_comparison_note,
    }
