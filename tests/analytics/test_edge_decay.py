"""Tests for src/analytics/edge_decay.py (05-03, R10.6)."""

import pytest

from scripts.seed_test_data import seed_database
from src.analytics import edge_decay as ed
from src.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "decay.db"))


@pytest.fixture
def seeded(db):
    return db, seed_database(db, n_days=7, seed=42)


def _series(db, ticker, edges, market_type="games", start_hours=72.0):
    """Write an observation series with explicit edge magnitudes.

    Edges are expressed relative to a fixed model probability so the recorded
    edge_cents come out exactly as requested.
    """
    model_prob = 0.60
    hours = start_hours
    for i, edge in enumerate(edges):
        ed.record_observation(
            db,
            ticker=ticker,
            market_type=market_type,
            mid_price_cents=int(round(model_prob * 100 - edge)),
            model_prob=model_prob,
            hours_to_close=hours,
            observed_at=f"2026-09-01T{i:02d}:00:00+00:00",
        )
        hours /= 2.0


class TestRecordObservation:
    def test_inserts_and_computes_edge(self, db):
        row_id = ed.record_observation(
            db, ticker="T-1", market_type="games",
            mid_price_cents=52, model_prob=0.61, hours_to_close=6.0,
        )
        assert row_id > 0
        row = db.fetchone("SELECT * FROM price_observations WHERE id = ?", (row_id,))
        assert row["edge_cents"] == 9      # 0.61*100 - 52
        assert row["mid_price_cents"] == 52
        assert row["hours_to_close"] == 6.0

    def test_edge_is_null_without_model_prob(self, db):
        row_id = ed.record_observation(db, "T-1", "games", mid_price_cents=52)
        row = db.fetchone("SELECT * FROM price_observations WHERE id = ?", (row_id,))
        assert row["edge_cents"] is None
        assert row["model_prob"] is None

    def test_defaults_observed_at_to_now(self, db):
        row_id = ed.record_observation(db, "T-1", "games", mid_price_cents=50)
        row = db.fetchone("SELECT * FROM price_observations WHERE id = ?", (row_id,))
        assert row["observed_at"].startswith("20")

    def test_accepts_unknown_market_type(self, db):
        """D5-07: market_type is free-form, not validated against an enum."""
        ed.record_observation(db, "T-1", "nfl_props", mid_price_cents=50, model_prob=0.6)
        row = db.fetchone("SELECT market_type FROM price_observations")
        assert row["market_type"] == "nfl_props"

    def test_hours_to_close_may_be_null(self, db):
        ed.record_observation(db, "T-1", "games", mid_price_cents=50, model_prob=0.6)
        row = db.fetchone("SELECT hours_to_close FROM price_observations")
        assert row["hours_to_close"] is None


class TestDecayCurve:
    def test_empty_for_unknown_ticker(self, db):
        assert ed.decay_curve(db, "NOPE") == []

    def test_ordered_and_carries_abs_edge(self, db):
        _series(db, "T-1", [10, 6, 2, -4])
        curve = ed.decay_curve(db, "T-1")
        assert [c["edge_cents"] for c in curve] == [10, 6, 2, -4]
        assert [c["abs_edge_cents"] for c in curve] == [10, 6, 2, 4]

    def test_abs_edge_none_when_edge_none(self, db):
        ed.record_observation(db, "T-1", "games", mid_price_cents=50)
        assert ed.decay_curve(db, "T-1")[0]["abs_edge_cents"] is None


