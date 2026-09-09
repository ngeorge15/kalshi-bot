"""Predeclaration protocol invariants: integrity, states, and fishing guards."""
from dataclasses import replace
import json

import pytest

from src.paper.protocol import (
    Protocol,
    Results,
    VerdictState,
    declare,
    evaluate,
    is_valid,
    load,
    save,
    verify,
)

DECLARED_AT = "2026-09-04T12:00:00Z"


def make_protocol(**changes):
    fields = dict(
        hypothesis="A weather-baseline model beats the market on temperature brackets.",
        primary_metric="event_clustered_paired_brier_improvement",
        secondary_metrics=["realized_pnl_cents", "fill_rate"],
        min_events=30,
        decision_threshold=0.01,
        stopping_rule="Stop after 30 independent city-day events or on 2026-12-31, whichever comes first.",
        run_kind="replay",
        declared_by="nikhi",
        declared_at=DECLARED_AT,
    )
    fields.update(changes)
    return declare(**fields)


def test_declare_returns_self_consistent_protocol():
    protocol = make_protocol()
    assert isinstance(protocol, Protocol)
    assert len(protocol.content_hash) == 64
    assert is_valid(protocol)


def test_declared_at_normalized_to_utc():
    protocol = make_protocol(declared_at="2026-09-04T08:00:00-04:00")
    assert protocol.declared_at == "2026-09-04T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Correctness bar: hand-editing a saved protocol must be caught, loudly.
# ---------------------------------------------------------------------------

