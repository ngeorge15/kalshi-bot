"""Tests for src/analytics/performance.py (05-02, R10.1-R10.3)."""

import pytest

from scripts.seed_test_data import seed_database
from src.analytics import performance as perf
from src.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "perf.db"))


@pytest.fixture
def seeded(db):
    manifest = seed_database(db, n_days=7, seed=42)
    return db, manifest


def _insert(db, ticker, market_type, prob, result, pnl, settled_at,
            model_name="m1", edge_cents=5, link=True):
    """Insert a linked prediction/trade/outcome triple with exact values."""
    pred_id = db.execute(
        "INSERT INTO predictions (ticker, market_type, model_name, model_version, "
        "predicted_prob, market_price_cents, edge_cents, features_json, created_at) "
        "VALUES (?, ?, ?, 1, ?, 50, ?, '', ?)",
        (ticker, market_type, model_name, prob, edge_cents, settled_at),
    ).lastrowid
    db.execute(
        "INSERT INTO outcomes (ticker, market_type, result, settlement_price_cents, "
        "trade_id, prediction_id, pnl_cents, settled_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            ticker,
            market_type,
            result,
            100 if result == "yes" else 0,
            pred_id if link else None,
            pnl,
            settled_at,
        ),
    )
    return pred_id


@pytest.fixture
def exact(db):
    """Two trades with hand-computed metrics.

    p=0.8 settles yes -> (0.8-1)^2 = 0.04
    p=0.6 settles no  -> (0.6-0)^2 = 0.36
    Brier = 0.20; accuracy = 0.5; mean predicted = 0.70; actual frequency = 0.50
    """
    _insert(db, "T-1", "games", 0.8, "yes", 400, "2026-09-01T12:00:00+00:00")
    _insert(db, "T-2", "games", 0.6, "no", -600, "2026-09-02T12:00:00+00:00")
    return db


class TestSettledJoin:
    """D5-06: one join helper, with a working fallback path."""

    def test_resolves_via_prediction_id(self, exact):
        rows = perf._settled_predictions(exact)
        assert len(rows) == 2
        assert rows[0]["predicted_prob"] == 0.8
        assert rows[0]["label"] == 1
        assert rows[1]["label"] == 0

    def test_ordered_oldest_first(self, exact):
        rows = perf._settled_predictions(exact)
        assert [r["ticker"] for r in rows] == ["T-1", "T-2"]

    def test_falls_back_to_ticker_when_link_missing(self, db):
        _insert(db, "T-9", "games", 0.75, "yes", 250,
                "2026-09-01T12:00:00+00:00", link=False)
        rows = perf._settled_predictions(db)
        assert len(rows) == 1
        assert rows[0]["predicted_prob"] == 0.75
        assert rows[0]["model_name"] == "m1"

    def test_fallback_picks_most_recent_prediction(self, db):
        db.execute(
            "INSERT INTO predictions (ticker, market_type, model_name, model_version, "
            "predicted_prob, market_price_cents, edge_cents, features_json, created_at) "
            "VALUES ('T-9', 'games', 'm1', 1, 0.30, 50, 5, '', '2026-08-01T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO predictions (ticker, market_type, model_name, model_version, "
            "predicted_prob, market_price_cents, edge_cents, features_json, created_at) "
            "VALUES ('T-9', 'games', 'm1', 1, 0.90, 50, 5, '', '2026-08-05T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO outcomes (ticker, market_type, result, settlement_price_cents, "
            "trade_id, prediction_id, pnl_cents, settled_at) "
            "VALUES ('T-9', 'games', 'yes', 100, NULL, NULL, 100, '2026-08-06T00:00:00+00:00')"
        )
        rows = perf._settled_predictions(db)
        assert rows[0]["predicted_prob"] == 0.90

    def test_drops_outcomes_with_no_prediction(self, db):
        db.execute(
            "INSERT INTO outcomes (ticker, market_type, result, settlement_price_cents, "
            "trade_id, prediction_id, pnl_cents, settled_at) "
            "VALUES ('ORPHAN', 'games', 'yes', 100, NULL, NULL, 100, '2026-09-01T00:00:00+00:00')"
        )
        assert perf._settled_predictions(db) == []

    def test_filters(self, seeded):
        db, _ = seeded
        rows = perf._settled_predictions(db, market_type="props")
        assert rows and all(r["market_type"] == "props" for r in rows)
        rows = perf._settled_predictions(db, model_name="weather_temp")
        assert rows and all(r["model_name"] == "weather_temp" for r in rows)

    def test_since_filter_is_inclusive(self, exact):
        rows = perf._settled_predictions(exact, since="2026-09-02T12:00:00+00:00")
        assert [r["ticker"] for r in rows] == ["T-2"]


