"""NBA player prop model: P(stat > line) for pts, reb, ast, 3pm.

Each prop type has its own trained GBM stored under a separate ModelStore
name (``nba_props_pts``, ``nba_props_reb``, etc.).

Feature vector (7 features per prop type):
    season_avg, last5_avg, last5_max, opp_def_rank_vs_pos, minutes_proj, line, home

Usage::

    from src.models.nba_props import nba_props_model
    import numpy as np

    features = np.array([[22.3, 20.1, 24.5, 0.8, 35.0, 85.0, 1]])
    prob = nba_props_model.predict(features, prop_type="pts")
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

FEATURE_NAMES = {
    "pts": [
        "season_avg_pts", "last5_avg_pts", "last5_max_pts",
        "opp_def_rank_vs_pos", "minutes_proj", "line", "home",
    ],
    "reb": [
        "season_avg_reb", "last5_avg_reb", "last5_max_reb",
        "opp_def_rank_vs_pos", "minutes_proj", "line", "home",
    ],
    "ast": [
        "season_avg_ast", "last5_avg_ast", "last5_max_ast",
        "opp_def_rank_vs_pos", "minutes_proj", "line", "home",
    ],
    "3pm": [
        "season_avg_3pm", "last5_avg_3pm", "last5_max_3pm",
        "opp_def_rank_vs_pos", "minutes_proj", "line", "home",
    ],
}

# Mapping from prop_type to the stat key used in player stats / game logs
_STAT_KEY = {
    "pts": "PTS",
    "reb": "REB",
    "ast": "AST",
    "3pm": "FG3M",
}


class NBAPropsModel:
    """NBA player prop prediction model supporting pts, reb, ast, 3pm.

    Each prop type maintains a separate model via ModelStore.

    Args:
        store_dir: Root directory for model persistence.
    """

    PROP_TYPES = ("pts", "reb", "ast", "3pm")

    def __init__(self, store_dir: str = "data/models") -> None:
        self.store_dir = store_dir
        self._models: dict[str, Optional[CalibratedClassifierCV]] = {
            pt: None for pt in self.PROP_TYPES
        }

    def predict(self, features: np.ndarray, prop_type: str = "pts") -> np.ndarray:
        """Return P(stat > line) for the given prop_type.

        Args:
            features: Shape ``(n_players, 7)``.  See
                ``FEATURE_NAMES[prop_type]``.
            prop_type: One of ``('pts', 'reb', 'ast', '3pm')``.

        Returns:
            numpy array shape ``(n_players,)`` with values in ``[0.0, 1.0]``.

        Raises:
            ModelNotFoundError: If no trained model for this prop_type.
            ValueError: If prop_type is not in :data:`PROP_TYPES`.
        """
        if prop_type not in self.PROP_TYPES:
            raise ValueError(
                f"prop_type must be one of {self.PROP_TYPES}, got '{prop_type}'"
            )

        if self._models.get(prop_type) is None:
            model_name = f"nba_props_{prop_type}"
            store = ModelStore(model_name, store_dir=self.store_dir)
            model, _ = store.load_latest()  # Raises ModelNotFoundError if not found
            self._models[prop_type] = model

        return self._models[prop_type].predict_proba(features)[:, 1]

    def assemble_features(
        self,
        player_id: int,
        opp_team_id: int,
        line: float,
        home: bool,
        prop_type: str = "pts",
    ) -> tuple[np.ndarray, str]:
        """Assemble feature vector for one player.

        Args:
            player_id: NBA player ID.
            opp_team_id: Opponent team ID.
            line: Prop line (e.g. 24.5 for pts).
            home: Whether the player's team is at home.
            prop_type: One of :data:`PROP_TYPES`.

        Returns:
            ``(features_array, confidence)`` where ``confidence`` is
            ``'high'``, ``'medium'``, or ``'low'``.
            ``confidence='low'`` when player is GTD/questionable (D-08).
        """
        from src.data.nba.players import (
            get_injury_report,
            get_matchup_context,
            get_player_game_logs,
            get_player_stats,
        )

        if prop_type not in self.PROP_TYPES:
            raise ValueError(
                f"prop_type must be one of {self.PROP_TYPES}, got '{prop_type}'"
            )

        stat_key = _STAT_KEY[prop_type]
        stat_key_lower = stat_key.lower()

        # Season averages
        all_stats = get_player_stats()
        player_stats = {}
        for ps in all_stats:
            pid = ps.get("PLAYER_ID", ps.get("player_id"))
            if pid == player_id:
                player_stats = ps
                break

        season_avg = float(
            player_stats.get(stat_key, player_stats.get(stat_key_lower, 0.0))
        )
        minutes_avg = float(
            player_stats.get("MIN", player_stats.get("min", 30.0))
        )

        # Last 5 games
        game_logs = get_player_game_logs(player_id, last_n=5)
        if game_logs:
            recent_vals = [
                float(g.get(stat_key, g.get(stat_key_lower, 0.0)))
                for g in game_logs
            ]
            last5_avg = float(np.mean(recent_vals))
            last5_max = float(np.max(recent_vals))
        else:
            last5_avg = season_avg
            last5_max = season_avg

        # Matchup context
        matchup = get_matchup_context(player_id, opp_team_id)
        opp_rank = float(matchup.get("position_rank", 15))
        # Normalize rank to 0-1 (1 = worst defense vs position = good for player)
        opp_def_factor = opp_rank / 30.0

        # Injury confidence
        confidence = "high"
        injuries = get_injury_report()
        for inj in injuries:
            inj_pid = inj.get("PLAYER_ID", inj.get("player_id"))
            if inj_pid == player_id:
                status = inj.get("status", "").upper()
                if status in ("OUT", "DOUBTFUL"):
                    confidence = "low"
                elif status in ("QUESTIONABLE", "GTD", "GAME TIME DECISION"):
                    confidence = "low"
                elif status == "PROBABLE":
                    confidence = "medium"
                break

        features = np.array([[
            season_avg,
            last5_avg,
            last5_max,
            opp_def_factor,
            minutes_avg,
            line,
            1.0 if home else 0.0,
        ]])

        return features, confidence

    def build_training_data(self, prop_type: str = "pts") -> tuple[np.ndarray, np.ndarray]:
        """Assemble the (X, y) training matrix for one prop type.

        Split out of :meth:`train` so :mod:`src.analytics.recalibrate` can
        shadow-train a candidate on identical features (see D-01 / D5-01).

        Returns:
            ``(X, y)`` with X shape ``(n, 7)`` matching ``FEATURE_NAMES[prop_type]``.

        Raises:
            InsufficientDataError: If fewer than 100 training samples.
            ValueError: If prop_type is invalid.
        """
        if prop_type not in self.PROP_TYPES:
            raise ValueError(
                f"prop_type must be one of {self.PROP_TYPES}, got '{prop_type}'"
            )

        from src.data.nba.players import get_player_stats, get_player_game_logs

        stat_key = _STAT_KEY[prop_type]
        stat_key_lower = stat_key.lower()

        logger.info("Fetching player data for %s prop training...", prop_type)
        all_players = get_player_stats()

        # Build training data from recent game logs
        rows = []
        labels = []

        for player in all_players:
            pid = player.get("PLAYER_ID", player.get("player_id"))
            if pid is None:
                continue

            season_avg = float(
                player.get(stat_key, player.get(stat_key_lower, 0.0))
            )
            minutes = float(
                player.get("MIN", player.get("min", 0.0))
            )

            # Skip low-minute players
            if minutes < 15.0:
                continue

            game_logs = get_player_game_logs(pid, last_n=20)
            if len(game_logs) < 5:
                continue

            vals = [
                float(g.get(stat_key, g.get(stat_key_lower, 0.0)))
                for g in game_logs
            ]
            player_median = float(np.median(vals))

            # Each game becomes a training sample
            for i, gl in enumerate(game_logs):
                actual = float(gl.get(stat_key, gl.get(stat_key_lower, 0.0)))
                if i >= 5:
                    last5 = vals[i - 5:i]
                else:
                    last5 = vals[:i] if i > 0 else [season_avg]

                rows.append([
                    season_avg,
                    float(np.mean(last5)),
                    float(np.max(last5)) if last5 else season_avg,
                    0.5,     # opp_def_factor placeholder
                    minutes,
                    player_median,  # Use player median as synthetic line
                    0.5,     # home placeholder
                ])
                labels.append(1 if actual > player_median else 0)

        if len(rows) < 100:
            raise InsufficientDataError(
                f"Need at least 100 training samples for {prop_type}; got {len(rows)}",
                n_available=len(rows),
                n_required=100,
            )

        X = np.array(rows)
        y = np.array(labels)
        return X, y

    def train(
        self,
        prop_type: str = "pts",
        n_estimators: int = 100,
        max_depth: int = 3,
        learning_rate: float = 0.1,
    ) -> dict:
        """Train a GBM model for the specified prop type.

        Uses player game logs from the data pipeline to build training data.
        Each game log entry becomes a training sample where label = 1 if the
        player exceeded the median stat value for that prop type.

        Args:
            prop_type: One of :data:`PROP_TYPES`.
            n_estimators: GBM number of estimators.
            max_depth: GBM max tree depth.
            learning_rate: GBM learning rate.

        Returns:
            Dict with keys ``'n_train'``, ``'n_test'``, ``'test_brier'``,
            ``'version'``.

        Raises:
            InsufficientDataError: If fewer than 100 training samples.
            ValueError: If prop_type is invalid.
        """
        X, y = self.build_training_data(prop_type=prop_type)

        split_idx = int(len(X) * 0.8)
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]

        logger.info(
            "Training props %s GBM + Platt on %d samples...",
            prop_type,
            len(X_train),
        )
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
        logger.info("Props %s test Brier: %.4f", prop_type, test_brier)

        model_name = f"nba_props_{prop_type}"
        store = ModelStore(model_name, store_dir=self.store_dir)
        data_hash = hashlib.md5(X.tobytes()).hexdigest()[:12]
        version = store.save(
            calibrated,
            calibrator=None,
            metrics={
                "test_brier": test_brier,
                "n_train": len(X_train),
                "n_test": len(X_test),
            },
            training_data_hash=data_hash,
        )
        self._models[prop_type] = calibrated
        return {
            "n_train": len(X_train),
            "n_test": len(X_test),
            "test_brier": test_brier,
            "version": version,
        }


# Module-level singleton per D-14
nba_props_model = NBAPropsModel()