class TestDecayByBucket:
    def test_returns_every_bucket_even_when_empty(self, db):
        buckets = ed.decay_by_bucket(db)
        assert len(buckets) == len(ed.DEFAULT_BUCKETS)
        assert all(b["n"] == 0 and b["mean_abs_edge_cents"] is None for b in buckets)

    def test_assigns_to_correct_bucket(self, db):
        # hours 72, 36, 18, 9, 4.5, 2.25 -> buckets 48h+, 24-48, 12-24, 3-12, 3-12, 0-3
        _series(db, "T-1", [10, 10, 10, 10, 10, 10])
        by_label = {b["label"]: b for b in ed.decay_by_bucket(db)}
        assert by_label["48h+"]["n"] == 1
        assert by_label["24-48h"]["n"] == 1
        assert by_label["12-24h"]["n"] == 1
        assert by_label["3-12h"]["n"] == 2
        assert by_label["0-3h"]["n"] == 1

    def test_decaying_edge_shrinks_toward_close(self, db):
        _series(db, "T-1", [20, 16, 12, 8, 4, 1])
        by_label = {b["label"]: b for b in ed.decay_by_bucket(db)}
        assert by_label["48h+"]["mean_abs_edge_cents"] > by_label["0-3h"]["mean_abs_edge_cents"]

    def test_excludes_null_hours_and_null_edges(self, db):
        _series(db, "T-1", [10, 10])
        ed.record_observation(db, "T-2", "games", mid_price_cents=50, model_prob=0.6)
        ed.record_observation(db, "T-3", "games", mid_price_cents=50, hours_to_close=5.0)
        assert sum(b["n"] for b in ed.decay_by_bucket(db)) == 2

    def test_market_type_filter(self, db):
        _series(db, "A", [10, 10], market_type="games")
        _series(db, "B", [10, 10], market_type="temperature")
        assert sum(b["n"] for b in ed.decay_by_bucket(db, market_type="games")) == 2

    def test_signed_mean_differs_from_absolute(self, db):
        _series(db, "T-1", [10, -10])
        buckets = [b for b in ed.decay_by_bucket(db) if b["n"]]
        signed = sum(b["mean_edge_cents"] * b["n"] for b in buckets)
        absolute = sum(b["mean_abs_edge_cents"] * b["n"] for b in buckets)
        assert signed == 0
        assert absolute == 20


class TestEdgePersistence:
    def test_empty_db(self, db):
        p = ed.edge_persistence(db)
        assert p["n_tickers"] == 0 and p["persistence_rate"] is None

    def test_decaying_edge_does_not_persist(self, db):
        _series(db, "T-1", [20, 10, 4, 1])
        p = ed.edge_persistence(db)
        assert p["n_tickers"] == 1
        assert p["n_persisted"] == 0
        assert p["persistence_rate"] == 0.0
        assert p["mean_retained_fraction"] == pytest.approx(0.05)

    def test_persistent_edge_counts(self, db):
        _series(db, "T-1", [20, 19, 18, 18])
        p = ed.edge_persistence(db)
        assert p["n_persisted"] == 1
        assert p["persistence_rate"] == 1.0

    def test_mixed_population(self, db):
        _series(db, "PERSIST", [20, 20, 20])
        _series(db, "DECAY", [20, 5, 1])
        p = ed.edge_persistence(db)
        assert p["n_tickers"] == 2
        assert p["persistence_rate"] == pytest.approx(0.5)

    def test_excludes_single_observation_tickers(self, db):
        _series(db, "ONE", [20])
        assert ed.edge_persistence(db)["n_tickers"] == 0

    def test_excludes_tiny_initial_edges(self, db):
        """Proportional decay of a 1-cent edge is noise, not signal."""
        _series(db, "TINY", [1, 0])
        assert ed.edge_persistence(db)["n_tickers"] == 0

    def test_sign_flip_counts_by_magnitude(self, db):
        """An edge that inverts but keeps magnitude has not decayed."""
        _series(db, "FLIP", [20, -20])
        assert ed.edge_persistence(db)["n_persisted"] == 1

    def test_thresholds_are_tunable(self, db):
        _series(db, "T-1", [20, 12])
        assert ed.edge_persistence(db, persistence_ratio=0.5)["n_persisted"] == 1
        assert ed.edge_persistence(db, persistence_ratio=0.8)["n_persisted"] == 0


class TestSeededData:
    """Against the seeder's mixed decaying/persistent population."""

    def test_both_populations_present(self, seeded):
        db, _ = seeded
        p = ed.edge_persistence(db)
        assert p["n_tickers"] > 0
        assert 0.0 < p["persistence_rate"] < 1.0

    def test_edges_shrink_toward_close_on_average(self, seeded):
        db, _ = seeded
        by_label = {b["label"]: b for b in ed.decay_by_bucket(db)}
        assert by_label["48h+"]["mean_abs_edge_cents"] > by_label["0-3h"]["mean_abs_edge_cents"]

    def test_by_market_type_covers_third_domain(self, seeded):
        """D5-07 again: cbb_games must appear without any code knowing about it."""
        db, manifest = seeded
        by_type = ed.decay_by_market_type(db)
        assert set(by_type) == set(manifest["market_types"])
        assert "cbb_games" in by_type

    def test_summarize_shape(self, seeded):
        db, _ = seeded
        s = ed.summarize_decay(db)
        assert set(s) == {"buckets", "persistence", "by_market_type"}
        assert len(s["buckets"]) == len(ed.DEFAULT_BUCKETS)
