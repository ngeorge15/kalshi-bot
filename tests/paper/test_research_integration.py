"""Integration tests for the research surfaces wired into report() and the CLI.

Covers the seam between the paper broker and the three research modules
(`events`/`uncertainty`, `protocol`, `health`/`marks`). The modules have their
own unit tests; these assert they are correctly connected and that the wiring
did not weaken the broker's existing determinism guarantee.
"""
import json

import pytest

from src.paper.broker import PaperBroker
from src.paper.cli import main


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(str(tmp_path / "paper.db"))


def _stamp(step):
    """Monotonic timestamps: the broker rejects an event tape that moves backwards."""
    return f"2026-09-04T{12 + step // 3600:02d}:{step // 60 % 60:02d}:{step % 60:02d}Z"


def build(broker, brackets_per_event, n_events):
    """Settle `n_events` events, each offered as `brackets_per_event` markets.

    Quotes and forecasts for every market are emitted first in strictly
    increasing time, then all settlements, so the tape never moves backwards.
    Each `process` result is checked, so a silently rejected event cannot make a
    test pass with an empty report.
    """
    step = 0
    tickers = []
    for event_index in range(n_events):
        event_key = f"NYC-2026-09-{event_index:02d}"
        for bracket in range(brackets_per_event):
            ticker = f"T{len(tickers) + 1}"
            tickers.append(ticker)
            timestamp = _stamp(step)
            assert broker.process({
                "event_id": f"q{ticker}", "type": "quote", "at": timestamp,
                "observed_at": timestamp, "ticker": ticker, "market_type": "temperature",
                "event_key": event_key, "close_at": "2026-09-04T18:00:00Z",
                "available": True, "yes_asks": [[45, 5]], "no_asks": [[60, 20]],
            })["status"] == "observed"
            step += 1
            assert broker.process({
                "event_id": f"p{ticker}", "type": "forecast", "at": _stamp(step),
                "ticker": ticker, "yes_probability": 0.6 + 0.01 * bracket,
                "model_name": "weather", "model_version": "1",
            })["status"] in {"resting", "filled", "skipped"}
            step += 1
    for ticker in tickers:
        broker.process({"event_id": f"s{ticker}", "type": "settlement",
                        "at": "2026-09-04T18:00:00Z", "ticker": ticker, "result": "yes"})
    report = broker.report()
    assert report["prediction_count"] == len(tickers), "tape did not record every forecast"
    return report


class TestClusteredScoresWired:
    """The correlated-bracket correction reaches report()."""

    def test_every_score_carries_the_clustered_block(self, broker):
        report = build(broker, brackets_per_event=3, n_events=2)
        assert report["scores"]
        for score in report["scores"]:
            assert "event_clustered" in score

    def test_events_are_fewer_than_markets(self, broker):
        report = build(broker, brackets_per_event=5, n_events=3)
        clustered = report["scores"][0]["event_clustered"]
        assert clustered["n_markets"] == 15
        assert clustered["n_events"] == 3

    def test_per_market_figures_are_unchanged(self, broker):
        """Existing keys keep their original per-market meaning."""
        report = build(broker, brackets_per_event=4, n_events=2)
        score = report["scores"][0]
        assert score["n_markets"] == 8
        assert score["brier_improvement"] == pytest.approx(
            score["market_brier"] - score["model_brier"])

    def test_single_event_reports_no_interval(self, broker):
        """One event cannot support an interval; it must not be fabricated."""
        report = build(broker, brackets_per_event=10, n_events=1)
        clustered = report["scores"][0]["event_clustered"]
        assert clustered["n_markets"] == 10
        assert clustered["n_events"] == 1
        assert clustered["ci_low"] is None and clustered["ci_high"] is None

    def test_many_brackets_one_event_is_flagged_degenerate(self, broker):
        report = build(broker, brackets_per_event=10, n_events=1)
        assert report["scores"][0]["event_clustered"]["degenerate"]["case"] == "single_event"

    def test_interval_present_with_several_events(self, broker):
        report = build(broker, brackets_per_event=2, n_events=4)
        clustered = report["scores"][0]["event_clustered"]
        assert clustered["ci_low"] is not None
        assert clustered["ci_low"] <= clustered["brier_improvement"] <= clustered["ci_high"]

    def test_clustering_shape_is_reported(self, broker):
        report = build(broker, brackets_per_event=5, n_events=2)
        shape = report["scores"][0]["event_clustered"]["clustering"]
        assert shape["n_events"] == 2
        assert shape["max_markets_per_event"] == 5