class TestBrier:
    """R10.1"""

    def test_exact_values(self, exact):
        s = perf.brier_summary(exact)
        assert s["n"] == 2
        assert s["brier"] == pytest.approx(0.20)
        assert s["accuracy"] == pytest.approx(0.5)
        assert s["mean_predicted_prob"] == pytest.approx(0.70)
        assert s["actual_frequency"] == pytest.approx(0.50)
        assert s["calibration_drift"] == pytest.approx(0.20)

    def test_empty_slice_returns_none_not_raises(self, db):
        s = perf.brier_summary(db)
        assert s["n"] == 0
        assert s["brier"] is None
        assert s["calibration_drift"] is None

    def test_unknown_market_type_is_empty_not_error(self, seeded):
        db, _ = seeded
        assert perf.brier_summary(db, market_type="does_not_exist")["n"] == 0

    def test_by_market_type_covers_every_type(self, seeded):
        db, manifest = seeded
        breakdown = perf.brier_by_market_type(db)
        assert set(breakdown) == set(manifest["market_types"])

    def test_by_market_type_counts_match_manifest(self, seeded):
        db, manifest = seeded
        breakdown = perf.brier_by_market_type(db)
        for mt, bucket in manifest["by_market_type"].items():
            assert breakdown[mt]["n"] == bucket["n_trades"], mt

    def test_by_model_covers_every_model(self, seeded):
        db, _ = seeded
        by_model = perf.brier_by_model(db)
        assert "nba_props" in by_model and "cbb_game" in by_model

    def test_drift_matches_seeder_manifest(self, seeded):
        """Analytics must recover exactly what the seeder recorded as truth."""
        db, manifest = seeded
        breakdown = perf.brier_by_market_type(db)
        for mt, bucket in manifest["by_market_type"].items():
            expected = bucket["mean_predicted_prob"] - bucket["actual_frequency"]
            assert breakdown[mt]["calibration_drift"] == pytest.approx(expected), mt


class TestRollingBrier:
    """R10.1: last 50 / last 100."""

    def test_default_windows(self, seeded):
        db, _ = seeded
        rolling = perf.rolling_brier(db)
        assert set(rolling) == {"last_50", "last_100"}

    def test_window_caps_at_available_rows(self, exact):
        rolling = perf.rolling_brier(exact, windows=(50,))
        assert rolling["last_50"]["n"] == 2

    def test_uses_most_recent_trades(self, exact):
        """A window of 1 must take the newest trade, not the oldest."""
        rolling = perf.rolling_brier(exact, windows=(1,))
        assert rolling["last_1"]["n"] == 1
        assert rolling["last_1"]["brier"] == pytest.approx(0.36)

    def test_empty_window_is_none_not_error(self, db):
        rolling = perf.rolling_brier(db, windows=(50,))
        assert rolling["last_50"] == {
            "n": 0, "brier": None, "accuracy": None, "calibration_drift": None
        }


