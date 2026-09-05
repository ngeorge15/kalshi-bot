"""Shadow recalibration: would retraining help, and by how much (R10.5)?

**This module never persists a model.**  Phase 3's D-01 makes ``pipeline.py`` the
only code path allowed to call a model's ``train()``, so that every version that
reaches disk has passed :class:`~src.validation.guards.OverfittingGuards`.  An
analytics command that could quietly swap the live model would defeat that.

So recalibration here is a *comparison*, not a deployment (D5-01): fit a candidate
in memory on the same features the incumbent was built from, score both on the
same held-back test split, and return a recommendation.  Acting on it is a
separate, deliberate ``python -m src.models.pipeline --train`` by a human or an
approved Phase 6 improvement.

**The holdout is never touched.**  :class:`~src.validation.splitter.TemporalSplitter`
write-protects it, and R10.5 is explicit that comparison happens on the test set.
Scoring a candidate on the holdout would burn the one clean sample the project
keeps for final validation.

Usage:
    from src.analytics.recalibrate import shadow_recalibrate

    report = shadow_recalibrate("nba_game")
    if report["recommendation"] == "retrain":
        ...  # a human runs pipeline.py --train
"""

import logging

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier

from src.models.exceptions import ModelNotFoundError
from src.validation.metrics import brier_score
from src.validation.walk_forward import WalkForwardValidator

logger = logging.getLogger(__name__)

# A candidate must beat the incumbent's Brier score by at least this much before
# retraining is recommended.  Brier differences below this are not distinguishable
# from resampling noise at the sample sizes this project trains on, and acting on
# them is how a model gets fitted to its own test split.
MIN_BRIER_IMPROVEMENT = 0.005

# Fraction of the (temporally ordered) data used for fitting; the remainder is the
# comparison test split.  Matches the 80/20 split the models' own train() uses.
TRAIN_FRACTION = 0.8


def _load_model_and_data(model_name: str):
    """Return ``(incumbent, X, y)`` for a GBM-backed model store.

    Uses each model's ``build_training_data()`` -- extracted from ``train()`` in
    05-03 precisely so the candidate is fitted on identical features to the
    incumbent.  Rebuilding features here instead would make the comparison
    meaningless the moment the two drifted apart.

    Raises:
        ValueError: If *model_name* is not a GBM-backed store.
        ModelNotFoundError: If no version of the model has been trained.
    """
    if model_name == "nba_game":
        from src.models.nba_game import NBAGameModel

        model = NBAGameModel()
        X, y = model.build_training_data()
    elif model_name == "nba_totals":
        from src.models.nba_totals import NBATotalsModel

        model = NBATotalsModel()
        X, y = model.build_training_data()
    elif model_name.startswith("nba_props_"):
        from src.models.nba_props import NBAPropsModel

        prop_type = model_name[len("nba_props_") :]
        model = NBAPropsModel()
        X, y = model.build_training_data(prop_type=prop_type)
    else:
        from src.analytics.performance import GBM_MODEL_STORES

        raise ValueError(
            f"{model_name} cannot be recalibrated. GBM-backed stores: {GBM_MODEL_STORES}"
        )

    from src.models.model_store import ModelStore

    store = ModelStore(model_name)
    incumbent, _ = store.load_latest()
    return incumbent, X, y


def fit_candidate(
    X_train: np.ndarray,
    y_train: np.ndarray,
    n_estimators: int = 100,
    max_depth: int = 3,
    learning_rate: float = 0.1,
) -> CalibratedClassifierCV:
    """Fit an in-memory candidate matching the models' own architecture.

    Same estimator, calibration method and ``random_state`` the models use, so a
    Brier difference reflects the newer data rather than a different setup.

    Returns:
        A fitted ``CalibratedClassifierCV``. Never persisted (D5-01).
    """
    gbm = GradientBoostingClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        random_state=42,
    )
    candidate = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
    candidate.fit(X_train, y_train)
    return candidate


def compare_on_test_set(
    incumbent,
    candidate,
    X_test: np.ndarray,
    y_test: np.ndarray,
    min_improvement: float = MIN_BRIER_IMPROVEMENT,
) -> dict:
    """Score both models on the same test split and decide (R10.5).

    Args:
        incumbent: The currently active fitted model.
        candidate: The freshly fitted shadow model.
        X_test: Test features -- the test split, never the holdout.
        y_test: Test labels.
        min_improvement: Brier improvement required to recommend retraining.

    Returns:
        Dict with ``incumbent_brier``, ``candidate_brier``, ``improvement``
        (positive means the candidate is better, since lower Brier is better),
        ``improved`` and ``recommendation`` (``"retrain"`` or ``"hold"``).
    """
    incumbent_probs = incumbent.predict_proba(X_test)[:, 1]
    candidate_probs = candidate.predict_proba(X_test)[:, 1]

    incumbent_brier = brier_score(incumbent_probs, y_test)
    candidate_brier = brier_score(candidate_probs, y_test)
    improvement = incumbent_brier - candidate_brier
    improved = improvement >= min_improvement

    return {
        "incumbent_brier": incumbent_brier,
        "candidate_brier": candidate_brier,
        "improvement": improvement,
        "min_improvement": min_improvement,
        "improved": improved,
        "recommendation": "retrain" if improved else "hold",
    }


