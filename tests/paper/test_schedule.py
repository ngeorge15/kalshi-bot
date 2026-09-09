"""Scheduling and coverage tracking for repeated observations.

No network: `observe_once` is exercised through an injected reader in the one
test that needs it, and every other test drives the journal directly.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.paper.broker import PaperBroker
from src.paper.config import PaperConfig
from src.paper import schedule


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(str(tmp_path / "sched.db"), PaperConfig(run_kind="forward"))


def entry(ticker="TEMP-NYC"):
    return {"ticker": ticker, "market_type": "temperature", "event_key": "NYC-2026-09-08"}


def attempt(broker, ticker, when, status="observed", reason=None):
    schedule.record_attempts(broker.db_path,
                             [{"ticker": ticker, "status": status, "reason": reason}], when)


class TestDueSelection:
    def test_never_observed_is_due(self, broker):
        assert schedule.due_entries(broker.db_path, [entry()], NOW) == [entry()]

    def test_recently_observed_is_not_due(self, broker):
        attempt(broker, "TEMP-NYC", NOW - timedelta(minutes=10))
        assert schedule.due_entries(broker.db_path, [entry()], NOW) == []

    def test_due_again_after_the_interval(self, broker):
        attempt(broker, "TEMP-NYC", NOW - timedelta(seconds=3600))
        assert schedule.due_entries(broker.db_path, [entry()], NOW) == [entry()]

    def test_interval_boundary_is_inclusive(self, broker):
        attempt(broker, "TEMP-NYC", NOW - timedelta(seconds=60))
        assert schedule.due_entries(broker.db_path, [entry()], NOW, 60) == [entry()]
        assert schedule.due_entries(broker.db_path, [entry()], NOW, 61) == []

    def test_tickers_are_scheduled_independently(self, broker):
        attempt(broker, "A", NOW - timedelta(minutes=5))
        due = schedule.due_entries(broker.db_path, [entry("A"), entry("B")], NOW)
        assert [d["ticker"] for d in due] == ["B"]

    def test_failed_attempts_still_back_off(self, broker):
        """Retrying a failing ticker in a tight loop would hammer a public API."""
        attempt(broker, "TEMP-NYC", NOW - timedelta(minutes=1), status="error", reason="boom")
        assert schedule.due_entries(broker.db_path, [entry()], NOW) == []

    def test_malformed_entry_is_surfaced_not_dropped(self, broker):
        assert schedule.due_entries(broker.db_path, [{"ticker": "  "}], NOW) == [{"ticker": "  "}]

    def test_rejects_non_list_watchlist(self, broker):
        with pytest.raises(ValueError, match="must be a JSON list"):
            schedule.due_entries(broker.db_path, {"ticker": "A"}, NOW)

    def test_rejects_bad_interval(self, broker):
        for bad in (0, -1, 1.5):
            with pytest.raises(ValueError, match="positive integer"):
                schedule.due_entries(broker.db_path, [entry()], NOW, bad)


class TestJournal:
    def test_records_every_result_including_skips(self, broker):
        written = schedule.record_attempts(broker.db_path, [
            {"ticker": "A", "status": "observed"},
            {"ticker": "B", "status": "skipped", "reason": "eligibility_missing_or_expired"},
            {"ticker": "C", "status": "error", "reason": "timeout"},
        ], NOW)
        assert written == 3

    def test_last_observed_at_is_none_before_any_attempt(self, broker):
        assert schedule.last_observed_at(broker.db_path, "A") is None

    def test_last_observed_at_returns_latest(self, broker):
        attempt(broker, "A", NOW - timedelta(hours=2))
        attempt(broker, "A", NOW - timedelta(hours=1))
        assert schedule.last_observed_at(broker.db_path, "A") == (NOW - timedelta(hours=1)).isoformat()

    def test_missing_ticker_is_journalled_as_unknown(self, broker):
        schedule.record_attempts(broker.db_path, [{"status": "error", "reason": "no ticker"}], NOW)
        assert schedule.last_observed_at(broker.db_path, "unknown") == NOW.isoformat()


class TestNextDue:
    def test_none_when_a_ticker_was_never_attempted(self, broker):
        assert schedule.next_due_at(broker.db_path, [entry()]) is None

    def test_earliest_across_tickers(self, broker):
        attempt(broker, "A", NOW - timedelta(minutes=50))
        attempt(broker, "B", NOW - timedelta(minutes=10))
        expected = (NOW - timedelta(minutes=50) + timedelta(seconds=3600)).isoformat()
        assert schedule.next_due_at(broker.db_path, [entry("A"), entry("B")]) == expected

    def test_none_on_empty_watchlist(self, broker):
        assert schedule.next_due_at(broker.db_path, []) is None


class TestGaps:
    def test_continuous_series_has_no_gaps(self, broker):
        for hours in range(5):
            attempt(broker, "A", NOW - timedelta(hours=4 - hours))
        assert schedule.collection_gaps(broker.db_path, "A") == []

    def test_pause_is_reported(self, broker):
        attempt(broker, "A", NOW - timedelta(days=4))
        attempt(broker, "A", NOW)
        gaps = schedule.collection_gaps(broker.db_path, "A")
        assert len(gaps) == 1
        assert gaps[0]["seconds"] == pytest.approx(4 * 86400)

    def test_single_attempt_cannot_form_a_gap(self, broker):
        attempt(broker, "A", NOW)
        assert schedule.collection_gaps(broker.db_path, "A") == []

    def test_gap_threshold_is_tunable(self, broker):
        attempt(broker, "A", NOW - timedelta(seconds=7200))
        attempt(broker, "A", NOW)
        assert schedule.collection_gaps(broker.db_path, "A", 3600, gap_multiple=3) == []
        assert len(schedule.collection_gaps(broker.db_path, "A", 600, gap_multiple=3)) == 1


class TestCoverage:
    def test_empty_history(self, broker):
        report = schedule.coverage_report(broker.db_path, [entry()], NOW)
        assert report["total_attempts"] == 0
        assert report["tickers_never_observed"] == ["TEMP-NYC"]

    def test_counts_attempts_and_observations_separately(self, broker):
        """Skips and errors are attempts but not observations."""
        attempt(broker, "A", NOW - timedelta(hours=3), status="observed")
        attempt(broker, "A", NOW - timedelta(hours=2), status="skipped", reason="not eligible")
        attempt(broker, "A", NOW - timedelta(hours=1), status="error", reason="timeout")
        stats = schedule.coverage_report(broker.db_path, [entry("A")], NOW)["tickers"][0]
        assert stats["attempts"] == 3
        assert stats["observed"] == 1

    def test_reports_first_and_last_attempt(self, broker):
        attempt(broker, "A", NOW - timedelta(hours=5))
        attempt(broker, "A", NOW)
        stats = schedule.coverage_report(broker.db_path, [entry("A")], NOW)["tickers"][0]
        assert stats["first_attempt_at"] == (NOW - timedelta(hours=5)).isoformat()
        assert stats["last_attempt_at"] == NOW.isoformat()

    def test_aggregates_gaps(self, broker):
        attempt(broker, "A", NOW - timedelta(days=5))
        attempt(broker, "A", NOW)
        assert schedule.coverage_report(broker.db_path, [entry("A")], NOW)["total_gaps"] == 1

    def test_note_warns_that_gaps_are_not_random(self, broker):
        note = schedule.coverage_report(broker.db_path, [entry()], NOW)["note"]
        assert "not" in note and "random" in note


class TestScheduledPass:
    def test_does_nothing_when_nothing_is_due(self, broker):
        attempt(broker, "TEMP-NYC", NOW - timedelta(minutes=1))
        outcome = schedule.run_scheduled_pass(broker, [entry()], NOW)
        assert outcome["ran"] is False
        assert outcome["due_count"] == 0
        assert outcome["results"] == []

    def test_runs_and_journals_when_due(self, broker):
        """Eligibility is absent here, so observe_once skips -- still an attempt."""
        outcome = schedule.run_scheduled_pass(broker, [entry()], NOW)
        assert outcome["ran"] is True
        assert outcome["recorded"] == 1
        assert schedule.last_observed_at(broker.db_path, "TEMP-NYC") == NOW.isoformat()

    def test_second_pass_is_suppressed_by_the_interval(self, broker):
        schedule.run_scheduled_pass(broker, [entry()], NOW)
        assert schedule.run_scheduled_pass(broker, [entry()], NOW + timedelta(minutes=5))["ran"] is False

    def test_requires_forward_mode(self, tmp_path):
        replay_broker = PaperBroker(str(tmp_path / "replay.db"), PaperConfig(run_kind="replay"))
        with pytest.raises(ValueError, match="forward paper experiment"):
            schedule.run_scheduled_pass(replay_broker, [entry()], NOW)

    def test_reports_next_due_time(self, broker):
        outcome = schedule.run_scheduled_pass(broker, [entry()], NOW)
        assert outcome["next_due_at"] == (NOW + timedelta(seconds=3600)).isoformat()
