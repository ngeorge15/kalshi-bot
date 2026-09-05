"""Tests for src/analytics/recalibrate.py (05-04, R10.5).

The load-and-compare path is exercised against a stubbed data loader: the real
one calls ``build_training_data()``, which pulls historical NBA data over the
network. The behaviour under test is the comparison and the no-persist
guarantee, not the data fetch.
"""

import numpy as np
import pytest

from src.analytics import recalibrate as rc
from src.models.exceptions import ModelNotFoundError


@pytest.fixture(scope="module")
def dataset():
    """Separable-ish binary data, temporally ordered, big enough to split."""
    rng = np.random.RandomState(0)
    X = rng.rand(400, 4)
    y = (X[:, 0] + rng.normal(0, 0.15, 400) > 0.5).astype(int)
    return X, y


@pytest.fixture(scope="module")
def fitted(dataset):
    X, y = dataset
    return rc.fit_candidate(X[:320], y[:320])


class TestFitCandidate:
    def test_returns_calibrated_model(self, fitted):
        from sklearn.calibration import CalibratedClassifierCV

        assert isinstance(fitted, CalibratedClassifierCV)

    def test_predicts_probabilities_in_range(self, fitted, dataset):
        X, _ = dataset
        probs = fitted.predict_proba(X[320:])[:, 1]
        assert probs.min() >= 0.0 and probs.max() <= 1.0

    def test_is_deterministic(self, dataset):
        X, y = dataset
        a = rc.fit_candidate(X[:200], y[:200]).predict_proba(X[200:210])[:, 1]
        b = rc.fit_candidate(X[:200], y[:200]).predict_proba(X[200:210])[:, 1]
        assert a == pytest.approx(b)


class TestCompareOnTestSet:
    def test_better_candidate_recommends_retrain(self, dataset):
        X, y = dataset
        weak = rc.fit_candidate(X[:60], y[:60], n_estimators=2, max_depth=1)
        strong = rc.fit_candidate(X[:320], y[:320])
        report = rc.compare_on_test_set(weak, strong, X[320:], y[320:])
        assert report["candidate_brier"] < report["incumbent_brier"]
        assert report["improved"] is True
        assert report["recommendation"] == "retrain"

    def test_identical_models_hold(self, fitted, dataset):
        X, y = dataset
        report = rc.compare_on_test_set(fitted, fitted, X[320:], y[320:])
        assert report["improvement"] == pytest.approx(0.0)
        assert report["recommendation"] == "hold"

    def test_worse_candidate_holds(self, dataset):
        X, y = dataset
        strong = rc.fit_candidate(X[:320], y[:320])
        weak = rc.fit_candidate(X[:60], y[:60], n_estimators=2, max_depth=1)
        report = rc.compare_on_test_set(strong, weak, X[320:], y[320:])
        assert report["improvement"] < 0
        assert report["recommendation"] == "hold"

    def test_marginal_improvement_holds(self, fitted, dataset):
        """Below-threshold gains are noise; acting on them is how you overfit."""
        X, y = dataset
        report = rc.compare_on_test_set(
            fitted, fitted, X[320:], y[320:], min_improvement=0.0
        )
        assert report["improved"] is True   # threshold 0 accepts a tie
        report = rc.compare_on_test_set(fitted, fitted, X[320:], y[320:])
        assert report["improved"] is False  # default threshold rejects it

    def test_threshold_is_reported(self, fitted, dataset):
        X, y = dataset
        report = rc.compare_on_test_set(fitted, fitted, X[320:], y[320:])
        assert report["min_improvement"] == rc.MIN_BRIER_IMPROVEMENT


class TestWalkForward:
    def test_reports_windows(self, dataset):
        X, y = dataset
        report = rc.walk_forward_report(X, y)
        assert report["n_windows"] > 0
        assert 0.0 <= report["mean_brier"] <= 1.0
        assert len(report["windows"]) == report["n_windows"]

    def test_windows_carry_metrics_dict(self, dataset):
        """WalkForwardValidator's contract yields metrics as a dict, not a scalar."""
        X, y = dataset
        report = rc.walk_forward_report(X, y)
        assert "brier" in report["windows"][0]["metrics"]

    def test_too_little_data_returns_empty_not_raises(self):
        X = np.random.RandomState(1).rand(4, 4)
        y = np.array([0, 1, 0, 1])
        report = rc.walk_forward_report(X, y)
        assert report["n_windows"] == 0
        assert report["mean_brier"] is None


