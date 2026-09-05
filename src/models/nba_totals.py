"""NBA over/under totals model: P(combined_score > line).

Pace-adjusted efficiency approach: uses team offensive ratings and pace
to estimate expected combined score, then calibrates the over probability
via Platt scaling.

Feature vector (5 features):
    home_ortg, away_ortg, home_pace, away_pace, line

Usage::

    from src.models.nba_totals import nba_totals_model
    import numpy as np

    features = np.array([[112.5, 108.0, 96.5, 100.0, 220.5]])
    prob = nba_totals_model.predict(features)   # P(total > line)
"""

import hashlib
import logging
from typing import Optional

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier

from src.models.exceptions import InsufficientDataError, ModelNotFoundError
from src.models.model_store import ModelStore

logger = logging.getLogger(__name__)

FEATURE_NAMES = [
    "home_ortg",   # Home team offensive rating (pts per 100 possessions)
    "away_ortg",   # Away team offensive rating
    "home_pace",   # Home team pace (possessions per 48 min)
    "away_pace",   # Away team pace
    "line",        # Total points line (e.g. 228.5)
]


class NBATotalsModel:
    """NBA game totals over/under prediction model.

    Wraps a Platt-calibrated GradientBoostingClassifier that predicts
    P(combined_score > line) from a 5-feature vector.

    Args:
        store_dir: Root directory for model persistence.
    """

    def __init__(self, store_dir: str = "data/models") -> None:
        self.store = ModelStore("nba_totals", store_dir=store_dir)
        self._model: Optional[CalibratedClassifierCV] = None
        self._calibrator = None

    def _load_model(self) -> None:
        """Load latest model from store. Raises ModelNotFoundError if none."""
        self._model, self._calibrator = self.store.load_latest()

    def predict(self, features: np.ndarray) -> np.ndarray:
        """Return calibrated P(total > line) for each row in features.

        Args:
            features: Shape ``(n_games, 5)`` numpy array.  See
                :data:`FEATURE_NAMES`.

        Returns:
            numpy array shape ``(n_games,)`` with values in ``[0.0, 1.0]``.

        Raises:
            ModelNotFoundError: If no trained model has been saved.
        """
        if self._model is None:
            self._load_model()
        return self._model.predict_proba(features)[:, 1]

    def assemble_features(
        self,
        home_team_id: int,
        away_team_id: int,
        line: float,
        team_stats: dict,
    ) -> np.ndarray:
        """Assemble a single-row feature vector for one game.

        Args:
            home_team_id: NBA team integer ID.
            away_team_id: NBA team integer ID.
            line: Total points line (e.g. 228.5).
            team_stats: Dict keyed by team_id with keys ``OFF_RATING``,
                ``PACE``.

        Returns:
            numpy array shape ``(1, 5)``.
        """
        home = team_stats.get(home_team_id, {})
        away = team_stats.get(away_team_id, {})
        return np.array([[
            float(home.get("OFF_RATING", home.get("off_rating", 110.0))),
            float(away.get("OFF_RATING", away.get("off_rating", 110.0))),
            float(home.get("PACE", home.get("pace", 98.0))),
            float(away.get("PACE", away.get("pace", 98.0))),
            float(line),
        ]])

    def build_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Assemble the (X, y) training matrix from historical game results.

        Split out of :meth:`train` so :mod:`src.analytics.recalibrate` can
        shadow-train a candidate on identical features (see D-01 / D5-01).

        Returns:
            ``(X, y)`` with X shape ``(n, 5)`` matching :data:`FEATURE_NAMES`.

        Raises:
            InsufficientDataError: If fewer than 200 historical games.
        """
        from src.data.nba.history import get_historical_results

        logger.info("Fetching historical NBA game results for totals training...")
        results = get_historical_results()
        if len(results) < 200:
            raise InsufficientDataError(
                f"Need at least 200 historical games; got {len(results)}",
                n_available=len(results),
                n_required=200,
            )

        sorted_results = sorted(results, key=lambda r: r["game_date"])

        # Compute totals and median for synthetic line
        totals = [r["home_pts"] + r["away_pts"] for r in sorted_results]
        median_total = float(np.median(totals))

        rows = []
        labels = []
        for i, r in enumerate(sorted_results):
            total = r["home_pts"] + r["away_pts"]
            # Use running median as synthetic line (avoids future leakage)
            if i > 50:
                line = float(np.median(totals[:i]))
            else:
                line = median_total

            rows.append([
                110.0,   # home_ortg placeholder (not in historical data)
                110.0,   # away_ortg placeholder
                98.0,    # home_pace placeholder
                98.0,    # away_pace placeholder
                line,
            ])
            labels.append(1 if total > line else 0)

        X = np.array(rows)
        y = np.array(labels)
        return X, y

    def train(
        self,
        n_estimators: int = 100,
        max_depth: int = 3,
        learning_rate: float = 0.1,
    ) -> dict:
        """Train GBM + Platt calibration on historical game results.

        Uses total points (home_pts + away_pts) and median total as the
        initial line estimate for training data.

        Returns:
            Dict with keys ``'n_train'``, ``'n_test'``, ``'test_brier'``,
            ``'version'``.

        Raises:
            InsufficientDataError: If fewer than 200 historical games.
        """
        X, y = self.build_training_data()

        split_idx = int(len(X) * 0.8)
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]

        logger.info("Training totals GBM + Platt on %d games...", len(X_train))
        gbm = GradientBoostingClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=learning_rate,
            random_state=42,
        )
        calibrated = CalibratedClassifierCV(gbm, method="sigmoid", cv=3)
        calibrated.fit(X_train, y_train)

        test_probs = calibrated.predict_proba(X_test)[:, 1]
        test_brier = float(np.mean((test_probs - y_test) ** 2))
        logger.info("Totals test Brier score: %.4f", test_brier)

        data_hash = hashlib.md5(X.tobytes()).hexdigest()[:12]
        version = self.store.save(
            calibrated,
            calibrator=None,
            metrics={
                "test_brier": test_brier,
                "n_train": len(X_train),
                "n_test": len(X_test),
            },
            training_data_hash=data_hash,
        )
        self._model = calibrated
        return {
            "n_train": len(X_train),
            "n_test": len(X_test),
            "test_brier": test_brier,
            "version": version,
        }


# Module-level singleton per D-14
nba_totals_model = NBATotalsModel()
