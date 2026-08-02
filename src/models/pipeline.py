"""Training pipeline CLI entry point (D-01, D-02, D-03).

Training is CLI-only — triggered manually. predict() never triggers training.
Individual model selection supported via --model (D-03).
Guards run before deploying any model. --force bypasses with a logged reason (D-07).

Usage:
    python -m src.models.pipeline --train --model nba_game
    python -m src.models.pipeline --train --model all
    python -m src.models.pipeline --train --model weather_temp --force "Initial deployment, < 50 OOS predictions"
    python -m src.models.pipeline --status
"""

import argparse
import logging
import sys
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline")

VALID_MODELS = ("nba_game", "nba_totals", "nba_props", "weather_temp", "weather_precip", "all")


def run_train(model_name: str, force_reason: Optional[str] = None) -> None:
    """Train one or all models, running overfitting guards before deployment.

    Args:
        model_name: One of VALID_MODELS.
        force_reason: If provided, bypasses GuardViolation with this reason string.
    """
    from src.db.database import Database
    from src.validation.guards import OverfittingGuards
    from src.models.exceptions import GuardViolation

    db = Database()
    guards = OverfittingGuards(db=db)

    models_to_train = list(VALID_MODELS[:-1]) if model_name == "all" else [model_name]

    for name in models_to_train:
        logger.info("--- Training %s ---", name)
        try:
            _train_single(name, guards, db)
        except GuardViolation as exc:
            if force_reason:
                logger.warning("GuardViolation raised during %s: %s. --force active, bypassing.", name, exc)
                guards.force_override(exc.guard_name, force_reason, name)
                _train_single(name, guards=None, db=db)
            else:
                logger.error("GuardViolation blocked deployment of %s: %s", name, exc)
                logger.error("Use --force REASON to bypass (logged for accountability).")
                sys.exit(1)
        except Exception as exc:
            logger.error("Training failed for %s: %s", name, exc, exc_info=True)
            sys.exit(1)


def _train_single(model_name: str, guards, db) -> None:
    """Load a model, call its train()/fit method, then run the sample size guard."""
    if model_name == "nba_game":
        from src.models.nba_game import NBAGameModel
        result = NBAGameModel().train()
    elif model_name == "nba_totals":
        from src.models.nba_totals import NBATotalsModel
        result = NBATotalsModel().train()
    elif model_name == "nba_props":
        from src.models.nba_props import NBAPropsModel
        model = NBAPropsModel()
        result = {prop_type: model.train(prop_type=prop_type) for prop_type in ("pts", "reb", "ast", "3pm")}
    elif model_name == "weather_temp":
        from src.models.weather_temp import WeatherTempModel
        result = WeatherTempModel().fit_bias_correction()
        logger.info("Weather temp bias correction fitted: %s", list(result.keys()))
        return
    elif model_name == "weather_precip":
        from src.models.weather_precip import WeatherPrecipModel
        WeatherPrecipModel()
        logger.info("WeatherPrecipModel loaded. Historical rates are computed on first predict().")
        return
    else:
        logger.error("Unknown model: %s. Valid: %s", model_name, VALID_MODELS)
        sys.exit(1)

    logger.info("Training complete for %s: %s", model_name, result)

    if guards is not None:
        n_oos_row = db.fetchone(
            "SELECT COUNT(*) AS n FROM predictions WHERE model_name = ?",
            (model_name,),
        )
        n_oos_count = n_oos_row["n"] if n_oos_row else 0
        if n_oos_count < 50:
            logger.warning(
                "Bootstrap mode: only %d OOS predictions for %s (< 50). "
                "Model trained and saved. Full guard enforcement after 50 OOS predictions (D-08).",
                n_oos_count, model_name,
            )
        else:
            guards.check_sample_size(n_oos_count)


def run_status() -> None:
    """Print current model versions and bootstrap status for all models."""
    from src.models.model_store import ModelStore
    from src.models.exceptions import ModelNotFoundError

    print(f"{'Model':<25} {'Version':<10} {'Bootstrap':<12}")
    print("-" * 47)
    for name in VALID_MODELS[:-1]:
        store = ModelStore(name)
        try:
            version = store.get_active_version()
            version_str = str(version)
        except ModelNotFoundError:
            version_str = "untrained"
        bootstrap = store.is_bootstrap_mode()
        status = "YES (<50 OOS)" if bootstrap else "NO"
        print(f"{name:<25} {version_str:<10} {status:<12}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.models.pipeline",
        description="Kalshi bot training pipeline (D-01: CLI-only training)",
    )
    parser.add_argument("--train", action="store_true", help="Train a model")
    parser.add_argument("--status", action="store_true", help="Show model versions and bootstrap status")
    parser.add_argument(
        "--model",
        choices=VALID_MODELS,
        default="all",
        help="Which model to train (default: all)",
    )
    parser.add_argument(
        "--force",
        metavar="REASON",
        help="Bypass GuardViolation with a mandatory reason string (logged to SQLite)",
    )

    args = parser.parse_args()

    if args.train:
        run_train(args.model, force_reason=args.force)
    elif args.status:
        run_status()
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