class TestValuationWired:
    """Mark-to-market reaches report() alongside equity at cost."""

    def test_report_exposes_market_valuation(self, broker):
        report = build(broker, brackets_per_event=1, n_events=1)
        for key in ("equity_at_market_cents", "market_value_cents",
                    "unvaluable_position_count", "unvaluable_positions"):
            assert key in report

    def test_fresh_experiment_has_no_valuation(self, broker):
        """No events processed means no experiment clock to value against."""
        report = broker.report()
        assert report["equity_at_market_cents"] is None
        assert report["unvaluable_position_count"] == 0


class TestDeterminismPreserved:
    """The broker's reopen-equality guarantee must survive the new fields."""

    def test_reopened_report_is_identical(self, broker):
        report = build(broker, brackets_per_event=3, n_events=2)
        assert PaperBroker(broker.db_path).report() == report

    def test_report_is_json_serialisable_without_nan(self, broker):
        """cli.py dumps with allow_nan=False, so a NaN would raise here."""
        report = build(broker, brackets_per_event=3, n_events=2)
        assert json.dumps(report, allow_nan=False)

    def test_repeated_reports_match(self, broker):
        build(broker, brackets_per_event=2, n_events=3)
        assert broker.report() == broker.report()


class TestEvidenceNote:
    """The note must warn about the naive figures it still reports."""

    def test_names_the_clustered_alternative(self, broker):
        note = build(broker, brackets_per_event=2, n_events=2)["evidence_note"]
        assert "event_clustered" in note
        assert "overstate" in note

    def test_distinguishes_cost_from_market_equity(self, broker):
        note = build(broker, brackets_per_event=1, n_events=1)["evidence_note"]
        assert "equity_at_market_cents" in note


