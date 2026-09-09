"""Event clustering invariants: grouping, summaries, and degeneracy detection."""
import pytest

from src.paper.events import cluster_by_event, cluster_summary, degenerate_clustering


def row(event_key="EVT-1", **changes):
    return {"event_key": event_key, "model_brier": 0.1, "market_brier": 0.2, **changes}


def test_cluster_by_event_groups_and_sorts_stably():
    rows = [row("B", model_brier=1), row("A", model_brier=2), row("B", model_brier=3), row("A", model_brier=4)]
    clusters = cluster_by_event(rows)
    assert [c["event_key"] for c in clusters] == ["A", "B"]
    assert [c["n_markets"] for c in clusters] == [2, 2]
    # Original relative order preserved within each cluster.
    assert [r["model_brier"] for r in clusters[0]["rows"]] == [2, 4]
    assert [r["model_brier"] for r in clusters[1]["rows"]] == [1, 3]


def test_cluster_by_event_empty_input():
    assert cluster_by_event([]) == []


@pytest.mark.parametrize("bad_key", [None, "", "   "])
def test_cluster_by_event_rejects_invalid_event_key(bad_key):
    with pytest.raises(ValueError, match="event_key"):
        cluster_by_event([row(event_key=bad_key)])


def test_cluster_by_event_rejects_missing_event_key():
    with pytest.raises(ValueError, match="event_key"):
        cluster_by_event([{"model_brier": 0.1, "market_brier": 0.2}])


def test_cluster_summary_basic():
    rows = [row("A")] * 3 + [row("B")] * 1
    summary = cluster_summary(rows)
    assert summary == {"n_markets": 4, "n_events": 2, "mean_markets_per_event": 2.0,
                        "max_markets_per_event": 3, "largest_event_share": 0.75}


def test_cluster_summary_empty_returns_none_statistics():
    summary = cluster_summary([])
    assert summary["n_markets"] == 0
    assert summary["n_events"] == 0
    assert summary["mean_markets_per_event"] is None
    assert summary["max_markets_per_event"] is None
    assert summary["largest_event_share"] is None


def test_degenerate_clustering_no_clustering_case():
    rows = [row(f"EVT-{i}") for i in range(5)]
    verdict = degenerate_clustering(rows)
    assert verdict["degenerate"] is True
    assert verdict["case"] == "no_clustering"
    assert "unique event_key" in verdict["reason"]
    assert verdict["n_markets"] == 5
    assert verdict["n_events"] == 5


def test_degenerate_clustering_single_event_case():
    rows = [row("ONLY") for _ in range(22)]
    verdict = degenerate_clustering(rows)
    assert verdict["degenerate"] is True
    assert verdict["case"] == "single_event"
    assert "one independent observation" in verdict["reason"]
    assert verdict["n_markets"] == 22
    assert verdict["n_events"] == 1


def test_degenerate_clustering_healthy_mix_is_not_degenerate():
    rows = [row("A")] * 3 + [row("B")] * 3 + [row("C")] * 3
    verdict = degenerate_clustering(rows)
    assert verdict["degenerate"] is False
    assert verdict["case"] is None
    assert verdict["reason"] is None


def test_degenerate_clustering_empty_is_not_degenerate():
    verdict = degenerate_clustering([])
    assert verdict == {"degenerate": False, "case": None, "reason": None, "n_markets": 0, "n_events": 0}


def test_degenerate_clustering_single_market_is_not_degenerate():
    # One market trivially satisfies both n_events==n_markets and n_events==1;
    # there is no meaningful distinction to report at n=1.
    verdict = degenerate_clustering([row("ONLY")])
    assert verdict["degenerate"] is False
