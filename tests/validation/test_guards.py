"""Tests for src/validation/guards.py — Overfitting guard system."""
import pytest


def test_sample_size_gate_blocks_below_50():
    """check_sample_size() raises GuardViolation when n_oos < 50 (R6.3, D-09)."""
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    guards = OverfittingGuards()
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_sample_size(n_oos=23)
    assert exc_info.value.guard_name == "sample_size_gate"


def test_sample_size_gate_passes_at_50():
    """check_sample_size() does not raise when n_oos == 50."""
    from src.validation.guards import OverfittingGuards

    guards = OverfittingGuards()
    guards.check_sample_size(n_oos=50)  # Must NOT raise


def test_dampening_blocks_large_parameter_change():
    """check_dampening() raises GuardViolation when change > 20% (R6.4, D-09)."""
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    guards = OverfittingGuards()
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_dampening(current_value=1.0, proposed_value=1.25)  # 25% change
    assert exc_info.value.guard_name == "dampening"


def test_dampening_passes_within_20_pct():
    """check_dampening() passes when change <= 20%."""
    from src.validation.guards import OverfittingGuards

    guards = OverfittingGuards()
    guards.check_dampening(current_value=1.0, proposed_value=1.19)  # 19% change — OK


def test_cooldown_blocks_during_cooldown_period(tmp_path):
    """check_cooldown() raises GuardViolation when < 30 settled trades since last change (R6.5)."""
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation
    from src.db.database import Database

    db = Database(db_path=str(tmp_path / "test.db"))
    guards = OverfittingGuards(db=db)
    # Simulate a recent improvement with only 10 post-apply trades
    db.execute(
        "INSERT INTO improvements (type, target, description, risk_level, "
        "auto_approvable, validated_on_n_samples, status, post_apply_trades, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("config_tweak", "kelly_fraction", "test", "low", 1, 60, "applied", 10,
         "2026-03-29T00:00:00Z"),
    )
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_cooldown()
    assert exc_info.value.guard_name == "cooldown"


def test_staleness_flags_old_parameters():
    """check_staleness() raises GuardViolation when model not re-validated in 30+ days (R6.6)."""
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    guards = OverfittingGuards()
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_staleness(last_validated_at="2025-01-01T00:00:00Z")
    assert exc_info.value.guard_name == "staleness"


def test_significance_gate_blocks_high_p_value():
    """check_significance() raises GuardViolation when p_value >= 0.05 (R6.7, D-09)."""
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    guards = OverfittingGuards()
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_significance(p_value=0.12)
    assert exc_info.value.guard_name == "significance"


def test_regime_check_passes_on_same_distribution():
    """check_regime() does not raise when both samples come from the same distribution (R6.8)."""
    import numpy as np
    from src.validation.guards import OverfittingGuards

    rng = np.random.RandomState(42)
    guards = OverfittingGuards()
    guards.check_regime(rng.normal(0, 1, 100), rng.normal(0, 1, 100))


def test_regime_check_blocks_distributional_shift():
    """check_regime() raises GuardViolation when KS test detects a significant shift (R6.8)."""
    import numpy as np
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    rng = np.random.RandomState(42)
    guards = OverfittingGuards()
    with pytest.raises(GuardViolation) as exc_info:
        guards.check_regime(rng.normal(0, 1, 100), rng.normal(5, 1, 100))
    assert exc_info.value.guard_name == "regime_change"


def test_force_override_requires_non_empty_reason():
    """force_override() rejects an empty reason string (D-07)."""
    from src.validation.guards import OverfittingGuards
    from src.db.database import Database

    guards = OverfittingGuards(db=Database(db_path=":memory:"))
    with pytest.raises(ValueError):
        guards.force_override("sample_size_gate", "", "nba_game")


def test_force_override_logs_to_improvements_table(tmp_path):
    """force_override() writes a 'guard_override' row to the improvements table (D-07)."""
    from src.validation.guards import OverfittingGuards
    from src.db.database import Database

    db = Database(db_path=str(tmp_path / "test.db"))
    guards = OverfittingGuards(db=db)
    guards.force_override("sample_size_gate", "Initial deployment, bootstrap mode confirmed", "nba_game")

    rows = db.fetchall("SELECT * FROM improvements WHERE type = 'guard_override'")
    assert len(rows) == 1
    assert "sample_size_gate" in rows[0]["description"]
    assert rows[0]["risk_level"] == "high"
    assert rows[0]["status"] == "applied"
