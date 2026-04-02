"""Model versioning, persistence, and rollback using joblib.

Stores trained scikit-learn models (plus optional calibrators) as versioned
joblib files in a directory.  Metadata (version, metrics, data hash, timestamps)
lives in a ``versions.json`` sidecar file.

Usage::

    from src.models.model_store import ModelStore
    store = ModelStore("nba_game")
    version = store.save(model, calibrator, metrics={"brier": 0.21}, training_data_hash="abc")
    model, calibrator = store.load_latest()
    store.rollback()
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import joblib

from src.models.exceptions import ModelNotFoundError

logger = logging.getLogger(__name__)


class ModelStore:
    """Versioned model persistence backed by joblib + JSON metadata.

    Directory layout for model_name ``"nba_game"`` under ``store_dir``::

        data/models/nba_game/
            versions.json          # [{version, metrics, data_hash, created_at, active}]
            nba_game_v1.joblib     # Trained model (version 1)
            nba_game_v1_cal.joblib # Optional calibrator (version 1)
            nba_game_v2.joblib     # Version 2
            ...

    Args:
        model_name: Identifier for this model (e.g. ``"nba_game"``).
        store_dir: Root directory for all model storage.  Defaults to
            ``"data/models"``.
    """

    def __init__(self, model_name: str, store_dir: str = "data/models") -> None:
        self.model_name = model_name
        self.model_dir = Path(store_dir) / model_name
        self._versions_path = self.model_dir / "versions.json"
        self._versions: list[dict] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        """Create the model directory if it doesn't exist."""
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def _load_versions(self) -> list[dict]:
        """Load versions metadata from disk, caching in memory."""
        if self._versions is not None:
            return self._versions
        if self._versions_path.exists():
            with open(self._versions_path) as f:
                self._versions = json.load(f)
        else:
            self._versions = []
        return self._versions

    def _save_versions(self) -> None:
        """Persist versions metadata to disk."""
        self._ensure_dir()
        with open(self._versions_path, "w") as f:
            json.dump(self._versions or [], f, indent=2)

    def _model_path(self, version: int) -> Path:
        return self.model_dir / f"{self.model_name}_v{version}.joblib"

    def _calibrator_path(self, version: int) -> Path:
        return self.model_dir / f"{self.model_name}_v{version}_cal.joblib"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        model: Any,
        calibrator: Any = None,
        metrics: dict | None = None,
        training_data_hash: str = "",
    ) -> int:
        """Save a trained model (and optional calibrator) as a new version.

        The new version becomes the active version.  Any previously active
        version is marked inactive.

        Args:
            model: A fitted scikit-learn estimator (or pipeline).
            calibrator: Optional fitted calibrator (e.g. CalibratedClassifierCV
                stored separately for inspection).  ``None`` if calibration is
                baked into *model*.
            metrics: Dict of evaluation metrics (e.g. ``{"brier": 0.21}``).
            training_data_hash: Short hash of the training data for
                reproducibility tracking.

        Returns:
            The new version number (1-indexed).
        """
        versions = self._load_versions()

        # Deactivate all existing versions
        for v in versions:
            v["active"] = False

        new_version = len(versions) + 1
        self._ensure_dir()

        # Persist artifacts
        joblib.dump(model, self._model_path(new_version))
        if calibrator is not None:
            joblib.dump(calibrator, self._calibrator_path(new_version))

        versions.append(
            {
                "version": new_version,
                "metrics": metrics or {},
                "training_data_hash": training_data_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "active": True,
            }
        )
        self._versions = versions
        self._save_versions()
        logger.info(
            "Saved %s v%d (metrics=%s, hash=%s)",
            self.model_name,
            new_version,
            metrics,
            training_data_hash,
        )
        return new_version

    def load_latest(self) -> tuple[Any, Any]:
        """Load the active model version from disk.

        Returns:
            ``(model, calibrator)`` tuple.  ``calibrator`` may be ``None``
            if none was saved.

        Raises:
            ModelNotFoundError: If no versions exist or the active version's
                file is missing.
        """
        versions = self._load_versions()
        active = [v for v in versions if v.get("active")]
        if not active:
            raise ModelNotFoundError(self.model_name)

        version_num = active[-1]["version"]
        model_path = self._model_path(version_num)
        if not model_path.exists():
            raise ModelNotFoundError(self.model_name, version=version_num)

        model = joblib.load(model_path)
        cal_path = self._calibrator_path(version_num)
        calibrator = joblib.load(cal_path) if cal_path.exists() else None

        logger.debug("Loaded %s v%d", self.model_name, version_num)
        return model, calibrator

    def get_active_version(self) -> int:
        """Return the active version number.

        Returns:
            Version number (1-indexed).

        Raises:
            ModelNotFoundError: If no active version exists.
        """
        versions = self._load_versions()
        active = [v for v in versions if v.get("active")]
        if not active:
            raise ModelNotFoundError(self.model_name)
        return active[-1]["version"]

    def rollback(self) -> int:
        """Roll back to the previous version.

        Deactivates the current active version and reactivates the one before it.

        Returns:
            The newly active version number.

        Raises:
            ModelNotFoundError: If fewer than 2 versions exist.
        """
        versions = self._load_versions()
        if len(versions) < 2:
            raise ModelNotFoundError(
                self.model_name,
                version=None,
            )

        # Find current active, deactivate it
        for v in versions:
            if v.get("active"):
                v["active"] = False

        # Activate the second-to-last version
        versions[-2]["active"] = True
        self._versions = versions
        self._save_versions()

        new_active = versions[-2]["version"]
        logger.info("Rolled back %s to v%d", self.model_name, new_active)
        return new_active

    def is_bootstrap_mode(self) -> bool:
        """Check if the model is in bootstrap mode (< 50 OOS predictions).

        Bootstrap mode (D-08/D-09): guards log warnings but do not block.
        Currently returns True if no active version exists (no predictions
        have been made at all).

        Returns:
            True if fewer than 50 out-of-sample predictions are recorded.
        """
        versions = self._load_versions()
        active = [v for v in versions if v.get("active")]
        if not active:
            return True
        # Check metrics for OOS count if tracked
        latest_metrics = active[-1].get("metrics", {})
        oos_count = latest_metrics.get("n_oos_predictions", 0)
        return oos_count < 50
