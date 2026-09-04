"""Tests for schema v3 and the deterministic paper-trading seeder (05-01)."""

import pytest

from scripts.seed_test_data import MARKET_PROFILES, seed_database
from src.db.database import Database


@pytest.fixture
def db(tmp_path):
    """Fresh database at schema v3."""
    return Database(str(tmp_path / "seed_test.db"))


@pytest.fixture
def seeded(db):
    """Database seeded at defaults, plus the ground-truth manifest."""
    manifest = seed_database(db, n_days=7, seed=42)
    return db, manifest


@pytest.fixture(params=[42, 7, 2024])
def high_volume(request, tmp_path):
    """~2000 trades, parameterised over seeds.

    Recovering a planted 8% calibration bias is sample-gated: at n=160 the
    standard error on the outcome frequency is ~0.04, so the bias sits inside
    the noise.  Around 500 samples per market type it separates cleanly.  Run
    across several seeds so the thresholds below are not tuned to one draw.
    """
    seed = request.param
    db = Database(str(tmp_path / f"hv_{seed}.db"))
    return db, seed_database(db, n_days=10, seed=seed, trades_per_day=200)


class TestSchemaV3:
    """D5-02, D5-03: new tables land and the version advances."""

    def test_schema_version_is_3(self, db):
        assert db.get_schema_version() == 3

    def test_feature_importance_table_exists(self, db):
        assert db.fetchall("SELECT * FROM feature_importance") == []

    def test_price_observations_table_exists(self, db):
        assert db.fetchall("SELECT * FROM price_observations") == []

    def test_feature_importance_unique_per_version_feature(self, db):
        args = ("nba_game", 1, "home_elo", 0.42, "2026-09-04T00:00:00+00:00")
        db.execute(
            "INSERT INTO feature_importance "
            "(model_name, model_version, feature_name, importance, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            args,
        )
        with pytest.raises(Exception):
            db.execute(
                "INSERT INTO feature_importance "
                "(model_name, model_version, feature_name, importance, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                args,
            )

    def test_upgrades_existing_v2_database(self, tmp_path):
        """A database created before v3 gains the new tables on next connect."""
        path = str(tmp_path / "upgrade.db")
        first = Database(path)
        first.execute("DELETE FROM schema_version WHERE version = 3")
        assert first.get_schema_version() == 2

        second = Database(path)
        assert second.get_schema_version() == 3
        assert second.fetchall("SELECT * FROM price_observations") == []


class TestDeterminism:
    """Same seed must reproduce the database exactly."""

    def test_same_seed_same_manifest(self, tmp_path):
        a = seed_database(Database(str(tmp_path / "a.db")), n_days=5, seed=7)
        b = seed_database(Database(str(tmp_path / "b.db")), n_days=5, seed=7)
        assert a == b

    def test_different_seed_differs(self, tmp_path):
        a = seed_database(Database(str(tmp_path / "a.db")), n_days=5, seed=7)
        b = seed_database(Database(str(tmp_path / "b.db")), n_days=5, seed=8)
        assert a["by_market_type"] != b["by_market_type"]

    def test_does_not_touch_global_rng(self, tmp_path):
        """Seeder must use a local Random -- global state stays untouched."""
        import random

        random.seed(1234)
        expected = [random.random() for _ in range(3)]

        random.seed(1234)
        seed_database(Database(str(tmp_path / "c.db")), n_days=2, seed=99)
        assert [random.random() for _ in range(3)] == expected


class TestSeededVolume:
    """Row counts sufficient for Phase 5 analytics and Phase 6's 60-trade gate."""

    def test_at_least_60_settled_trades(self, seeded):
        _, manifest = seeded
        assert manifest["total_trades"] >= 60

    def test_row_counts_match_manifest(self, seeded):
        db, manifest = seeded
        for table, key in (
            ("trades", "total_trades"),
            ("predictions", "total_predictions"),
            ("outcomes", "total_outcomes"),
            ("price_observations", "total_price_observations"),
        ):
            row = db.fetchone(f"SELECT COUNT(*) AS cnt FROM {table}")
            assert row["cnt"] == manifest[key], table

    def test_outcomes_link_to_predictions_and_trades(self, seeded):
        """D5-06's preferred join path is exercised, not just the ticker fallback."""
        db, _ = seeded
        row = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM outcomes "
            "WHERE prediction_id IS NULL OR trade_id IS NULL"
        )
        assert row["cnt"] == 0

    def test_join_via_prediction_id_resolves(self, seeded):
        db, manifest = seeded
        rows = db.fetchall(
            "SELECT p.predicted_prob, o.result FROM outcomes o "
            "JOIN predictions p ON p.id = o.prediction_id"
        )
        assert len(rows) == manifest["total_outcomes"]


