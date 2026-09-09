"""End-to-end integration tests for Phase 3: prediction models + validation + guards.

Non-integration tests (class TestPhase3Unit) run in standard CI without any env vars.
Integration tests (class TestPhase3Integration) require KALSHI_INTEGRATION=true and
live NBA/NWS API access.

Test coverage:
    - All 5 model singletons importable
    - ModelStore save/load/rollback roundtrip
    - TemporalSplitter temporal ordering + holdout write-protection
    - WalkForwardValidator on synthetic NBA data
    - OverfittingGuards: all 6 checks enforced end-to-end
    - WeatherTempModel: fit_bias_correction() + predict_brackets() sum = 1.0
    - Pipeline CLI --status runs without error
"""

import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression


# ---------------------------------------------------------------------------
# Unit-level integration tests (no live API required)
# ---------------------------------------------------------------------------

class TestPhase3Unit:
    """Tests that verify Phase 3 components compose correctly using synthetic data."""

    def test_all_models_importable(self):
        """All 5 model singletons import without error."""
        from src.models.nba_game import nba_game_model
        from src.models.nba_totals import nba_totals_model
        from src.models.nba_props import nba_props_model
        from src.models.weather_temp import weather_temp_model
        from src.models.weather_precip import weather_precip_model
        assert nba_game_model is not None
        assert nba_totals_model is not None
        assert nba_props_model is not None
        assert weather_temp_model is not None
        assert weather_precip_model is not None

    def test_model_store_roundtrip(self, tmp_path):
        """ModelStore: save LogisticRegression -> load_latest -> predictions match."""
        from src.models.model_store import ModelStore

        store = ModelStore("nba_game", store_dir=str(tmp_path / "models"))

        X = np.random.RandomState(42).randn(50, 8)
        y = (X[:, 0] > 0).astype(int)
        model = LogisticRegression(random_state=42).fit(X, y)

        version = store.save(
            model,
            calibrator=None,
            metrics={"test_brier": 0.22, "n_train": 40, "n_test": 10},
            training_data_hash="abc123",
        )
        assert version == 1

        loaded_model, loaded_cal = store.load_latest()
        assert loaded_model is not None
        assert loaded_cal is None

        original_probs = model.predict_proba(X)[:, 1]
        loaded_probs = loaded_model.predict_proba(X)[:, 1]
        np.testing.assert_array_almost_equal(original_probs, loaded_probs, decimal=6)

    def test_model_store_rollback(self, tmp_path):
        """ModelStore.rollback() restores the previous version as active."""
        from src.models.model_store import ModelStore

        store = ModelStore("nba_game", store_dir=str(tmp_path / "models"))
        X = np.random.RandomState(0).randn(20, 3)
        y = (X[:, 0] > 0).astype(int)

        for _ in range(2):
            m = LogisticRegression().fit(X, y)
            store.save(m, None, {}, "hash")

        assert store.get_active_version() == 2
        store.rollback()
        assert store.get_active_version() == 1

    def test_model_store_bootstrap_mode(self, tmp_path):
        """is_bootstrap_mode() returns True when no version has been trained yet."""
        from src.models.model_store import ModelStore

        store = ModelStore("nba_game", store_dir=str(tmp_path / "models"))
        assert store.is_bootstrap_mode() is True

    def test_temporal_splitter_integration(self):
        """TemporalSplitter: temporal ordering preserved, holdout write-protected."""
        from src.validation.splitter import TemporalSplitter

        n = 100
        dates = pd.date_range("2022-01-01", periods=n, freq="D")
        X = np.random.randn(n, 8)
        y = np.random.randint(0, 2, n)

        splitter = TemporalSplitter(dates)
        (X_train, X_test), (y_train, y_test) = splitter.split(X, y)
        X_holdout, _ = splitter.holdout(X, y, allow_holdout=True)

        assert len(X_train) == 60
        assert len(X_test) == 20
        assert len(X_holdout) == 20
        assert len(y_train) == 60

        train_dates = dates[:60]
        test_dates = dates[60:80]
        holdout_dates = dates[80:]
        assert train_dates.max() < test_dates.min()
        assert test_dates.max() < holdout_dates.min()

        with pytest.raises(PermissionError):
            splitter.access_holdout()

        holdout_idx = splitter.access_holdout(allow_holdout=True)
        assert len(holdout_idx) == 20

    def test_walk_forward_on_synthetic_nba(self):
        """WalkForwardValidator yields valid per-window metrics on synthetic NBA data."""
        from src.validation.walk_forward import WalkForwardValidator
        from src.validation.metrics import brier_score

        n = 200
        rng = np.random.RandomState(42)
        X = rng.randn(n, 8)
        y = (X[:, 0] + rng.randn(n) * 0.5 > 0).astype(int)

        def train_fn(X_tr, y_tr):
            return LogisticRegression(random_state=42).fit(X_tr, y_tr)

        def test_fn(model, X_te, y_te):
            probs = model.predict_proba(X_te)[:, 1]
            return {"brier_score": brier_score(probs, y_te)}

        validator = WalkForwardValidator(n_splits=4)
        results = list(validator.validate(X, y, train_fn, test_fn))

        assert len(results) == 4
        for r in results:
            assert "window" in r
            assert "brier_score" in r["metrics"]
            assert 0.0 < r["metrics"]["brier_score"] < 1.0, "Brier score should be in (0, 1)"
            assert r["train_size"] > 0
            assert r["test_size"] > 0
            assert r["train_end_idx"] < r["test_start_idx"]

    def test_guard_system_end_to_end(self, tmp_path):
        """All 6 guard checks enforced; force_override() logs to improvements table."""
        from src.validation.guards import OverfittingGuards
        from src.models.exceptions import GuardViolation
        from src.db.database import Database

        db = Database(db_path=str(tmp_path / "test.db"))
        guards = OverfittingGuards(db=db)

        with pytest.raises(GuardViolation) as exc:
            guards.check_sample_size(23)
        assert exc.value.guard_name == "sample_size_gate"
        assert exc.value.details["n_oos"] == 23
        guards.check_sample_size(50)

        with pytest.raises(GuardViolation) as exc:
            guards.check_dampening(1.0, 1.25)
        assert exc.value.guard_name == "dampening"
        guards.check_dampening(1.0, 1.19)

        old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        with pytest.raises(GuardViolation) as exc:
            guards.check_staleness(old_date)
        assert exc.value.guard_name == "staleness"
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        guards.check_staleness(recent_date)

        with pytest.raises(GuardViolation) as exc:
            guards.check_significance(0.12)
        assert exc.value.guard_name == "significance"
        guards.check_significance(0.03)

        rng = np.random.RandomState(42)
        sample1 = rng.normal(0, 1, 100)
        sample2_similar = rng.normal(0, 1, 100)
        sample2_different = rng.normal(5, 1, 100)
        guards.check_regime(sample1, sample2_similar)
        with pytest.raises(GuardViolation) as exc:
            guards.check_regime(sample1, sample2_different)
        assert exc.value.guard_name == "regime_change"

        guards.force_override("sample_size_gate", "Initial deployment, bootstrap mode confirmed", "nba_game")
        rows = db.fetchall("SELECT * FROM improvements WHERE type = 'guard_override'")
        assert len(rows) == 1
        assert "sample_size_gate" in rows[0]["description"]
        assert rows[0]["risk_level"] == "high"
        assert rows[0]["status"] == "applied"

    def test_cooldown_guard_with_database(self, tmp_path):
        """check_cooldown() fires when the last applied improvement has < 30 post_apply_trades."""
        from src.validation.guards import OverfittingGuards
        from src.models.exceptions import GuardViolation
        from src.db.database import Database

        db = Database(db_path=str(tmp_path / "test.db"))
        guards = OverfittingGuards(db=db)

        guards.check_cooldown()  # no applied improvements yet, should pass

        db.execute(
            "INSERT INTO improvements (type, target, description, risk_level, auto_approvable, "
            "validated_on_n_samples, status, post_apply_trades, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("config_tweak", "kelly_fraction", "test", "low", 1, 60, "applied", 10, "2026-03-29T00:00:00Z"),
        )
        with pytest.raises(GuardViolation) as exc:
            guards.check_cooldown()
        assert exc.value.guard_name == "cooldown"
        assert exc.value.details["post_apply_trades"] == 10

        db.execute("UPDATE improvements SET post_apply_trades = 30 WHERE target = 'kelly_fraction'")
        guards.check_cooldown()

    def test_weather_temp_bracket_probs_sum_to_one(self, tmp_path):
        """fit_bias_correction() on an empty DB still produces valid sigma; predict_brackets() sums to 1.0."""
        from src.models.weather_temp import WeatherTempModel
        from unittest.mock import patch

        model = WeatherTempModel(db_path=str(tmp_path / "test.db"))
        model.fit_bias_correction(min_observations=0)

        assert "KNYC" in model.bias_offsets
        assert len(model.bias_offsets["KNYC"]) == 12

        with patch.object(model, "_get_nws_point_forecast", return_value=55.0):
            brackets = [(None, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, None)]
            probs = model.predict_brackets("KNYC", "2026-04-01", brackets=brackets)

        assert len(probs) == 6
        total = sum(probs.values())
        assert abs(total - 1.0) < 0.01, f"Bracket probs should sum to 1.0, got {total}"
        for label, p in probs.items():
            assert 0.0 <= p <= 1.0, f"Bracket '{label}' probability out of range: {p}"

    def test_pipeline_status_runs(self):
        """python -m src.models.pipeline --status exits 0."""
        result = subprocess.run(
            [sys.executable, "-m", "src.models.pipeline", "--status"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        assert result.returncode == 0, f"pipeline --status failed: {result.stderr}"
        assert "nba_game" in result.stdout

    def test_holdout_never_accessed_during_training(self):
        """TemporalSplitter holdout protection ensures a training loop cannot access holdout."""
        from src.validation.splitter import TemporalSplitter

        n = 100
        dates = pd.date_range("2022-01-01", periods=n, freq="D")
        X = np.random.randn(n, 8)
        y = np.random.randint(0, 2, n)
        splitter = TemporalSplitter(dates)
        (X_train, X_test), (y_train, y_test) = splitter.split(X, y)

        # The ordinary API must not hand the holdout over. This assertion is the
        # point of the test: previously split() returned the holdout arrays, so
        # a training loop already held them before any gate was consulted.
        assert len(X_train) + len(X_test) == 80
        assert splitter.holdout_accessed is False

        def malicious_training_loop():
            return splitter.access_holdout()  # raises PermissionError

        with pytest.raises(PermissionError):
            malicious_training_loop()
        with pytest.raises(PermissionError):
            splitter.holdout(X, y)
        assert splitter.holdout_accessed is False

        holdout_idx = splitter.access_holdout(allow_holdout=True)
        X_holdout_explicit = X[holdout_idx]
        assert len(X_holdout_explicit) > 0
        assert splitter.holdout_accessed is True


# ---------------------------------------------------------------------------
# Integration tests (require live APIs)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPhase3Integration:
    """Integration tests requiring KALSHI_INTEGRATION=true and live data."""

    def test_nba_game_model_predict_on_todays_games(self, skip_without_integration):
        """NBA game model produces calibrated predictions for tonight's schedule."""
        from src.models.nba_game import NBAGameModel
        from src.models.exceptions import ModelNotFoundError
        from src.data.nba.teams import get_todays_schedule, get_team_stats, EloTracker

        model = NBAGameModel()
        schedule = get_todays_schedule()
        if not schedule:
            pytest.skip("No NBA games today")

        team_stats_list = get_team_stats()
        team_stats = {s["team_id"]: s for s in team_stats_list}
        tracker = EloTracker()

        game = schedule[0]
        features = model.assemble_features(
            game["home_team_id"], game["away_team_id"],
            game["game_date"], team_stats, tracker,
        )
        try:
            probs = model.predict(features)
            assert 0.0 <= probs[0] <= 1.0
        except ModelNotFoundError:
            pytest.skip("No trained NBA game model -- run pipeline --train first")

    def test_weather_temp_model_predict_brackets_nyc(self, skip_without_integration):
        """Weather temp model produces bracket probabilities summing to 1.0 for NYC."""
        from src.models.weather_temp import WeatherTempModel
        import datetime

        model = WeatherTempModel()
        model.fit_bias_correction(min_observations=0)
        forecast_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        probs = model.predict_brackets("KNYC", forecast_date)
        total = sum(probs.values())
        assert abs(total - 1.0) < 0.05

    def test_walk_forward_validation_on_nba_history(self, skip_without_integration):
        """Walk-forward validator runs on NBA historical data without errors."""
        from src.data.nba.history import get_historical_results
        from src.validation.walk_forward import WalkForwardValidator
        from src.validation.metrics import brier_score

        results = get_historical_results()
        if len(results) < 50:
            pytest.skip("Insufficient historical NBA data")

        X = np.zeros((len(results), 2))
        for i, r in enumerate(results):
            X[i, 0] = 1500.0  # placeholder ELO
            X[i, 1] = float(r.get("home_win", 0))
        y = np.array([int(r.get("home_win", 0)) for r in results])

        def train_fn(X_tr, y_tr):
            return LogisticRegression(random_state=42).fit(X_tr[:, :1], y_tr)

        def test_fn(model, X_te, y_te):
            probs = model.predict_proba(X_te[:, :1])[:, 1]
            return {"brier_score": brier_score(probs, y_te)}

        validator = WalkForwardValidator(n_splits=3)
        walk_results = list(validator.validate(X[:, :1], y, train_fn, test_fn))
        assert len(walk_results) == 3
