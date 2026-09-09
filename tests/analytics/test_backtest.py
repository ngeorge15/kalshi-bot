"""Tests for src/analytics/backtest.py: retrospective NBA model-skill harness.

nba_game_results is empty in this repository (no network fetch has ever been
run), so every test builds a synthetic season in-memory via
`_make_synthetic_games` rather than touching the database or nba_api.
"""

import csv
from datetime import date, timedelta

import numpy as np
import pytest

from src.analytics import backtest as bt

# Fixed seed for every synthetic-data helper call: determinism is a hard
# project requirement, and comparing two runs' output is how several tests
# below prove reproducibility.
SEED = 1234


def _make_synthetic_games(
    n_games: int,
    home_win_prob: float,
    n_teams: int = 10,
    games_per_day: int = 6,
    seed: int = SEED,
    start_date: date = date(2023, 10, 24),
) -> list[dict]:
    """Build a synthetic season of NBA-shaped game dicts.

    Each game's home_win is an independent Bernoulli(home_win_prob) draw
    (via a seeded numpy.random.Generator -- never the global RNG), so the
    "true" home-court skill of this synthetic league is exactly
    home_win_prob by construction. Scores are generated consistent with the
    drawn outcome. game_date advances every `games_per_day` games, so
    several games legitimately share one date (a "slate"), which matters for
    the event_key=game_date clustering tests.
    """
    rng = np.random.default_rng(seed)
    games = []
    current_date = start_date
    for i in range(n_games):
        home_id, away_id = rng.choice(n_teams, size=2, replace=False)
        home_win = bool(rng.random() < home_win_prob)
        base = int(rng.integers(95, 115))
        margin = int(rng.integers(1, 20))
        if home_win:
            home_pts, away_pts = base + margin, base
        else:
            home_pts, away_pts = base, base + margin
        games.append({
            "game_id": f"G{i:05d}",
            "game_date": current_date.isoformat(),
            "home_team_id": int(home_id),
            "away_team_id": int(away_id),
            "home_pts": home_pts,
            "away_pts": away_pts,
            "home_win": 1 if home_win else 0,
            "season": "2023-24",
        })
        if (i + 1) % games_per_day == 0:
            current_date += timedelta(days=1)
    return games


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


class TestBaselines:
    def test_base_rate_matches_empirical_mean(self):
        games = [{"home_win": 1}, {"home_win": 1}, {"home_win": 0}, {"home_win": 1}]
        assert bt.base_rate(games) == pytest.approx(0.75)

    def test_base_rate_empty_raises(self):
        with pytest.raises(ValueError):
            bt.base_rate([])

    def test_always_home_returns_rate_regardless_of_game(self):
        assert bt.always_home({"anything": "goes"}, 0.63) == pytest.approx(0.63)

    def test_elo_only_equal_ratings_with_home_advantage(self):
        tracker = bt.EloTracker()
        game = {"home_team_id": 1, "away_team_id": 2}
        prob = bt.elo_only(game, tracker)
        # Both teams start at INITIAL_ELO; only HOME_ADVANTAGE separates them,
        # so home should be favored (prob > 0.5) but not overwhelmingly.
        assert 0.5 < prob < 0.75

    def test_elo_only_stronger_home_team_favored_more(self):
        tracker = bt.EloTracker()
        tracker._ratings[1] = 1600.0  # Strong home team.
        tracker._ratings[2] = 1300.0  # Weak away team.
        prob = bt.elo_only({"home_team_id": 1, "away_team_id": 2}, tracker)
        assert prob > 0.85

    def test_elo_only_uses_home_advantage_constant(self):
        tracker = bt.EloTracker()
        game = {"home_team_id": 1, "away_team_id": 2}
        # Manually replicate the formula to pin down the exact contract.
        expected = bt.EloTracker.expected_outcome(
            bt.EloTracker.INITIAL_ELO + bt.EloTracker.HOME_ADVANTAGE,
            bt.EloTracker.INITIAL_ELO,
        )
        assert bt.elo_only(game, tracker) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# load_market_prices
# ---------------------------------------------------------------------------


class TestLoadMarketPrices:
    def test_reads_valid_csv(self, tmp_path):
        path = tmp_path / "odds.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["game_id", "market_yes_probability"])
            writer.writerow(["G00001", "0.62"])
            writer.writerow(["G00002", "0.41"])
        prices = bt.load_market_prices(str(path))
        assert prices == {"G00001": pytest.approx(0.62), "G00002": pytest.approx(0.41)}

    def test_missing_columns_raises(self, tmp_path):
        path = tmp_path / "bad.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["game_id", "some_other_column"])
            writer.writerow(["G00001", "0.5"])
        with pytest.raises(ValueError):
            bt.load_market_prices(str(path))

    def test_out_of_range_probability_raises(self, tmp_path):
        path = tmp_path / "odds.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["game_id", "market_yes_probability"])
            writer.writerow(["G00001", "1.5"])
        with pytest.raises(ValueError):
            bt.load_market_prices(str(path))

    def test_non_finite_probability_raises(self, tmp_path):
        path = tmp_path / "odds.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["game_id", "market_yes_probability"])
            writer.writerow(["G00001", "nan"])
        with pytest.raises(ValueError):
            bt.load_market_prices(str(path))

    def test_non_numeric_probability_raises(self, tmp_path):
        path = tmp_path / "odds.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["game_id", "market_yes_probability"])
            writer.writerow(["G00001", "not_a_number"])
        with pytest.raises(ValueError):
            bt.load_market_prices(str(path))