def shadow_recalibrate(
    model_name: str,
    run_walk_forward: bool = True,
    min_improvement: float = MIN_BRIER_IMPROVEMENT,
) -> dict:
    """Compare a freshly fitted candidate against the active model (R10.5).

    Fits in memory and **never calls** ``ModelStore.save()`` or any model's
    ``train()`` (D5-01).  The returned ``recommendation`` is advisory; applying it
    means a separate ``pipeline.py --train`` run, where the overfitting guards
    apply.

    Args:
        model_name: A GBM-backed model store name.
        run_walk_forward: Also run rolling-window validation on the candidate
            (R10.5 asks for it).  Skippable because it refits per window.
        min_improvement: Brier improvement required to recommend retraining.

    Returns:
        Dict with ``model_name``, ``n_train``, ``n_test``, the comparison fields
        from :func:`compare_on_test_set`, ``walk_forward`` (or ``None``),
        ``persisted`` (always ``False``) and ``holdout_used`` (always ``False``).

    Raises:
        ValueError: If the model is not GBM-backed, or too few samples to split.
        ModelNotFoundError: If the model has never been trained.
    """
    incumbent, X, y = _load_model_and_data(model_name)

    split_idx = int(len(X) * TRAIN_FRACTION)
    if split_idx < 1 or split_idx >= len(X):
        raise ValueError(
            f"Not enough samples to split for recalibration: n={len(X)}"
        )

    # Temporal split -- X and y arrive oldest-first from build_training_data().
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    logger.info(
        "Shadow-recalibrating %s on %d train / %d test samples (nothing will be saved)",
        model_name,
        len(X_train),
        len(X_test),
    )
    candidate = fit_candidate(X_train, y_train)
    report = compare_on_test_set(incumbent, candidate, X_test, y_test, min_improvement)

    walk_forward = None
    if run_walk_forward:
        walk_forward = walk_forward_report(X, y)

    report.update(
        {
            "model_name": model_name,
            "n_train": len(X_train),
            "n_test": len(X_test),
            "walk_forward": walk_forward,
            "persisted": False,
            "holdout_used": False,
        }
    )
    logger.info(
        "%s: incumbent Brier %.4f vs candidate %.4f -> %s",
        model_name,
        report["incumbent_brier"],
        report["candidate_brier"],
        report["recommendation"],
    )
    return report


def walk_forward_report(X: np.ndarray, y: np.ndarray) -> dict:
    """Rolling-window validation of the candidate architecture (R10.5).

    Delegates to :class:`~src.validation.walk_forward.WalkForwardValidator` so
    analytics and the validation layer use one implementation.

    Returns:
        Dict with ``n_windows``, ``mean_brier``, ``std_brier`` and ``windows``.
        ``n_windows`` is 0 with ``None`` statistics when the series is too short
        for even one window -- not an error, just not enough history.
    """

    def train_fn(X_tr, y_tr):
        return fit_candidate(X_tr, y_tr)

    def test_fn(model, X_te, y_te):
        # WalkForwardValidator's contract is metrics-dict-returning, not scalar.
        return {"brier": brier_score(model.predict_proba(X_te)[:, 1], y_te)}

    validator = WalkForwardValidator()
    try:
        # validate() is a generator, so errors surface on consumption, not on
        # the call -- materialise inside the guard.
        windows = list(validator.validate(X, y, train_fn, test_fn))
    except (ValueError, IndexError) as exc:
        logger.warning("Walk-forward validation could not run: %s", exc)
        return {"n_windows": 0, "mean_brier": None, "std_brier": None, "windows": []}

    scores = [w["metrics"]["brier"] for w in windows]
    if not scores:
        return {"n_windows": 0, "mean_brier": None, "std_brier": None, "windows": []}

    return {
        "n_windows": len(scores),
        "mean_brier": float(np.mean(scores)),
        "std_brier": float(np.std(scores, ddof=1)) if len(scores) > 1 else 0.0,
        "windows": windows,
    }


def recalibrate_all(model_names: tuple[str, ...] | None = None) -> dict[str, dict]:
    """Shadow-recalibrate every GBM-backed model that has been trained.

    Models with no saved version are skipped with a note rather than raising --
    a partially bootstrapped project is the normal state early on.

    Returns:
        Dict keyed by model name. Skipped models carry ``{"skipped": reason}``.
    """
    from src.analytics.performance import GBM_MODEL_STORES

    names = model_names if model_names is not None else GBM_MODEL_STORES
    results: dict[str, dict] = {}
    for name in names:
        try:
            results[name] = shadow_recalibrate(name)
        except ModelNotFoundError:
            results[name] = {"skipped": "no trained version"}
            logger.info("Skipping %s: never trained", name)
        except Exception as exc:  # noqa: BLE001 - one bad model must not stop the sweep
            results[name] = {"skipped": f"{type(exc).__name__}: {exc}"}
            logger.warning("Skipping %s: %s", name, exc)
    return results
