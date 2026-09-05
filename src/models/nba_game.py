"""NBA game outcome model: P(home_team_wins) using ELO + team stats + rest.

Training: GradientBoostingClassifier with Platt scaling (CalibratedClassifierCV).
Inference: Always returns Platt-calibrated probabilities (D-05).
Persistence: ModelStore('nba_game') handles versioned joblib saves.

Feature vector (7 features):
    home_elo, away_elo, home_court, home_rest_days, away_rest_days,
    home_net_rating, away_net_rating

Usage::

    from src.models.nba_game import nba_game_model
    import numpy as np

    features = np.array([[1500.0, 1480.0, 1, 2, 0, 0.05, 0.03]])
    prob = nba_game_model.predict(features)   # P(home wins)
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
    "home_elo",         # EloTracker.get_elo(home_team_id), typical range 1200-1700
    "away_elo",         # EloTracker.get_elo(away_team_id)
    "home_court",       # 1 if home game, 0 if away
    "home_rest_days",   # days since last game, capped at 7
    "away_rest_days",   # days since last game, capped at 7
    "home_net_rating",  # team_stats OFF_RATING - DEF_RATING, range -15 to +15
    "away_net_rating",  # team_stats OFF_RATING - DEF_RATING
]


class NBAGameModel:
    """NBA game moneyline/spread prediction model.

    Wraps a Platt-calibrated GradientBoostingClassifier that predicts
    P(home_team_wins) from a 7-feature vector.

    Args:
        store_dir: Root directory for model persistence.
    """

    def __init__(self, store_dir: str = "data/models") -> None:
        self.store = ModelStore("nba_game", store_dir=store_dir)
        self._model: Optional[CalibratedClassifierCV] = None
        self._calibrator = None  # Stored separately for inspection

    def _load_model(self) -> None:
        """Load latest model from store. Raises ModelNotFoundError if none."""
        self._model, self._calibrator = self.store.load_latest()

    def predict(self, features: np.ndarray, context: str = "moneyline") -> np.ndarray:
        """Return calibrated P(home_team_wins) for each row in features.

        Args:
            features: Shape ``(n_games, 7)`` numpy array.  See
                :data:`FEATURE_NAMES`.
            context: ``'moneyline'`` (P home wins outright) or ``'spread'``
                (same model, returned for interface consistency).

        Returns:
            numpy array shape ``(n_games,)`` with values in ``[0.0, 1.0]``.

        Raises:
            ModelNotFoundError: If no trained model has been saved.
        """
        if self._model is None:
            self._load_model()
        # CalibratedClassifierCV wraps GBM; predict_proba returns [P(loss), P(win)]
        return self._model.predict_proba(features)[:, 1]

    def assemble_features(
        self,
        home_team_id: int,
        away_team_id: int,
        game_date,
        team_stats: dict,
        elo_tracker,
        schedule: list[dict] | None = None,
    ) -> np.ndarray:
        """Assemble a single-row feature vector for one game.

        Args:
            home_team_id: NBA team integer ID.
            away_team_id: NBA team integer ID.
            game_date: ``datetime.date`` object.
            team_stats: Dict keyed by team_id from ``get_team_stats()``.
            elo_tracker: :class:`~src.data.nba.teams.EloTracker` instance.
            schedule: List of game dicts for rest-day calculation.  If
                ``None``, rest days default to 1.

        Returns:
            numpy array shape ``(1, 7)``.  Safe to call ``predict()`` on.
        """
        from src.data.nba.teams import get_rest_days

        home_elo = elo_tracker.get_elo(home_team_id)
        away_elo = elo_tracker.get_elo(away_team_id)

        if schedule is not None:
            home_rest = min(get_rest_days(home_team_id, schedule, game_date), 7)
            away_rest = min(get_rest_days(away_team_id, schedule, game_date), 7)
        else:
            home_rest = 1
            away_rest = 1

        home_stats = team_stats.get(home_team_id, {})
        away_stats = team_stats.get(away_team_id, {})

        return np.array([[
            home_elo,
            away_elo,
            1.0,  # home_court — always 1 (caller assembles from home perspective)
            float(home_rest),
            float(away_rest),
            float(home_stats.get("NET_RATING", home_stats.get("net_rating", 0.0))),
            float(away_stats.get("NET_RATING", away_stats.get("net_rating", 0.0))),
        ]])

    def build_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Assemble the (X, y) training matrix from historical game results.

        Split out of :meth:`train` so :mod:`src.analytics.recalibrate` can
        shadow-train a candidate on identical features without calling
        ``train()`` -- which D-01 reserves for ``pipeline.py`` and which would
        persist a new version.  Building the data is not training.

        Returns:
            ``(X, y)`` with X shape ``(n, 7)`` matching :data:`FEATURE_NAMES`,
            ordered oldest game first.

        Raises:
            InsufficientDataError: If fewer than 200 historical games.
        """
        from src.data.nba.history import get_historical_results
        from src.data.nba.teams import EloTracker

        logger.info("Fetching historical NBA game results for training...")
        results = get_historical_results()
        if len(results) < 200:
            raise InsufficientDataError(
                f"Need at least 200 historical games; got {len(results)}",
                n_available=len(results),
                n_required=200,
            )

        # Build feature matrix from historical results
        tracker = EloTracker()
        rows = []
        labels = []

        # Sort by game_date ascending — critical for temporal ordering
        sorted_results = sorted(results, key=lambda r: r["game_date"])

        for r in sorted_results:
            home_id = r["home_team_id"]
            away_id = r["away_team_id"]
            home_elo = tracker.get_elo(home_id)
            away_elo = tracker.get_elo(away_id)

            rows.append([
                home_elo,
                away_elo,
                1.0,    # home_court
                1.0,    # home_rest (approximation — no schedule in historical data)
                1.0,    # away_rest
                0.0,    # home_net_rating (not available in historical training)
                0.0,    # away_net_rating
            ])
            labels.append(1 if r["home_win"] else 0)

            # Update ELO after recording features (no leakage)
            margin = abs(r["home_pts"] - r["away_pts"])
            if r["home_win"]:
                tracker.update(home_id, away_id, margin)
            else:
                tracker.update(away_id, home_id, margin)

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

        Fetches data from ``get_historical_results()`` and builds features
        using a rolling EloTracker updated sequentially (no future leakage).

        Returns:
            Dict with keys ``'n_train'``, ``'n_test'``, ``'test_brier'``,
            ``'version'``.

        Raises:
            InsufficientDataError: If fewer than 200 historical games.
        """
        X, y = self.build_training_data()

        # Simple temporal split: 80/20
        split_idx = int(len(X) * 0.8)
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]

        logger.info("Training GBM + Platt calibration on %d games...", len(X_train))
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
        logger.info("Test Brier score: %.4f", test_brier)

        data_hash = hashlib.md5(X.tobytes()).hexdigest()[:12]
        version = self.store.save(
            calibrated,
            calibrator=None,  # CalibratedClassifierCV already contains calibration
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
nba_game_model = NBAGameModel()