# ---------------------------------------------------------------------------
# run_backtest: correctness bars
# ---------------------------------------------------------------------------


class TestCorrectnessBars:
    def test_constant_half_model_near_zero_improvement_over_base_rate(self):
        # true home_win_prob = 0.5 makes always_home's Brier-optimal constant
        # coincide with a flat 0.5 predictor, so the improvement should be
        # tightly centered on zero (small deviation only from finite-sample
        # noise in the estimated rate).
        games = _make_synthetic_games(n_games=400, home_win_prob=0.5, seed=SEED)
        result = bt.run_backtest(
            games,
            n_splits=4,
            min_train_size=100,
            fit_fn=lambda X_tr, y_tr: None,
            predict_fn=lambda model, X_te, y_te: np.full(len(X_te), 0.5),
        )
        improvement = result["aggregate"]["brier_improvement_vs_always_home"]
        assert improvement == pytest.approx(0.0, abs=0.02)

    def test_oracle_model_near_zero_brier_and_large_improvement(self):
        games = _make_synthetic_games(n_games=400, home_win_prob=0.6, seed=SEED)
        result = bt.run_backtest(
            games,
            n_splits=4,
            min_train_size=100,
            fit_fn=lambda X_tr, y_tr: None,
            predict_fn=lambda model, X_te, y_te: y_te.astype(float),
        )
        agg = result["aggregate"]
        assert agg["model_brier"] == pytest.approx(0.0, abs=1e-9)
        assert agg["brier_improvement_vs_always_home"] > 0.15
        assert agg["brier_improvement_vs_elo_only"] > 0.1

    def test_inverted_model_clearly_negative_improvement(self):
        games = _make_synthetic_games(n_games=400, home_win_prob=0.6, seed=SEED)
        result = bt.run_backtest(
            games,
            n_splits=4,
            min_train_size=100,
            fit_fn=lambda X_tr, y_tr: None,
            predict_fn=lambda model, X_te, y_te: 1.0 - y_te.astype(float),
        )
        agg = result["aggregate"]
        assert agg["model_brier"] == pytest.approx(1.0, abs=1e-9)
        assert agg["brier_improvement_vs_always_home"] < -0.5
        assert agg["brier_improvement_vs_elo_only"] < -0.5

    def test_temporal_integrity_no_training_game_after_test_window(self):
        games = _make_synthetic_games(n_games=400, home_win_prob=0.58, seed=SEED)
        result = bt.run_backtest(games, n_splits=4, min_train_size=100)
        for window in result["windows"]:
            # The last training date must never be later than the first test
            # date of the same window.
            assert window["train_end_date"] <= window["test_start_date"]
            # And windows must be in non-decreasing chronological order.
        test_starts = [w["test_start_date"] for w in result["windows"]]
        assert test_starts == sorted(test_starts)

    def test_no_market_prices_states_no_comparison_was_made(self):
        games = _make_synthetic_games(n_games=300, home_win_prob=0.58, seed=SEED)
        result = bt.run_backtest(games, n_splits=3, min_train_size=100)
        assert result["market_comparison"] is None
        assert "no market comparison was performed" in result["market_comparison_note"].lower()
        assert "no odds source" in result["market_comparison_note"].lower()

    def test_synthetic_market_prices_computes_paired_clustered_comparison(self):
        games = _make_synthetic_games(n_games=300, home_win_prob=0.58, seed=SEED)
        # Build "market" prices from real per-game data so the test never
        # fabricates probabilities disconnected from the harness's own
        # validation rules -- these values are simply well-formed CSV input,
        # exactly what load_market_prices would hand back.
        rng = np.random.default_rng(SEED)
        market_prices = {
            g["game_id"]: float(np.clip(rng.normal(0.55, 0.1), 0.01, 0.99))
            for g in games
        }
        result = bt.run_backtest(
            games, n_splits=3, min_train_size=100, market_prices=market_prices
        )
        mc = result["market_comparison"]
        assert mc is not None
        assert mc["n_games_matched"] > 0
        assert mc["by_game_id"]["estimate"] is not None
        assert mc["by_slate_date"]["estimate"] is not None
        assert "paired, event-clustered" in result["market_comparison_note"].lower()


# ---------------------------------------------------------------------------
# run_backtest: structure and clustering sensitivity
# ---------------------------------------------------------------------------


