"""Tests for feature importance extraction and tracking (05-03, R10.4)."""

import numpy as np
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from src.analytics import performance as perf
from src.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "fi.db"))


@pytest.fixture(scope="module")
def fitted_calibrated():
    """A CalibratedClassifierCV wrapping a GBM, matching how models are persisted.

    Feature 0 fully determines the label, so its importance must dominate --
    which makes the extraction assertion meaningful rather than merely structural.
    """
    rng = np.random.RandomState(0)
    X = rng.rand(120, 4)
    y = (X[:, 0] > 0.5).astype(int)
    gbm = GradientBoostingClassifier(n_estimators=10, random_state=42)
    return CalibratedClassifierCV(gbm, method="sigmoid", cv=3).fit(X, y)


@pytest.fixture(scope="module")
def fitted_bare():
    rng = np.random.RandomState(1)
    X = rng.rand(80, 4)
    y = (X[:, 1] > 0.5).astype(int)
    return GradientBoostingClassifier(n_estimators=10, random_state=42).fit(X, y)


NAMES4 = ["f0", "f1", "f2", "f3"]


class TestGbmFeatureNames:
    """The store-name -> feature-list mapping."""

    def test_nba_game(self):
        names = perf.gbm_feature_names("nba_game")
        assert len(names) == 7
        assert names[0] == "home_elo"

    def test_nba_totals(self):
        assert perf.gbm_feature_names("nba_totals") == [
            "home_ortg", "away_ortg", "home_pace", "away_pace", "line",
        ]

    def test_props_variants_resolve(self):
        for prop in ("pts", "reb", "ast", "3pm"):
            names = perf.gbm_feature_names(f"nba_props_{prop}")
            assert len(names) == 7
            assert names[0] == f"season_avg_{prop}"

    def test_unknown_prop_type_raises(self):
        with pytest.raises(ValueError, match="Unknown prop type"):
            perf.gbm_feature_names("nba_props_blocks")

    def test_weather_models_are_rejected(self):
        """Weather models hold no estimator, so R10.4 does not apply to them."""
        for name in ("weather_temp", "weather_precip"):
            with pytest.raises(ValueError, match="not a GBM-backed model store"):
                perf.gbm_feature_names(name)

    def test_every_declared_store_resolves(self):
        for name in perf.GBM_MODEL_STORES:
            assert len(perf.gbm_feature_names(name)) > 0


class TestExtractImportances:
    def test_averages_across_calibration_folds(self, fitted_calibrated):
        result = perf.extract_feature_importances(fitted_calibrated, NAMES4)
        assert set(result) == set(NAMES4)
        assert sum(result.values()) == pytest.approx(1.0, abs=1e-6)

    def test_recovers_the_informative_feature(self, fitted_calibrated):
        result = perf.extract_feature_importances(fitted_calibrated, NAMES4)
        assert max(result, key=result.get) == "f0"

    def test_matches_manual_fold_average(self, fitted_calibrated):
        """Guards against silently reading only the first fold."""
        per_fold = np.vstack([
            c.estimator.feature_importances_
            for c in fitted_calibrated.calibrated_classifiers_
        ])
        expected = per_fold.mean(axis=0)
        result = perf.extract_feature_importances(fitted_calibrated, NAMES4)
        assert [result[n] for n in NAMES4] == pytest.approx(list(expected))

    def test_uses_all_three_folds(self, fitted_calibrated):
        assert len(fitted_calibrated.calibrated_classifiers_) == 3

    def test_accepts_bare_estimator(self, fitted_bare):
        result = perf.extract_feature_importances(fitted_bare, NAMES4)
        assert max(result, key=result.get) == "f1"

    def test_preserves_feature_order(self, fitted_bare):
        assert list(perf.extract_feature_importances(fitted_bare, NAMES4)) == NAMES4

    def test_rejects_name_count_mismatch(self, fitted_bare):
        """A silent mismatch would mislabel every feature."""
        with pytest.raises(ValueError, match="Feature count mismatch"):
            perf.extract_feature_importances(fitted_bare, ["a", "b"])

    def test_rejects_estimator_without_importances(self):
        rng = np.random.RandomState(2)
        X = rng.rand(40, 4)
        model = LogisticRegression().fit(X, (X[:, 0] > 0.5).astype(int))
        with pytest.raises(ValueError, match="no feature_importances_"):
            perf.extract_feature_importances(model, NAMES4)


