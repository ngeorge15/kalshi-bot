"""Phase 5 end-to-end test — the ROADMAP's stated working test for R10.

    "Simulate a week of paper trading, generate full performance report +
     snapshot JSON, verify per-market-type breakdowns are correct."

This runs the real path a human or the Phase 6 evaluator would: seed a week,
rebuild daily P&L, render the report, write the snapshot and the plot, and check
the per-market-type numbers against the seeder's independently recorded ground
truth.
"""

import json
import subprocess
import sys

import pytest

from scripts.seed_test_data import seed_database
from src.analytics import daily_report, edge_decay, performance, snapshot
from src.db.database import Database


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    """A simulated week of paper trading, plus the ground-truth manifest."""
    path = tmp_path_factory.mktemp("phase5") / "week.db"
    db = Database(str(path))
    manifest = seed_database(db, n_days=7, seed=42)
    return db, manifest, str(path)


class TestSimulatedWeek:
    def test_a_week_of_trades_settled(self, week):
        _, manifest, _ = week
        assert manifest["n_days"] == 7
        assert manifest["total_trades"] >= 60

    def test_spans_multiple_days(self, week):
        db, _, _ = week
        days = db.fetchall("SELECT DISTINCT DATE(settled_at) AS d FROM outcomes")
        assert len(days) == 7


