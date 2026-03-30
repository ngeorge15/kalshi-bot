"""Custom exceptions for the prediction model and validation system.

All guard violations are raised as GuardViolation and must be caught by
the caller.  A --force bypass logs the reason string to the SQLite
improvements table before proceeding (D-07).
"""


class GuardViolation(Exception):
    """Raised when an overfitting guard rejects a deployment or parameter change.

    Per D-07: guards are hard blocks. Bypass requires --force with a mandatory
    reason string that is logged to the improvements SQLite table.

    Args:
        guard_name: Name of the guard that triggered (e.g. 'sample_size_gate').
        message: Human-readable description of the violation.
        details: Optional dict of numeric context (e.g. {'n_samples': 23, 'required': 50}).
    """

    def __init__(self, guard_name: str, message: str, details: dict | None = None) -> None:
        self.guard_name = guard_name
        self.details = details or {}
        super().__init__(f"[{guard_name}] {message}")


class InsufficientDataError(Exception):
    """Raised when there is not enough historical data to train or predict.

    Args:
        message: Description of what data is missing or too sparse.
        n_available: How many records are available.
        n_required: How many are needed.
    """

    def __init__(self, message: str, n_available: int = 0, n_required: int = 0) -> None:
        self.n_available = n_available
        self.n_required = n_required
        super().__init__(message)


class ModelNotFoundError(Exception):
    """Raised when model_store cannot find a trained model for the given name/version.

    Args:
        model_name: The model identifier (e.g. 'nba_game').
        version: Optional version number; None means 'latest'.
    """

    def __init__(self, model_name: str, version: int | None = None) -> None:
        self.model_name = model_name
        self.version = version
        ver_str = f" v{version}" if version is not None else " (latest)"
        super().__init__(f"No trained model found for '{model_name}'{ver_str}. Run: python -m src.models.pipeline --train --model {model_name}")


class BootstrapModeError(Exception):
    """Raised when code path tries to enforce full guards in bootstrap mode.

    Bootstrap mode: < 50 out-of-sample predictions exist (D-08).
    During bootstrap, guards log status but do not block trading.
    """