class TestCalibrationBins:
    """R10.2, D5-04: data first, image later."""

    def test_shape_is_stable(self, seeded):
        db, _ = seeded
        assert len(perf.calibration_bins(db, n_bins=10)) == 10
        assert len(perf.calibration_bins(db, n_bins=5)) == 5

    def test_empty_db_still_returns_full_shape(self, db):
        bins = perf.calibration_bins(db, n_bins=10)
        assert len(bins) == 10
        assert all(b["count"] == 0 and b["mean_predicted"] is None for b in bins)

    def test_counts_sum_to_sample_size(self, seeded):
        db, manifest = seeded
        bins = perf.calibration_bins(db)
        assert sum(b["count"] for b in bins) == manifest["total_outcomes"]

    def test_probability_of_one_lands_in_final_bin(self, db):
        _insert(db, "T-1", "games", 1.0, "yes", 100, "2026-09-01T00:00:00+00:00")
        bins = perf.calibration_bins(db, n_bins=10)
        assert bins[-1]["count"] == 1
        assert sum(b["count"] for b in bins) == 1

    def test_bin_assignment_is_correct(self, db):
        _insert(db, "A", "games", 0.05, "no", -50, "2026-09-01T00:00:00+00:00")
        _insert(db, "B", "games", 0.95, "yes", 50, "2026-09-02T00:00:00+00:00")
        bins = perf.calibration_bins(db, n_bins=10)
        assert bins[0]["count"] == 1
        assert bins[9]["count"] == 1
        assert bins[0]["actual_frequency"] == 0.0
        assert bins[9]["actual_frequency"] == 1.0


class TestPnl:
    """R10.3"""

    def test_curve_accumulates(self, exact):
        curve = perf.pnl_curve(exact)
        assert [p["cumulative_pnl_cents"] for p in curve] == [400, -200]

    def test_curve_final_equals_sum(self, seeded):
        db, manifest = seeded
        curve = perf.pnl_curve(db)
        expected = sum(b["pnl_cents"] for b in manifest["by_market_type"].values())
        assert curve[-1]["cumulative_pnl_cents"] == expected

    def test_win_rate_exact(self, exact):
        w = perf.win_rate(exact)
        assert w == {"n": 2, "wins": 1, "losses": 1,
                     "win_rate": pytest.approx(0.5), "total_pnl_cents": -200}

    def test_win_rate_empty_is_none(self, db):
        assert perf.win_rate(db)["win_rate"] is None

    def test_zero_pnl_counts_as_neither_win_nor_loss(self, db):
        _insert(db, "FLAT", "games", 0.5, "yes", 0, "2026-09-01T00:00:00+00:00")
        w = perf.win_rate(db)
        assert w["n"] == 1 and w["wins"] == 0 and w["losses"] == 0
        assert w["win_rate"] is None

    def test_pnl_by_market_type_matches_manifest(self, seeded):
        db, manifest = seeded
        by_type = perf.pnl_by_market_type(db)
        for mt, bucket in manifest["by_market_type"].items():
            assert by_type[mt]["total_pnl_cents"] == bucket["pnl_cents"], mt

    def test_third_domain_present_in_breakdown(self, seeded):
        """D5-07: breakdowns must not assume {nba, weather}."""
        db, _ = seeded
        assert "cbb_games" in perf.pnl_by_market_type(db)

    def test_edge_on_wins_vs_losses(self, db):
        _insert(db, "W", "games", 0.8, "yes", 100, "2026-09-01T00:00:00+00:00", edge_cents=9)
        _insert(db, "L", "games", 0.8, "no", -100, "2026-09-02T00:00:00+00:00", edge_cents=3)
        e = perf.edge_on_wins_vs_losses(db)
        assert e["mean_edge_on_wins"] == 9
        assert e["mean_edge_on_losses"] == 3
        assert e["edge_gap"] == 6

    def test_edge_gap_none_when_one_side_empty(self, db):
        _insert(db, "W", "games", 0.8, "yes", 100, "2026-09-01T00:00:00+00:00", edge_cents=9)
        e = perf.edge_on_wins_vs_losses(db)
        assert e["mean_edge_on_losses"] is None
        assert e["edge_gap"] is None


class TestDrawdown:
    def test_empty_is_zero(self, db):
        assert perf.drawdown(db)["max_drawdown_cents"] == 0

    def test_peak_to_trough(self, db):
        # cumulative: 100, 300, 50, 120  -> max drawdown 250 (300 -> 50)
        for i, (pnl, day) in enumerate([(100, "01"), (200, "02"), (-250, "03"), (70, "04")]):
            _insert(db, f"T{i}", "games", 0.5, "yes" if pnl > 0 else "no", pnl,
                    f"2026-09-{day}T00:00:00+00:00")
        d = perf.drawdown(db)
        assert d["max_drawdown_cents"] == 250
        assert d["peak_cents"] == 300
        assert d["trough_cents"] == 50
        assert d["current_drawdown_cents"] == 180

    def test_monotonic_rise_has_no_drawdown(self, db):
        for i, day in enumerate(["01", "02", "03"]):
            _insert(db, f"T{i}", "games", 0.5, "yes", 100, f"2026-09-{day}T00:00:00+00:00")
        assert perf.drawdown(db)["max_drawdown_cents"] == 0