class TestCliSurfaces:
    def _init(self, tmp_path):
        db = str(tmp_path / "cli.db")
        assert main(["--db", db, "init"]) == 0
        return db

    def test_health_requires_events_first(self, tmp_path, capsys):
        db = self._init(tmp_path)
        assert main(["--db", db, "health"]) == 2
        assert "nothing to check" in capsys.readouterr().err

    def test_health_runs_after_events(self, tmp_path, capsys):
        db = self._init(tmp_path)
        broker = PaperBroker(db)
        build(broker, brackets_per_event=1, n_events=1)
        capsys.readouterr()  # discard init output so only the health JSON remains
        assert main(["--db", db, "health"]) == 0
        assert json.loads(capsys.readouterr().out)["severity"] in {"ok", "warn", "fail"}

    def test_protocol_declare_then_verify(self, tmp_path, capsys):
        db = self._init(tmp_path)
        config = tmp_path / "proto_config.json"
        config.write_text(json.dumps({
            "hypothesis": "Baseline beats market on clustered paired Brier",
            "primary_metric": "event_clustered_brier_improvement_ci_low",
            "secondary_metrics": ["point_estimate"], "min_events": 30,
            "decision_threshold": 0.01,
            "stopping_rule": "Stop after 30 settled events or 2026-12-31, whichever first",
            "run_kind": "forward", "declared_by": "test"}))
        saved = str(tmp_path / "declared.json")
        assert main(["--db", db, "protocol-declare", "--config", str(config),
                     "--output", saved]) == 0
        capsys.readouterr()
        assert main(["--db", db, "protocol-verify", saved]) == 0
        assert json.loads(capsys.readouterr().out)["valid"] is True

    def test_tampered_protocol_fails_verification(self, tmp_path, capsys):
        db = self._init(tmp_path)
        config = tmp_path / "proto_config.json"
        config.write_text(json.dumps({
            "hypothesis": "Baseline beats market on clustered paired Brier",
            "primary_metric": "event_clustered_brier_improvement_ci_low",
            "secondary_metrics": [], "min_events": 30, "decision_threshold": 0.01,
            "stopping_rule": "Stop after 30 settled events or 2026-12-31, whichever first",
            "run_kind": "forward", "declared_by": "test"}))
        saved = tmp_path / "declared.json"
        main(["--db", db, "protocol-declare", "--config", str(config), "--output", str(saved)])
        capsys.readouterr()

        tampered = json.loads(saved.read_text())
        tampered["decision_threshold"] = 0.0
        saved.write_text(json.dumps(tampered, indent=2, sort_keys=True))

        assert main(["--db", db, "protocol-verify", str(saved)]) == 2
        assert json.loads(capsys.readouterr().out)["valid"] is False

    def test_evaluate_refuses_below_two_events(self, tmp_path, capsys):
        """A conclusion needs an interval, and one event cannot produce one."""
        db = self._init(tmp_path)
        broker = PaperBroker(db)
        build(broker, brackets_per_event=8, n_events=1)
        config = tmp_path / "proto_config.json"
        config.write_text(json.dumps({
            "hypothesis": "Baseline beats market on clustered paired Brier",
            "primary_metric": "event_clustered_brier_improvement_ci_low",
            "secondary_metrics": [], "min_events": 30, "decision_threshold": 0.01,
            "stopping_rule": "Stop after 30 settled events or 2026-12-31, whichever first",
            "run_kind": "forward", "declared_by": "test"}))
        saved = str(tmp_path / "declared.json")
        main(["--db", db, "protocol-declare", "--config", config.as_posix(), "--output", saved])
        capsys.readouterr()
        assert main(["--db", db, "protocol-evaluate", saved,
                     "--model", "weather", "--model-version", "1"]) == 2
        assert "too few" in capsys.readouterr().err

    def test_evaluate_reports_insufficient_data_not_failure(self, tmp_path, capsys):
        """Enough events for an interval, but below the predeclared minimum."""
        db = self._init(tmp_path)
        broker = PaperBroker(db)
        build(broker, brackets_per_event=1, n_events=4)
        config = tmp_path / "proto_config.json"
        config.write_text(json.dumps({
            "hypothesis": "Baseline beats market on clustered paired Brier",
            "primary_metric": "event_clustered_brier_improvement_ci_low",
            "secondary_metrics": ["point_estimate"], "min_events": 30,
            "decision_threshold": 0.01,
            "stopping_rule": "Stop after 30 settled events or 2026-12-31, whichever first",
            "run_kind": "forward", "declared_by": "test"}))
        saved = str(tmp_path / "declared.json")
        main(["--db", db, "protocol-declare", "--config", config.as_posix(), "--output", saved])
        capsys.readouterr()
        assert main(["--db", db, "protocol-evaluate", saved,
                     "--model", "weather", "--model-version", "1"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "insufficient_data"

    def test_evaluate_rejects_unknown_model(self, tmp_path, capsys):
        db = self._init(tmp_path)
        broker = PaperBroker(db)
        build(broker, brackets_per_event=1, n_events=3)
        config = tmp_path / "proto_config.json"
        config.write_text(json.dumps({
            "hypothesis": "Baseline beats market on clustered paired Brier",
            "primary_metric": "event_clustered_brier_improvement_ci_low",
            "secondary_metrics": [], "min_events": 2, "decision_threshold": 0.01,
            "stopping_rule": "Stop after 30 settled events or 2026-12-31, whichever first",
            "run_kind": "forward", "declared_by": "test"}))
        saved = str(tmp_path / "declared.json")
        main(["--db", db, "protocol-declare", "--config", config.as_posix(), "--output", saved])
        capsys.readouterr()
        assert main(["--db", db, "protocol-evaluate", saved,
                     "--model", "nonexistent", "--model-version", "1"]) == 2
        assert "No scored markets" in capsys.readouterr().err