class TestRunBacktestStructure:
    @pytest.fixture
    def result(self):
        games = _make_synthetic_games(n_games=500, home_win_prob=0.6, seed=SEED)
        return bt.run_backtest(games, n_splits=4, min_train_size=120)

    def test_top_level_keys(self, result):
        for key in (
            "scope", "n_games_total", "windows", "aggregate",
            "clustering_sensitivity", "market_comparison", "market_comparison_note",
        ):
            assert key in result

    def test_scope_mentions_skill_not_market(self, result):
        scope = result["scope"].lower()
        assert "skill" in scope or "baseline" in scope
        assert "does not" in scope or "not establish" in scope

    def test_windows_nonempty_and_cover_metrics(self, result):
        assert len(result["windows"]) > 0
        for w in result["windows"]:
            for key in (
                "model_brier", "model_accuracy", "model_calibration_error",
                "always_home_brier", "always_home_accuracy", "always_home_calibration_error",
                "elo_only_brier", "elo_only_accuracy", "elo_only_calibration_error",
                "brier_improvement_vs_always_home", "brier_improvement_vs_elo_only",
                "n_games",
            ):
                assert key in w

    def test_aggregate_n_games_matches_sum_of_windows(self, result):
        assert result["aggregate"]["n_games"] == sum(w["n_games"] for w in result["windows"])

    def test_brier_scores_within_valid_range(self, result):
        for w in result["windows"]:
            for key in ("model_brier", "always_home_brier", "elo_only_brier"):
                assert 0.0 <= w[key] <= 1.0

    def test_clustering_sensitivity_reports_both_granularities(self, result):
        cs = result["clustering_sensitivity"]
        assert cs["by_game_id"]["estimate"] is not None
        assert cs["by_slate_date"]["estimate"] is not None
        assert cs["comparison_baseline"] == "elo_only"
        assert "materially_different" in cs

    def test_clustering_by_game_and_by_date_barely_differ_for_nba(self, result):
        # The core claim this harness must demonstrate rather than assume:
        # NBA games are close to independent, so clustering by game_id vs.
        # by whole slate-date should produce CIs of similar width.
        cs = result["clustering_sensitivity"]
        w_game = cs["ci_width_by_game_id"]
        w_date = cs["ci_width_by_slate_date"]
        assert w_game is not None and w_date is not None
        relative_diff = abs(w_date - w_game) / w_game
        assert relative_diff < 0.5  # Generous bound; the point is "not wildly different."

    def test_determinism_same_seed_same_result(self):
        games = _make_synthetic_games(n_games=300, home_win_prob=0.6, seed=SEED)
        r1 = bt.run_backtest(games, n_splits=3, min_train_size=100, seed=7)
        r2 = bt.run_backtest(games, n_splits=3, min_train_size=100, seed=7)
        assert r1["aggregate"]["model_brier"] == r2["aggregate"]["model_brier"]
        assert (
            r1["clustering_sensitivity"]["by_game_id"]["ci_low"]
            == r2["clustering_sensitivity"]["by_game_id"]["ci_low"]
        )

    def test_empty_games_raises(self):
        with pytest.raises(ValueError):
            bt.run_backtest([])

    def test_unsorted_input_is_sorted_internally(self):
        # One game per day (games_per_day=1) so game_date is a total order
        # with no ties -- otherwise reversing the input legitimately changes
        # same-day tie-break order under Python's stable sort, which cascades
        # into a different (still non-leaking) ELO trajectory. That is a
        # property of ties, not a bug in run_backtest's sorting.
        games = _make_synthetic_games(n_games=300, home_win_prob=0.6, games_per_day=1, seed=SEED)
        shuffled = list(reversed(games))
        result_sorted = bt.run_backtest(games, n_splits=3, min_train_size=100)
        result_shuffled = bt.run_backtest(shuffled, n_splits=3, min_train_size=100)
        assert (
            result_sorted["aggregate"]["model_brier"]
            == pytest.approx(result_shuffled["aggregate"]["model_brier"])
        )


# ---------------------------------------------------------------------------
# Default model (GBM + Platt calibration) actually trains and predicts
# ---------------------------------------------------------------------------


class TestDefaultModel:
    def test_default_model_beats_coin_flip_with_real_signal(self):
        # A league where home_win_prob (0.75) is far from 0.5 gives ELO (and
        # therefore the GBM riding on ELO features) a strong, learnable
        # signal: the model should land well below the 0.25 Brier score of
        # random guessing.
        games = _make_synthetic_games(n_games=600, home_win_prob=0.75, seed=SEED)
        result = bt.run_backtest(games, n_splits=4, min_train_size=150)
        assert result["aggregate"]["model_brier"] < 0.22

    def test_single_class_training_window_falls_back_gracefully(self):
        # home_win_prob=1.0 guarantees every training window has only the
        # positive class -- this must not raise, it must use the constant
        # fallback (_ConstantProbabilityModel).
        games = _make_synthetic_games(n_games=200, home_win_prob=1.0, seed=SEED)
        result = bt.run_backtest(games, n_splits=3, min_train_size=60)
        for w in result["windows"]:
            assert w["model_brier"] == pytest.approx(0.0, abs=1e-9)
