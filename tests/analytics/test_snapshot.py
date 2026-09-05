"""Tests for src/analytics/snapshot.py (05-04, R10.7) — the Phase 6 data contract."""

import json

import pytest

from scripts.seed_test_data import seed_database
from src.analytics import snapshot as snap
from src.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "snap.db"))


@pytest.fixture
def seeded(db):
    return db, seed_database(db, n_days=7, seed=42)


@pytest.fixture
def snapshot(seeded):
    db, manifest = seeded
    return snap.generate_snapshot(db, include_config=False), manifest, db


class TestContract:
    """Shape is an interface — Phase 6 parses it."""

    def test_is_versioned(self, snapshot):
        s, _, _ = snapshot
        assert s["schema_version"] == snap.SNAPSHOT_SCHEMA_VERSION

    def test_top_level_keys(self, snapshot):
        s, _, _ = snapshot
        assert set(s) == {
            "schema_version", "generated_at", "overall", "rolling",
            "by_market_type", "by_model", "calibration",
            "win_rate_by_edge_bucket", "model_versions", "feature_importances",
            "recent_trades", "recent_improvements", "edge_decay",
        }

    def test_json_serialisable(self, snapshot):
        """Anything numpy-typed would break the evaluator's JSON round-trip."""
        s, _, _ = snapshot
        assert json.loads(json.dumps(s)) is not None

    def test_empty_database_still_produces_valid_snapshot(self, db):
        s = snap.generate_snapshot(db, include_config=False)
        assert s["schema_version"] == snap.SNAPSHOT_SCHEMA_VERSION
        assert s["overall"]["n"] == 0
        assert s["overall"]["brier"] is None
        assert s["overall"]["sharpe_ratio"] is None
        assert json.loads(json.dumps(s)) is not None

    def test_generated_at_is_iso_utc(self, snapshot):
        s, _, _ = snapshot
        assert s["generated_at"].endswith("+00:00")


class TestContent:
    def test_overall_matches_totals(self, snapshot):
        s, manifest, _ = snapshot
        assert s["overall"]["n"] == manifest["total_outcomes"]
        expected = sum(b["pnl_cents"] for b in manifest["by_market_type"].values())
        assert s["overall"]["total_pnl_cents"] == expected

    def test_rebuilds_daily_pnl_so_sharpe_is_available(self, seeded):
        """Snapshot must not depend on someone having run the aggregator first."""
        db, _ = seeded
        assert db.fetchone("SELECT COUNT(*) AS c FROM daily_pnl")["c"] == 0
        s = snap.generate_snapshot(db, include_config=False)
        assert db.fetchone("SELECT COUNT(*) AS c FROM daily_pnl")["c"] > 0
        assert s["overall"]["sharpe_ratio"] is not None

    def test_rolling_windows_present(self, snapshot):
        s, _, _ = snapshot
        assert set(s["rolling"]) == {"last_50", "last_100"}

    def test_recent_trades_capped_and_newest_first(self, snapshot):
        s, _, _ = snapshot
        trades = s["recent_trades"]
        assert len(trades) == snap.RECENT_TRADES
        assert trades[0]["settled_at"] >= trades[-1]["settled_at"]

    def test_recent_trades_shorter_than_cap_on_small_db(self, db):
        seed_database(db, n_days=1, seed=1, trades_per_day=3)
        s = snap.generate_snapshot(db, include_config=False)
        assert len(s["recent_trades"]) == 3

    def test_calibration_bins_present_per_market_type(self, snapshot):
        s, manifest, _ = snapshot
        assert set(s["calibration"]["by_market_type"]) == set(manifest["market_types"])

    def test_edge_buckets_cover_all_settled_trades(self, snapshot):
        s, manifest, _ = snapshot
        assert sum(b["n"] for b in s["win_rate_by_edge_bucket"]) == manifest["total_outcomes"]

    def test_empty_sections_are_empty_not_missing(self, snapshot):
        """Nothing has written model_versions or improvements yet."""
        s, _, _ = snapshot
        assert s["model_versions"] == {}
        assert s["feature_importances"] == {}
        assert s["recent_improvements"] == []