class TestShadowRecalibrate:
    """D5-01: compare, never deploy."""

    def _stub_loader(self, monkeypatch, incumbent, X, y):
        monkeypatch.setattr(
            rc, "_load_model_and_data", lambda name: (incumbent, X, y)
        )

    def test_returns_full_report(self, monkeypatch, fitted, dataset):
        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)
        report = rc.shadow_recalibrate("nba_game", run_walk_forward=False)
        assert report["model_name"] == "nba_game"
        assert report["n_train"] == 320
        assert report["n_test"] == 80
        assert report["recommendation"] in {"retrain", "hold"}

    def test_never_persists(self, monkeypatch, fitted, dataset):
        """The guarantee D5-01 rests on: ModelStore.save must never be reached."""
        from src.models.model_store import ModelStore

        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)

        def explode(*args, **kwargs):
            raise AssertionError("recalibrate must never persist a model")

        monkeypatch.setattr(ModelStore, "save", explode)
        report = rc.shadow_recalibrate("nba_game", run_walk_forward=False)
        assert report["persisted"] is False

    def test_never_calls_model_train(self, monkeypatch, fitted, dataset):
        """D-01 reserves train() for pipeline.py."""
        from src.models.nba_game import NBAGameModel

        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)

        def explode(*args, **kwargs):
            raise AssertionError("recalibrate must never call train()")

        monkeypatch.setattr(NBAGameModel, "train", explode)
        rc.shadow_recalibrate("nba_game", run_walk_forward=False)

    def test_reports_holdout_untouched(self, monkeypatch, fitted, dataset):
        """R10.5 compares on the test set, never the holdout."""
        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)
        report = rc.shadow_recalibrate("nba_game", run_walk_forward=False)
        assert report["holdout_used"] is False

    def test_split_is_temporal_not_shuffled(self, monkeypatch, fitted, dataset):
        """Train must be the earliest 80%, preserving order."""
        X, y = dataset
        captured = {}

        def capture(X_train, y_train, **kwargs):
            captured["X_train"] = X_train
            return fitted

        self._stub_loader(monkeypatch, fitted, X, y)
        monkeypatch.setattr(rc, "fit_candidate", capture)
        rc.shadow_recalibrate("nba_game", run_walk_forward=False)
        assert np.array_equal(captured["X_train"], X[:320])

    def test_walk_forward_included_when_requested(self, monkeypatch, fitted, dataset):
        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)
        report = rc.shadow_recalibrate("nba_game", run_walk_forward=True)
        assert report["walk_forward"]["n_windows"] > 0

    def test_walk_forward_skippable(self, monkeypatch, fitted, dataset):
        X, y = dataset
        self._stub_loader(monkeypatch, fitted, X, y)
        assert rc.shadow_recalibrate("nba_game", run_walk_forward=False)["walk_forward"] is None

    def test_too_few_samples_raises(self, monkeypatch, fitted):
        X = np.random.RandomState(2).rand(1, 4)
        y = np.array([1])
        self._stub_loader(monkeypatch, fitted, X, y)
        with pytest.raises(ValueError, match="Not enough samples"):
            rc.shadow_recalibrate("nba_game", run_walk_forward=False)


class TestModelSelection:
    def test_weather_models_rejected(self):
        """They hold no estimator, so there is nothing to recalibrate."""
        for name in ("weather_temp", "weather_precip"):
            with pytest.raises(ValueError, match="cannot be recalibrated"):
                rc._load_model_and_data(name)

    def test_unknown_model_rejected(self):
        with pytest.raises(ValueError, match="cannot be recalibrated"):
            rc._load_model_and_data("not_a_model")


class TestRecalibrateAll:
    def test_skips_untrained_models_without_raising(self, monkeypatch):
        def not_found(name):
            raise ModelNotFoundError(f"no version of {name}")

        monkeypatch.setattr(rc, "_load_model_and_data", not_found)
        results = rc.recalibrate_all(("nba_game", "nba_totals"))
        assert results["nba_game"]["skipped"] == "no trained version"
        assert results["nba_totals"]["skipped"] == "no trained version"

    def test_one_failure_does_not_stop_the_sweep(self, monkeypatch, fitted, dataset):
        X, y = dataset

        def loader(name):
            if name == "nba_game":
                raise RuntimeError("data source down")
            return fitted, X, y

        monkeypatch.setattr(rc, "_load_model_and_data", loader)
        results = rc.recalibrate_all(("nba_game", "nba_totals"))
        assert "RuntimeError" in results["nba_game"]["skipped"]
        assert results["nba_totals"]["recommendation"] in {"retrain", "hold"}

    def test_defaults_to_every_gbm_store(self, monkeypatch):
        from src.analytics.performance import GBM_MODEL_STORES

        monkeypatch.setattr(
            rc, "_load_model_and_data",
            lambda name: (_ for _ in ()).throw(ModelNotFoundError("none")),
        )
        assert set(rc.recalibrate_all()) == set(GBM_MODEL_STORES)


class TestBuildTrainingDataExtraction:
    """The 05-04 refactor: train() must still work through the new seam."""

    def test_models_expose_build_training_data(self):
        from src.models.nba_game import NBAGameModel
        from src.models.nba_props import NBAPropsModel
        from src.models.nba_totals import NBATotalsModel

        assert callable(NBAGameModel().build_training_data)
        assert callable(NBATotalsModel().build_training_data)
        assert callable(NBAPropsModel().build_training_data)

    def test_props_builder_takes_prop_type(self):
        import inspect

        from src.models.nba_props import NBAPropsModel

        sig = inspect.signature(NBAPropsModel().build_training_data)
        assert "prop_type" in sig.parameters
