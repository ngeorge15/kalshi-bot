"""Overfitting guard system — hard blocks on model deployment and parameter changes.

Guards enforce the overfitting protection philosophy from PROJECT.md.
Each guard raises GuardViolation on failure.  Bypass requires --force with a
mandatory reason string logged to the improvements table (D-07).

Usage::

    from src.validation.guards import OverfittingGuards
    guards = OverfittingGuards()
    guards.check_sample_size(n_oos=23)      # raises GuardViolation
    guards.check_dampening(1.0, 1.25)       # raises GuardViolation (25% change)
    guards.check_significance(p_value=0.12) # raises GuardViolation
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from scipy import stats

from src.models.exceptions import GuardViolation

logger = logging.getLogger(__name__)

# Guard thresholds (matching PROJECT.md)
MIN_OOS_PREDICTIONS = 50       # R6.3, D-09
MAX_PARAM_CHANGE_PCT = 0.20    # R6.4
COOLDOWN_TRADES = 30           # R6.5
STALENESS_DAYS = 30            # R6.6
SIGNIFICANCE_THRESHOLD = 0.05  # R6.7


class OverfittingGuards:
    """Collection of overfitting protection guards.

    Each ``check_*`` method validates a specific condition and raises
    :class:`~src.models.exceptions.GuardViolation` if the condition is
    not met.  All guards are independent — call whichever subset applies
    to your workflow.

    Args:
        db: Optional Database instance for guards that query SQLite
            (e.g. cooldown checks the improvements table).
    """

    def __init__(self, db=None) -> None:
        self.db = db

    def check_sample_size(self, n_oos: int) -> None:
        """Ensure at least MIN_OOS_PREDICTIONS out-of-sample predictions.

        Args:
            n_oos: Number of out-of-sample predictions available.

        Raises:
            GuardViolation: If ``n_oos < 50``.
        """
        if n_oos < MIN_OOS_PREDICTIONS:
            raise GuardViolation(
                guard_name="sample_size_gate",
                message=(
                    f"Need at least {MIN_OOS_PREDICTIONS} OOS predictions "
                    f"before deploying; got {n_oos}"
                ),
                details={"n_oos": n_oos, "required": MIN_OOS_PREDICTIONS},
            )
        logger.debug("sample_size_gate: PASS (n_oos=%d)", n_oos)

    def check_dampening(
        self, current_value: float, proposed_value: float,
    ) -> None:
        """Block parameter changes exceeding MAX_PARAM_CHANGE_PCT (20%).

        Args:
            current_value: Current parameter value.
            proposed_value: Proposed new value.

        Raises:
            GuardViolation: If the absolute change exceeds 20%.
        """
        if current_value == 0:
            pct_change = abs(proposed_value)
        else:
            pct_change = abs(proposed_value - current_value) / abs(current_value)

        if pct_change > MAX_PARAM_CHANGE_PCT:
            raise GuardViolation(
                guard_name="dampening",
                message=(
                    f"Parameter change {pct_change:.1%} exceeds "
                    f"max {MAX_PARAM_CHANGE_PCT:.0%}"
                ),
                details={
                    "current": current_value,
                    "proposed": proposed_value,
                    "pct_change": pct_change,
                    "max_pct": MAX_PARAM_CHANGE_PCT,
                },
            )
        logger.debug(
            "dampening: PASS (%.4f -> %.4f, change=%.1%%)",
            current_value, proposed_value, pct_change * 100,
        )

    def check_cooldown(self) -> None:
        """Block improvements if < COOLDOWN_TRADES settled since last change.

        Queries the ``improvements`` table for the most recent applied
        improvement and checks ``post_apply_trades``.

        Raises:
            GuardViolation: If cooldown period has not elapsed.
        """
        if self.db is None:
            logger.warning("cooldown: SKIP (no database provided)")
            return

        row = self.db.fetchone(
            "SELECT post_apply_trades FROM improvements "
            "WHERE status = 'applied' "
            "ORDER BY applied_at DESC, created_at DESC LIMIT 1"
        )

        if row is None:
            # No prior improvements — cooldown doesn't apply
            logger.debug("cooldown: PASS (no prior improvements)")
            return

        post_trades = row.get("post_apply_trades", 0)
        if post_trades < COOLDOWN_TRADES:
            raise GuardViolation(
                guard_name="cooldown",
                message=(
                    f"Only {post_trades} trades since last improvement "
                    f"(need {COOLDOWN_TRADES})"
                ),
                details={
                    "post_apply_trades": post_trades,
                    "required": COOLDOWN_TRADES,
                },
            )
        logger.debug("cooldown: PASS (post_apply_trades=%d)", post_trades)

    def check_staleness(self, last_validated_at: str) -> None:
        """Flag parameters not re-validated in STALENESS_DAYS (30 days).

        Args:
            last_validated_at: ISO-8601 UTC timestamp of last validation.

        Raises:
            GuardViolation: If more than 30 days have passed.
        """
        try:
            last_dt = datetime.fromisoformat(last_validated_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise GuardViolation(
                guard_name="staleness",
                message=f"Invalid timestamp: {last_validated_at}",
            )

        now = datetime.now(timezone.utc)
        days_since = (now - last_dt).days

        if days_since > STALENESS_DAYS:
            raise GuardViolation(
                guard_name="staleness",
                message=(
                    f"Parameters not validated in {days_since} days "
                    f"(max {STALENESS_DAYS})"
                ),
                details={
                    "days_since": days_since,
                    "max_days": STALENESS_DAYS,
                    "last_validated": last_validated_at,
                },
            )
        logger.debug("staleness: PASS (last validated %d days ago)", days_since)

    def check_significance(self, p_value: float) -> None:
        """Block deployment when edge is not statistically significant.

        Args:
            p_value: P-value from binomial test of positive edge.

        Raises:
            GuardViolation: If ``p_value >= 0.05``.
        """
        if p_value >= SIGNIFICANCE_THRESHOLD:
            raise GuardViolation(
                guard_name="significance",
                message=(
                    f"p-value {p_value:.4f} >= {SIGNIFICANCE_THRESHOLD} "
                    f"— edge not statistically significant"
                ),
                details={
                    "p_value": p_value,
                    "threshold": SIGNIFICANCE_THRESHOLD,
                },
            )
        logger.debug("significance: PASS (p=%.4f)", p_value)

    def check_regime(self, sample1, sample2) -> None:
        """Alert on distributional shift between two feature samples (R6.8).

        Runs a two-sample Kolmogorov-Smirnov test comparing a historical
        feature distribution against a recent one. A significant shift means
        the model is at risk of silently fitting to a new regime (e.g. NBA
        schedule density change, seasonal weather transition) rather than
        being explicitly retrained for it.

        Args:
            sample1: Historical feature sample (1D array-like).
            sample2: Recent feature sample (1D array-like).

        Raises:
            GuardViolation: If the KS test p-value < SIGNIFICANCE_THRESHOLD.
        """
        arr1 = np.asarray(sample1, dtype=float)
        arr2 = np.asarray(sample2, dtype=float)
        ks_stat, p_value = stats.ks_2samp(arr1, arr2)

        if p_value < SIGNIFICANCE_THRESHOLD:
            raise GuardViolation(
                guard_name="regime_change",
                message=(
                    f"Distributional shift detected: KS p-value {p_value:.4f} "
                    f"< {SIGNIFICANCE_THRESHOLD} (ks_stat={ks_stat:.4f})"
                ),
                details={
                    "ks_stat": float(ks_stat),
                    "p_value": float(p_value),
                    "sample1_size": len(arr1),
                    "sample2_size": len(arr2),
                },
            )
        logger.debug("regime_change: PASS (KS p=%.4f)", p_value)

    def force_override(self, guard_name: str, reason: str, model_name: str) -> None:
        """Bypass a guard violation with a mandatory reason, logged for accountability (D-07).

        Per D-07: guards are hard blocks, but a human (or the evaluator, per
        its own cooldown rules) can force past one by supplying a non-empty
        reason. The bypass is written to the ``improvements`` table so it
        shows up in the evaluator's audit trail rather than disappearing.

        Args:
            guard_name: Name of the guard being bypassed (e.g. 'sample_size_gate').
            reason: Human-provided justification. Must be non-empty.
            model_name: The model the override applies to.

        Raises:
            ValueError: If reason is empty or blank.
            RuntimeError: If no database was provided to this guard instance.
        """
        if not reason or not reason.strip():
            raise ValueError("force_override requires a non-empty reason (D-07)")
        if self.db is None:
            raise RuntimeError("Cannot log force_override without a database connection")

        self.db.execute(
            "INSERT INTO improvements "
            "(type, target, description, risk_level, auto_approvable, "
            "validated_on_n_samples, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "guard_override",
                model_name,
                f"[FORCE OVERRIDE] Guard '{guard_name}' bypassed. Reason: {reason}",
                "high",
                0,
                0,
                "applied",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        logger.warning(
            "FORCE OVERRIDE: guard '%s' bypassed for model '%s'. Reason: %s",
            guard_name, model_name, reason,
        )

    def run_all(
        self,
        n_oos: int,
        p_value: float,
        last_validated_at: str,
        current_value: Optional[float] = None,
        proposed_value: Optional[float] = None,
    ) -> list[str]:
        """Run all applicable guards and return list of violations.

        Does not raise — catches GuardViolation and collects messages.
        Useful for generating a report of all issues at once.

        Returns:
            List of violation message strings (empty if all pass).
        """
        violations = []

        for check, kwargs in [
            (self.check_sample_size, {"n_oos": n_oos}),
            (self.check_significance, {"p_value": p_value}),
            (self.check_staleness, {"last_validated_at": last_validated_at}),
            (self.check_cooldown, {}),
        ]:
            try:
                check(**kwargs)
            except GuardViolation as e:
                violations.append(str(e))

        if current_value is not None and proposed_value is not None:
            try:
                self.check_dampening(current_value, proposed_value)
            except GuardViolation as e:
                violations.append(str(e))

        return violations