class TestDomainAgnostic:
    """D5-07: the evaluator's view must not have a fixed market-type key set."""

    def test_breakdowns_include_third_domain(self, snapshot):
        s, _, _ = snapshot
        assert "cbb_games" in s["by_market_type"]["brier"]
        assert "cbb_games" in s["by_market_type"]["pnl"]
        assert "cbb_games" in s["calibration"]["by_market_type"]
        assert "cbb_games" in s["edge_decay"]["by_market_type"]

    def test_new_market_type_appears_without_code_change(self, db):
        """An entirely unknown sport must flow through to the snapshot."""
        db.execute(
            "INSERT INTO predictions (ticker, market_type, model_name, model_version, "
            "predicted_prob, market_price_cents, edge_cents, features_json, created_at) "
            "VALUES ('NFL-1', 'nfl_props', 'nfl_props_model', 1, 0.7, 60, 10, '', "
            "'2026-09-01T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO outcomes (ticker, market_type, result, settlement_price_cents, "
            "trade_id, prediction_id, pnl_cents, settled_at) "
            "VALUES ('NFL-1', 'nfl_props', 'yes', 100, NULL, 1, 400, "
            "'2026-09-01T12:00:00+00:00')"
        )
        s = snap.generate_snapshot(db, include_config=False)
        assert "nfl_props" in s["by_market_type"]["brier"]
        assert "nfl_props_model" in s["by_model"]


class TestWinRateByEdgeBucket:
    def test_buckets_assigned_correctly(self, db):
        from tests.analytics.test_performance import _insert

        _insert(db, "A", "games", 0.6, "yes", 100, "2026-09-01T00:00:00+00:00", edge_cents=2)
        _insert(db, "B", "games", 0.6, "yes", 100, "2026-09-02T00:00:00+00:00", edge_cents=4)
        _insert(db, "C", "games", 0.6, "no", -100, "2026-09-03T00:00:00+00:00", edge_cents=15)
        by_label = {b["label"]: b for b in snap.win_rate_by_edge_bucket(db)}
        assert by_label["0-3c"]["n"] == 1
        assert by_label["3-5c"]["n"] == 1
        assert by_label["12c+"]["n"] == 1
        assert by_label["12c+"]["win_rate"] == 0.0

    def test_empty_bucket_has_none_win_rate(self, db):
        assert all(b["win_rate"] is None for b in snap.win_rate_by_edge_bucket(db))


class TestModelSections:
    def test_model_versions_read_from_sql(self, db):
        from src.analytics import performance as perf

        perf.record_model_version(db, "nba_game", 3, metrics={"test_brier": 0.19})
        versions = snap.model_versions(db)
        assert versions["nba_game"]["version"] == 3
        assert versions["nba_game"]["metrics"]["test_brier"] == 0.19

    def test_only_active_versions_reported(self, db):
        from src.analytics import performance as perf

        perf.record_model_version(db, "nba_game", 1)
        perf.record_model_version(db, "nba_game", 2)
        assert snap.model_versions(db)["nba_game"]["version"] == 2

    def test_unparseable_metrics_json_does_not_crash(self, db):
        db.execute(
            "INSERT INTO model_versions (model_name, version, parameters_json, "
            "metrics_json, training_data_hash, is_active, created_at) "
            "VALUES ('nba_game', 1, '{}', 'not json', '', 1, '2026-09-01T00:00:00+00:00')"
        )
        assert snap.model_versions(db)["nba_game"]["metrics"] == {}

    def test_feature_importances_included_when_recorded(self, db):
        for name, value in [("home_elo", 0.5), ("away_elo", 0.5)]:
            db.execute(
                "INSERT INTO feature_importance (model_name, model_version, "
                "feature_name, importance, recorded_at) VALUES (?, ?, ?, ?, ?)",
                ("nba_game", 1, name, value, "2026-09-01T00:00:00+00:00"),
            )
        fi = snap.feature_importances(db)
        assert fi["nba_game"]["importances"]["home_elo"] == 0.5
        assert fi["nba_game"]["version"] == 1


class TestWriteSnapshot:
    def test_writes_parseable_json(self, seeded, tmp_path):
        db, _ = seeded
        out = str(tmp_path / "reports" / "snap.json")
        assert snap.write_snapshot(db, out, include_config=False) == out
        with open(out) as fh:
            loaded = json.load(fh)
        assert loaded["schema_version"] == snap.SNAPSHOT_SCHEMA_VERSION

    def test_creates_missing_directories(self, seeded, tmp_path):
        db, _ = seeded
        out = str(tmp_path / "a" / "b" / "c" / "snap.json")
        snap.write_snapshot(db, out, include_config=False)
        import os

        assert os.path.exists(out)


class TestConfigSection:
    def test_missing_config_is_none_not_a_crash(self, seeded, monkeypatch):
        """Config is None without Kalshi credentials — the normal test state."""
        db, _ = seeded
        monkeypatch.setattr("src.config.config", None)
        s = snap.generate_snapshot(db, include_config=True)
        assert s["config"] is None