def test_hand_edited_saved_protocol_fails_verification(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    save(protocol, path)

    # Mutate the file directly, as a researcher tempted to loosen the bar
    # after seeing results might, without recomputing the hash.
    raw = json.loads(path.read_text())
    raw["decision_threshold"] = 0.0001
    path.write_text(json.dumps(raw))

    result = verify(protocol, path)
    assert result.valid is False
    assert "modified after declaration" in result.message
    assert str(path) in result.message


def test_hand_edited_protocol_yields_invalid_protocol_verdict(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    save(protocol, path)

    raw = json.loads(path.read_text())
    raw["min_events"] = 1
    path.write_text(json.dumps(raw))

    tampered = load(path)  # still passes field-level validation
    assert tampered.min_events == 1
    assert not is_valid(tampered)

    results = Results(n_events=1, primary_metric_value=0.5)
    verdict = evaluate(tampered, results)
    assert verdict.state == VerdictState.INVALID_PROTOCOL
    assert "content_hash" in verdict.explanation


def test_verify_round_trip_succeeds(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    save(protocol, path)
    result = verify(protocol, path)
    assert result.valid is True
    assert result.mismatched_fields == ()


def test_verify_reports_specific_field_when_in_memory_protocol_diverges(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    save(protocol, path)

    # A protocol object mutated after declaration (bypassing declare()) no
    # longer matches its own saved file, and no longer has a valid hash either.
    diverged = replace(protocol, min_events=5)
    result = verify(diverged, path)
    assert result.valid is False
    assert "min_events" in result.mismatched_fields or "content_hash" in result.mismatched_fields \
        or "modified after declaration" in result.message


def test_verify_missing_file():
    protocol = make_protocol()
    result = verify(protocol, "/nonexistent/path/protocol.json")
    assert result.valid is False
    assert "No saved protocol" in result.message


def test_verify_corrupt_json(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    path.write_text("{not valid json")
    result = verify(protocol, path)
    assert result.valid is False
    assert "not valid JSON" in result.message


# ---------------------------------------------------------------------------
# Correctness bar: a bare Protocol(...), never declared, cannot be "met".
# ---------------------------------------------------------------------------

def test_never_declared_protocol_is_invalid():
    raw = Protocol(
        hypothesis="Never declared.",
        primary_metric="brier_improvement",
        secondary_metrics=(),
        min_events=1,
        decision_threshold=0.0,
        stopping_rule="Stop at 1 event.",
        run_kind="replay",
        declared_at=DECLARED_AT,
        declared_by="nikhi",
        # content_hash omitted -> defaults to "" -> can never validate
    )
    assert not is_valid(raw)
    verdict = evaluate(raw, Results(n_events=1000, primary_metric_value=999.0))
    assert verdict.state == VerdictState.INVALID_PROTOCOL


def test_hash_stable_regardless_of_kwarg_order():
    a = declare(
        hypothesis="Same hypothesis.", primary_metric="brier_improvement",
        secondary_metrics=["pnl", "fill_rate"], min_events=10,
        decision_threshold=0.02, stopping_rule="Stop at 10 events.",
        run_kind="replay", declared_by="nikhi", declared_at=DECLARED_AT,
    )
    b = declare(
        declared_by="nikhi", run_kind="replay", decision_threshold=0.02,
        min_events=10, secondary_metrics=["pnl", "fill_rate"],
        primary_metric="brier_improvement", hypothesis="Same hypothesis.",
        stopping_rule="Stop at 10 events.", declared_at=DECLARED_AT,
    )
    assert a.content_hash == b.content_hash
    assert a == b


def test_hash_computation_insensitive_to_underlying_dict_key_order():
    from src.paper.protocol import _content_hash

    fields_a = {"hypothesis": "x", "min_events": 5, "decision_threshold": 0.1}
    fields_b = {"decision_threshold": 0.1, "hypothesis": "x", "min_events": 5}
    assert list(fields_a.keys()) != list(fields_b.keys())
    assert _content_hash(fields_a) == _content_hash(fields_b)


# ---------------------------------------------------------------------------
# Correctness bar: insufficient_data must never collapse into not_met.
# ---------------------------------------------------------------------------

def test_spectacular_but_underpowered_result_is_insufficient_data_not_not_met():
    protocol = make_protocol(min_events=30, decision_threshold=0.01)
    # An enormous, obviously "passing" effect size, but far too few events.
    results = Results(n_events=2, primary_metric_value=0.9)
    verdict = evaluate(protocol, results)
    assert verdict.state == VerdictState.INSUFFICIENT_DATA
    assert verdict.state != VerdictState.NOT_MET
    assert verdict.state != VerdictState.MET


def test_sufficient_events_and_effect_below_threshold_is_not_met():
    protocol = make_protocol(min_events=30, decision_threshold=0.05)
    results = Results(n_events=40, primary_metric_value=0.01)
    verdict = evaluate(protocol, results)
    assert verdict.state == VerdictState.NOT_MET


def test_sufficient_events_and_effect_at_or_above_threshold_is_met():
    protocol = make_protocol(min_events=30, decision_threshold=0.05)
    results = Results(n_events=30, primary_metric_value=0.05)
    verdict = evaluate(protocol, results)
    assert verdict.state == VerdictState.MET
    assert verdict.n_events == 30
    assert verdict.protocol_hash == protocol.content_hash
    assert "does not establish" in verdict.explanation or "not statistical proof" in verdict.explanation


def test_secondary_metrics_never_change_verdict_state():
    protocol = make_protocol(min_events=5, decision_threshold=0.05)
    # A spectacular secondary metric must not turn a failing primary metric into a pass.
    results = Results(n_events=10, primary_metric_value=0.0, secondary_metric_values={"pnl": 1_000_000.0})
    verdict = evaluate(protocol, results)
    assert verdict.state == VerdictState.NOT_MET
    assert verdict.secondary_metric_values == {"pnl": 1_000_000.0}
    assert "non-confirmatory" in verdict.explanation


# ---------------------------------------------------------------------------
# Correctness bar: declaring two primary metrics must raise.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_primary", [
    ["brier_improvement", "pnl"],
    ("brier_improvement", "pnl"),
    {"brier_improvement", "pnl"},
])
def test_multiple_primary_metrics_rejected(bad_primary):
    with pytest.raises(TypeError, match="exactly one"):
        make_protocol(primary_metric=bad_primary)


def test_primary_metric_duplicated_in_secondary_metrics_rejected():
    with pytest.raises(ValueError, match="secondary"):
        make_protocol(primary_metric="brier_improvement", secondary_metrics=["brier_improvement", "pnl"])


def test_duplicate_secondary_metrics_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        make_protocol(secondary_metrics=["pnl", "pnl"])


# ---------------------------------------------------------------------------
# Correctness bar: empty/whitespace hypothesis must raise.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_hypothesis", ["", "   ", "\t\n", None, 123])
def test_empty_or_invalid_hypothesis_rejected(bad_hypothesis):
    with pytest.raises((ValueError, TypeError)):
        make_protocol(hypothesis=bad_hypothesis)


# ---------------------------------------------------------------------------
# Other field validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_min_events", [0, -1, 1.5, True, "30"])
def test_invalid_min_events_rejected(bad_min_events):
    with pytest.raises(ValueError):
        make_protocol(min_events=bad_min_events)


@pytest.mark.parametrize("bad_threshold", [float("nan"), float("inf"), float("-inf"), True, "0.01"])
def test_invalid_decision_threshold_rejected(bad_threshold):
    with pytest.raises(ValueError):
        make_protocol(decision_threshold=bad_threshold)


def test_invalid_run_kind_rejected():
    with pytest.raises(ValueError, match="run_kind"):
        make_protocol(run_kind="production")


def test_stopping_rule_forbids_optional_stopping_language():
    with pytest.raises(ValueError, match="optional stopping"):
        make_protocol(stopping_rule="Stop collecting once the results look good.")


def test_stopping_rule_requires_nonempty_text():
    with pytest.raises(ValueError):
        make_protocol(stopping_rule="   ")


def test_declared_at_requires_timezone():
    with pytest.raises(ValueError, match="timezone-aware"):
        make_protocol(declared_at="2026-09-04T12:00:00")


def test_declared_by_requires_nonempty_text():
    with pytest.raises(ValueError):
        make_protocol(declared_by="")


# ---------------------------------------------------------------------------
# save/load round trip
# ---------------------------------------------------------------------------

def test_save_load_round_trip_preserves_equality(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "nested" / "protocol.json"
    save(protocol, path)
    loaded = load(path)
    assert loaded == protocol
    assert is_valid(loaded)
    assert verify(loaded, path).valid


def test_load_rejects_file_with_invalid_field(tmp_path):
    protocol = make_protocol()
    path = tmp_path / "protocol.json"
    save(protocol, path)
    raw = json.loads(path.read_text())
    raw["hypothesis"] = "   "
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load(path)


def test_results_rejects_non_finite_primary_metric():
    with pytest.raises(ValueError):
        Results(n_events=10, primary_metric_value=float("nan"))


def test_results_rejects_negative_n_events():
    with pytest.raises(ValueError):
        Results(n_events=-1, primary_metric_value=0.1)