class TestFullReport:
    """R10.8"""

    def test_report_renders(self, week):
        db, _, _ = week
        report = daily_report.build_report(db)
        assert "KALSHI BOT — DAILY REPORT" in report
        assert "BY MARKET TYPE" in report
        assert "ALL TIME" in report

    def test_report_lists_every_market_type(self, week):
        db, manifest, _ = week
        report = daily_report.build_report(db)
        for market_type in manifest["market_types"]:
            assert market_type in report, market_type

    def test_report_includes_third_domain(self, week):
        """D5-07: the report must not have a fixed list of sports."""
        db, _, _ = week
        assert "cbb_games" in daily_report.build_report(db)

    def test_report_never_prints_raw_none(self, week):
        """'None' leaking into a report is a formatting bug; 'n/a' is intended."""
        db, _, _ = week
        assert "None" not in daily_report.build_report(db)

    def test_report_on_empty_database(self, tmp_path):
        db = Database(str(tmp_path / "empty.db"))
        report = daily_report.build_report(db)
        assert "no settled trades yet" in report
        assert "None" not in report

    def test_sharpe_is_computed_not_na(self, week):
        """Regression: rebuilding only the requested day left Sharpe at n/a."""
        db, _, _ = week
        report = daily_report.build_report(db)
        sharpe_line = next(l for l in report.splitlines() if "Sharpe" in l)
        assert "n/a" not in sharpe_line

    def test_cli_runs(self, week):
        _, _, path = week
        result = subprocess.run(
            [sys.executable, "-m", "src.analytics.daily_report", "--db", path],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "DAILY REPORT" in result.stdout


class TestPerMarketTypeBreakdowns:
    """The ROADMAP's explicit requirement: breakdowns must be *correct*."""

    def test_trade_counts_match_ground_truth(self, week):
        db, manifest, _ = week
        breakdown = performance.brier_by_market_type(db)
        for market_type, truth in manifest["by_market_type"].items():
            assert breakdown[market_type]["n"] == truth["n_trades"], market_type

    def test_pnl_matches_ground_truth(self, week):
        db, manifest, _ = week
        breakdown = performance.pnl_by_market_type(db)
        for market_type, truth in manifest["by_market_type"].items():
            assert breakdown[market_type]["total_pnl_cents"] == truth["pnl_cents"], market_type

    def test_calibration_drift_matches_ground_truth(self, week):
        db, manifest, _ = week
        breakdown = performance.brier_by_market_type(db)
        for market_type, truth in manifest["by_market_type"].items():
            expected = truth["mean_predicted_prob"] - truth["actual_frequency"]
            assert breakdown[market_type]["calibration_drift"] == pytest.approx(expected)

    def test_breakdowns_sum_to_the_totals(self, week):
        db, _, _ = week
        breakdown = performance.pnl_by_market_type(db)
        assert sum(b["total_pnl_cents"] for b in breakdown.values()) == \
            performance.win_rate(db)["total_pnl_cents"]
        assert sum(b["n"] for b in breakdown.values()) == performance.win_rate(db)["n"]

    def test_every_market_type_reconciles_exactly(self, week):
        """Correctness is per-type reconciliation, not the sign of any one number."""
        db, manifest, _ = week
        breakdown = performance.pnl_by_market_type(db)
        assert set(breakdown) == set(manifest["market_types"])
        for market_type, truth in manifest["by_market_type"].items():
            assert breakdown[market_type]["n"] == truth["n_trades"], market_type


class TestSnapshotArtifact:
    """R10.7"""

    def test_writes_valid_json(self, week, tmp_path):
        db, _, _ = week
        out = str(tmp_path / "snapshot.json")
        snapshot.write_snapshot(db, out, include_config=False)
        with open(out) as fh:
            loaded = json.load(fh)
        assert loaded["schema_version"] == snapshot.SNAPSHOT_SCHEMA_VERSION

    def test_snapshot_breakdowns_agree_with_manifest(self, week, tmp_path):
        db, manifest, _ = week
        out = str(tmp_path / "snapshot2.json")
        snapshot.write_snapshot(db, out, include_config=False)
        with open(out) as fh:
            loaded = json.load(fh)
        for market_type, truth in manifest["by_market_type"].items():
            assert loaded["by_market_type"]["pnl"][market_type]["total_pnl_cents"] == \
                truth["pnl_cents"]

    def test_snapshot_carries_every_evaluator_section(self, week):
        db, _, _ = week
        s = snapshot.generate_snapshot(db, include_config=False)
        for section in ("overall", "rolling", "by_market_type", "by_model",
                        "calibration", "win_rate_by_edge_bucket", "edge_decay",
                        "recent_trades"):
            assert section in s, section

    def test_sharpe_and_drawdown_are_computed(self, week):
        db, _, _ = week
        s = snapshot.generate_snapshot(db, include_config=False)
        assert s["overall"]["sharpe_ratio"] is not None
        assert s["overall"]["drawdown"]["max_drawdown_cents"] >= 0


class TestPlotArtifact:
    """R10.2's image half."""

    def test_writes_a_png(self, week, tmp_path):
        db, _, _ = week
        out = str(tmp_path / "plots" / "calibration.png")
        performance.render_reliability_diagram(performance.calibration_bins(db), out)
        with open(out, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_renders_on_empty_data(self, tmp_path):
        """Must not crash before any trade has settled."""
        db = Database(str(tmp_path / "empty2.db"))
        out = str(tmp_path / "empty.png")
        performance.render_reliability_diagram(performance.calibration_bins(db), out)
        with open(out, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"


class TestEdgeDecayOverTheWeek:
    """R10.6"""

    def test_both_populations_observed(self, week):
        db, _, _ = week
        p = edge_decay.edge_persistence(db)
        assert 0.0 < p["persistence_rate"] < 1.0

    def test_edges_shrink_toward_settlement(self, week):
        db, _, _ = week
        by_label = {b["label"]: b for b in edge_decay.decay_by_bucket(db)}
        assert by_label["48h+"]["mean_abs_edge_cents"] > by_label["0-3h"]["mean_abs_edge_cents"]


class TestConsistencyAcrossSurfaces:
    """Report, snapshot and direct queries must not disagree."""

    def test_totals_agree(self, week):
        db, _, _ = week
        s = snapshot.generate_snapshot(db, include_config=False)
        direct = performance.win_rate(db)
        assert s["overall"]["total_pnl_cents"] == direct["total_pnl_cents"]
        assert s["overall"]["n"] == direct["n"]

    def test_daily_pnl_sums_to_cumulative(self, week):
        db, _, _ = week
        performance.recompute_all_daily_pnl(db)
        row = db.fetchone("SELECT SUM(realized_pnl_cents) AS total FROM daily_pnl")
        assert row["total"] == performance.win_rate(db)["total_pnl_cents"]


class TestPatternDetectionRequiresVolume:
    """A simulated week is enough to verify the machinery, not to detect the patterns.

    The ROADMAP's Phase 5 working test says "simulate a week of paper trading",
    and that is the right scope for checking the breakdowns reconcile. But
    Phase 6's working test asks the evaluator to *diagnose* planted patterns --
    props overconfident by 8%, one market type consistently losing -- and a week
    does not carry the samples to support either claim.

    At the ~8 precipitation trades a seeded week produces, the P&L sign is a coin
    flip regardless of the planted bias; the drift standard error is
    sqrt(p(1-p)/n) ~ 0.17. These tests pin the volume at which the patterns
    actually become visible, so Phase 6 seeds accordingly rather than asking an
    LLM to find signal that is not there.
    """

    @pytest.fixture(scope="class")
    def long_run(self, tmp_path_factory):
        path = tmp_path_factory.mktemp("phase5_long") / "long.db"
        db = Database(str(path))
        return db, seed_database(db, n_days=10, seed=42, trades_per_day=200)

    def test_week_alone_is_underpowered(self, week):
        """Documents the limitation rather than pretending it does not exist."""
        _, manifest, _ = week
        assert manifest["by_market_type"]["precipitation"]["n_trades"] < 30

    def test_losing_market_type_detectable_at_volume(self, long_run):
        db, _ = long_run
        assert performance.pnl_by_market_type(db)["precipitation"]["total_pnl_cents"] < 0

    def test_overconfidence_detectable_at_volume(self, long_run):
        db, _ = long_run
        breakdown = performance.brier_by_market_type(db)
        assert breakdown["props"]["calibration_drift"] > 0.03
        assert breakdown["precipitation"]["calibration_drift"] > 0.09

    def test_calibrated_types_stay_flat_at_volume(self, long_run):
        db, _ = long_run
        breakdown = performance.brier_by_market_type(db)
        for market_type in ("games", "temperature"):
            assert abs(breakdown[market_type]["calibration_drift"]) < 0.06

    def test_biased_types_rank_above_calibrated_ones(self, long_run):
        """The signal Phase 6's evaluator must be able to pick up."""
        db, _ = long_run
        drift = {
            mt: stats["calibration_drift"]
            for mt, stats in performance.brier_by_market_type(db).items()
        }
        assert drift["props"] > drift["games"]
        assert drift["precipitation"] > drift["temperature"]

    def test_reliability_diagram_shows_the_bias(self, long_run):
        """Props bins should sit below the diagonal: predicted above observed."""
        db, _ = long_run
        bins = performance.calibration_bins(db, market_type="props")
        populated = [b for b in bins if b["count"] >= 20]
        below = sum(1 for b in populated if b["mean_predicted"] > b["actual_frequency"])
        assert below > len(populated) / 2