class TestRecordImportances:
    def test_writes_one_row_per_feature(self, db, fitted_bare):
        perf.record_feature_importances(db, "nba_game", 1, fitted_bare, NAMES4)
        row = db.fetchone("SELECT COUNT(*) AS c FROM feature_importance")
        assert row["c"] == 4

    def test_idempotent_for_same_version(self, db, fitted_bare):
        perf.record_feature_importances(db, "nba_game", 1, fitted_bare, NAMES4)
        perf.record_feature_importances(db, "nba_game", 1, fitted_bare, NAMES4)
        row = db.fetchone("SELECT COUNT(*) AS c FROM feature_importance")
        assert row["c"] == 4

    def test_defaults_feature_names_from_model_name(self, db, fitted_calibrated):
        rng = np.random.RandomState(3)
        X = rng.rand(100, 5)
        y = (X[:, 2] > 0.5).astype(int)
        model = CalibratedClassifierCV(
            GradientBoostingClassifier(n_estimators=8, random_state=1),
            method="sigmoid", cv=3,
        ).fit(X, y)
        recorded = perf.record_feature_importances(db, "nba_totals", 1, model)
        assert set(recorded) == set(perf.gbm_feature_names("nba_totals"))

    def test_values_round_trip(self, db, fitted_bare):
        written = perf.record_feature_importances(db, "nba_game", 1, fitted_bare, NAMES4)
        rows = db.fetchall(
            "SELECT feature_name, importance FROM feature_importance ORDER BY feature_name"
        )
        assert {r["feature_name"]: r["importance"] for r in rows} == pytest.approx(written)


class TestImportanceHistory:
    """R10.4's 'tracked over time' half."""

    def _record(self, db, version, values):
        db.execute("DELETE FROM feature_importance WHERE model_version = ?", (version,))
        for name, value in zip(NAMES4, values):
            db.execute(
                "INSERT INTO feature_importance (model_name, model_version, "
                "feature_name, importance, recorded_at) VALUES (?, ?, ?, ?, ?)",
                ("nba_game", version, name, value, f"2026-09-0{version}T00:00:00+00:00"),
            )

    def test_empty_history(self, db):
        assert perf.get_importance_history(db, "nba_game") == []

    def test_ordered_by_version(self, db):
        self._record(db, 2, [0.1, 0.2, 0.3, 0.4])
        self._record(db, 1, [0.4, 0.3, 0.2, 0.1])
        history = perf.get_importance_history(db, "nba_game")
        assert [h["model_version"] for h in history] == [1, 2]

    def test_groups_features_per_version(self, db):
        self._record(db, 1, [0.4, 0.3, 0.2, 0.1])
        history = perf.get_importance_history(db, "nba_game")
        assert history[0]["importances"] == {"f0": 0.4, "f1": 0.3, "f2": 0.2, "f3": 0.1}

    def test_drift_needs_two_versions(self, db):
        self._record(db, 1, [0.4, 0.3, 0.2, 0.1])
        assert perf.importance_drift(db, "nba_game") == {
            "from_version": None, "to_version": None, "drift": {},
        }

    def test_drift_between_latest_two(self, db):
        self._record(db, 1, [0.4, 0.3, 0.2, 0.1])
        self._record(db, 2, [0.1, 0.3, 0.2, 0.4])
        d = perf.importance_drift(db, "nba_game")
        assert d["from_version"] == 1 and d["to_version"] == 2
        assert d["drift"]["f0"] == pytest.approx(-0.3)
        assert d["drift"]["f1"] == pytest.approx(0.0)
        assert d["drift"]["f3"] == pytest.approx(0.3)

    def test_feature_absent_in_one_version_drifts_against_zero(self, db):
        self._record(db, 1, [0.4, 0.3, 0.2, 0.1])
        db.execute(
            "INSERT INTO feature_importance (model_name, model_version, "
            "feature_name, importance, recorded_at) VALUES "
            "('nba_game', 2, 'brand_new', 0.5, '2026-09-02T00:00:00+00:00')"
        )
        d = perf.importance_drift(db, "nba_game")
        assert d["drift"]["brand_new"] == pytest.approx(0.5)
        assert d["drift"]["f0"] == pytest.approx(-0.4)


class TestModelVersionsTable:
    """D5-02: the table that had been dead since schema v1."""

    def test_writes_a_row(self, db):
        perf.record_model_version(db, "nba_game", 1, metrics={"test_brier": 0.21})
        row = db.fetchone("SELECT * FROM model_versions WHERE model_name = 'nba_game'")
        assert row["version"] == 1
        assert row["is_active"] == 1
        assert '"test_brier": 0.21' in row["metrics_json"]

    def test_activating_a_version_deactivates_the_others(self, db):
        perf.record_model_version(db, "nba_game", 1)
        perf.record_model_version(db, "nba_game", 2)
        rows = db.fetchall(
            "SELECT version, is_active FROM model_versions "
            "WHERE model_name = 'nba_game' ORDER BY version"
        )
        assert [r["is_active"] for r in rows] == [0, 1]

    def test_other_models_are_unaffected(self, db):
        perf.record_model_version(db, "nba_game", 1)
        perf.record_model_version(db, "nba_totals", 1)
        row = db.fetchone(
            "SELECT is_active FROM model_versions WHERE model_name = 'nba_game'"
        )
        assert row["is_active"] == 1

    def test_rerecording_same_version_is_idempotent(self, db):
        perf.record_model_version(db, "nba_game", 1, metrics={"a": 1})
        perf.record_model_version(db, "nba_game", 1, metrics={"a": 2})
        row = db.fetchone("SELECT COUNT(*) AS c FROM model_versions")
        assert row["c"] == 1