class TestSharpe:
    """D5-05: daily returns, annualised by sqrt(252)."""

    def test_none_with_fewer_than_two_days(self, db):
        assert perf.sharpe_ratio(db) is None
        _insert(db, "T1", "games", 0.5, "yes", 100, "2026-09-01T00:00:00+00:00")
        perf.recompute_all_daily_pnl(db)
        assert perf.sharpe_ratio(db) is None

    def test_none_on_zero_variance(self, db):
        for i, day in enumerate(["01", "02", "03"]):
            _insert(db, f"T{i}", "games", 0.5, "yes", 100, f"2026-09-{day}T00:00:00+00:00")
        perf.recompute_all_daily_pnl(db)
        assert perf.sharpe_ratio(db) is None

    def test_known_value(self, db):
        """Daily P&L [100, -50, 150]: mean 66.667, sample std 104.083."""
        import numpy as np

        for i, (pnl, day) in enumerate([(100, "01"), (-50, "02"), (150, "03")]):
            _insert(db, f"T{i}", "games", 0.5, "yes" if pnl > 0 else "no", pnl,
                    f"2026-09-{day}T00:00:00+00:00")
        perf.recompute_all_daily_pnl(db)
        r = np.array([100.0, -50.0, 150.0])
        expected = r.mean() / r.std(ddof=1) * np.sqrt(252)
        assert perf.sharpe_ratio(db) == pytest.approx(expected)

    def test_scale_invariant_under_capital_normalisation(self, db):
        for i, (pnl, day) in enumerate([(100, "01"), (-50, "02"), (150, "03")]):
            _insert(db, f"T{i}", "games", 0.5, "yes" if pnl > 0 else "no", pnl,
                    f"2026-09-{day}T00:00:00+00:00")
        perf.recompute_all_daily_pnl(db)
        assert perf.sharpe_ratio(db) == pytest.approx(
            perf.sharpe_ratio(db, capital_cents=50000)
        )


class TestDailyPnlRecompute:
    """The aggregator that previously did not exist."""

    def test_populates_from_outcomes(self, db):
        _insert(db, "A", "games", 0.6, "yes", 300, "2026-09-01T10:00:00+00:00")
        _insert(db, "B", "games", 0.4, "no", -100, "2026-09-01T18:00:00+00:00")
        row = perf.recompute_daily_pnl(db, "2026-09-01")
        assert row["realized_pnl_cents"] == 200
        assert row["total_trades"] == 2
        assert row["winning_trades"] == 1
        assert row["losing_trades"] == 1

    def test_idempotent(self, db):
        _insert(db, "A", "games", 0.6, "yes", 300, "2026-09-01T10:00:00+00:00")
        first = perf.recompute_daily_pnl(db, "2026-09-01")
        second = perf.recompute_daily_pnl(db, "2026-09-01")
        assert first["realized_pnl_cents"] == second["realized_pnl_cents"]
        assert db.fetchone("SELECT COUNT(*) AS c FROM daily_pnl")["c"] == 1

    def test_day_with_no_outcomes_is_zeroed(self, db):
        row = perf.recompute_daily_pnl(db, "2026-01-01")
        assert row["realized_pnl_cents"] == 0 and row["total_trades"] == 0

    def test_recompute_all_covers_every_settled_day(self, seeded):
        db, _ = seeded
        rebuilt = perf.recompute_all_daily_pnl(db)
        days = db.fetchall("SELECT DISTINCT DATE(settled_at) AS d FROM outcomes")
        assert len(rebuilt) == len(days)

    def test_recompute_all_totals_match_ledger(self, seeded):
        db, manifest = seeded
        perf.recompute_all_daily_pnl(db)
        row = db.fetchone("SELECT SUM(realized_pnl_cents) AS total FROM daily_pnl")
        expected = sum(b["pnl_cents"] for b in manifest["by_market_type"].values())
        assert row["total"] == expected