class TestDomainAgnostic:
    """D5-07: nothing may assume the market types are {nba, weather}."""

    def test_three_domains_present(self, seeded):
        _, manifest = seeded
        assert "cbb_games" in manifest["market_types"]

    def test_five_market_types(self, seeded):
        db, _ = seeded
        rows = db.fetchall("SELECT DISTINCT market_type FROM outcomes")
        assert len({r["market_type"] for r in rows}) == len(MARKET_PROFILES)

    def test_third_domain_has_rows_in_every_table(self, seeded):
        db, _ = seeded
        for table in ("trades", "predictions", "outcomes", "price_observations"):
            row = db.fetchone(
                f"SELECT COUNT(*) AS cnt FROM {table} WHERE market_type = ?",
                ("cbb_games",),
            )
            assert row["cnt"] > 0, table


class TestPlantedBiases:
    """The ground truth the analytics layer is supposed to recover.

    Drift is ``mean_predicted_prob - actual_frequency``.  Because outcomes are
    drawn against the hidden ``true_p`` and the stated probability is
    ``true_p + calibration_bias``, drift is an unbiased estimator of the planted
    bias -- just a noisy one, hence the high-volume fixture.
    """

    def _drift(self, manifest, market_type):
        b = manifest["by_market_type"][market_type]
        return b["mean_predicted_prob"] - b["actual_frequency"]

    def test_props_overconfident_by_about_8_points(self, high_volume):
        _, manifest = high_volume
        assert 0.03 < self._drift(manifest, "props") < 0.13

    def test_precipitation_overconfident_by_about_15_points(self, high_volume):
        _, manifest = high_volume
        assert 0.09 < self._drift(manifest, "precipitation") < 0.20

    def test_calibrated_types_have_small_drift(self, high_volume):
        _, manifest = high_volume
        for mt in ("games", "temperature"):
            assert abs(self._drift(manifest, mt)) < 0.06, mt

    def test_biased_types_rank_above_calibrated_ones(self, high_volume):
        """The property that actually matters: miscalibration is detectable by rank."""
        _, manifest = high_volume
        props = self._drift(manifest, "props")
        precip = self._drift(manifest, "precipitation")
        for calibrated in ("games", "temperature"):
            assert props > self._drift(manifest, calibrated)
            assert precip > self._drift(manifest, calibrated)

    def test_precipitation_loses_money(self, high_volume):
        _, manifest = high_volume
        assert manifest["by_market_type"]["precipitation"]["pnl_cents"] < 0

    def test_calibrated_types_are_profitable(self, high_volume):
        _, manifest = high_volume
        combined = sum(
            manifest["by_market_type"][mt]["pnl_cents"]
            for mt in ("games", "temperature")
        )
        assert combined > 0

    def test_pnl_is_arithmetically_consistent(self, seeded):
        """P&L must follow from fill price and settlement, not be hand-set."""
        db, _ = seeded
        rows = db.fetchall(
            "SELECT o.settlement_price_cents, o.pnl_cents, t.price_cents, t.quantity "
            "FROM outcomes o JOIN trades t ON t.id = o.trade_id"
        )
        for r in rows:
            expected = round(
                (r["settlement_price_cents"] - r["price_cents"]) * r["quantity"]
            )
            assert r["pnl_cents"] == expected


class TestPriceObservations:
    """R10.6 inputs: both decaying and persistent edges must be present."""

    def test_every_ticker_has_a_series(self, seeded):
        db, manifest = seeded
        rows = db.fetchall(
            "SELECT ticker, COUNT(*) AS cnt FROM price_observations GROUP BY ticker"
        )
        assert len(rows) == manifest["total_outcomes"]
        assert all(r["cnt"] >= 5 for r in rows)

    def test_hours_to_close_descends_within_a_ticker(self, seeded):
        db, _ = seeded
        ticker = db.fetchone("SELECT ticker FROM price_observations LIMIT 1")["ticker"]
        hours = [
            r["hours_to_close"]
            for r in db.fetchall(
                "SELECT hours_to_close FROM price_observations "
                "WHERE ticker = ? ORDER BY observed_at",
                (ticker,),
            )
        ]
        assert hours == sorted(hours, reverse=True)

    def test_both_decaying_and_persistent_edges_exist(self, seeded):
        """Mean |edge| near close should vary widely across tickers."""
        db, _ = seeded
        rows = db.fetchall(
            "SELECT ticker, ABS(edge_cents) AS e FROM price_observations "
            "WHERE hours_to_close <= 2.0"
        )
        near_close = [r["e"] for r in rows]
        assert min(near_close) <= 2
        assert max(near_close) >= 4

    def test_mid_price_within_bid_ask(self, seeded):
        db, _ = seeded
        row = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM price_observations "
            "WHERE mid_price_cents < yes_bid_cents OR mid_price_cents > yes_ask_cents"
        )
        assert row["cnt"] == 0
