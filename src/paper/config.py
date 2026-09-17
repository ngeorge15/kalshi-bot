"""Paper-only settings, independent of credentials and live trading config."""
from dataclasses import asdict, dataclass
import math

from src.paper.fees import KNOWN_FEE_TYPES


@dataclass(frozen=True)
class PaperConfig:
    """Persisted experiment settings; costs are assumptions, not an exchange tariff."""

    initial_cash_cents: int = 100_000
    fee_per_contract_cents: int = 2
    # "flat" (default) charges fee_per_contract_cents per contract, matching every
    # experiment run before this field existed. "kalshi" instead charges Kalshi's
    # real price-dependent quadratic formula (src/paper/fees.py) using fee_type
    # and fee_multiplier below, both read from the series' public API record.
    fee_model: str = "flat"
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    slippage_cents: int = 1
    max_quote_age_seconds: int = 60
    max_forecast_age_seconds: int = 21_600
    weather_sigma_f: float = 5.0
    max_exposure_cents: int = 25_000
    max_daily_loss_cents: int = 5_000  # positive loss magnitude
    max_positions: int = 10
    max_event_positions: int = 2
    max_contracts_per_order: int = 25
    kelly_fraction: float = 0.25
    min_edge: float = 0.05  # after assumed costs
    # Research mode records model probability, market probability and outcome
    # without ever sizing or placing a paper order. It exists because proving
    # predictive edge needs those three values, and none of them require the
    # market to be tradeable by the operator -- reading a public book is not
    # trading. Eligibility is still recorded, just not used to skip collection.
    research_only: bool = False
    run_kind: str = "replay"
    allowed_market_types: tuple[str, ...] = ("temperature", "precipitation")

    def __post_init__(self) -> None:
        for name in ("initial_cash_cents", "max_quote_age_seconds", "max_forecast_age_seconds", "max_exposure_cents",
                     "max_daily_loss_cents", "max_positions", "max_event_positions",
                     "max_contracts_per_order"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("fee_per_contract_cents", "slippage_cents"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 99:
                raise ValueError(f"{name} must be an integer in [0, 99]")
        for name in ("kelly_fraction", "min_edge"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if isinstance(self.weather_sigma_f, bool) or not math.isfinite(self.weather_sigma_f) or self.weather_sigma_f <= 0:
            raise ValueError("weather_sigma_f must be finite and positive")
        if self.fee_model not in {"flat", "kalshi"}:
            raise ValueError("fee_model must be flat or kalshi")
        if self.fee_type not in KNOWN_FEE_TYPES:
            raise ValueError(f"fee_type must be one of {sorted(KNOWN_FEE_TYPES)}")
        if (isinstance(self.fee_multiplier, bool) or not isinstance(self.fee_multiplier, (int, float))
                or not math.isfinite(self.fee_multiplier) or self.fee_multiplier <= 0):
            raise ValueError("fee_multiplier must be finite and positive")
        if type(self.research_only) is not bool:
            raise ValueError("research_only must be a bool")
        if self.run_kind not in {"synthetic", "replay", "forward"}:
            raise ValueError("run_kind must be synthetic, replay, or forward")
        if not self.allowed_market_types or any(
            not isinstance(item, str) or not item for item in self.allowed_market_types
        ):
            raise ValueError("allowed_market_types must contain nonempty names")

        if self.run_kind == "forward" and not set(self.allowed_market_types) <= {"temperature", "precipitation"}:
            raise ValueError("Forward mode currently supports weather only; use replay for other research")

    def to_dict(self) -> dict:
        """Return JSON-compatible settings."""
        return asdict(self)
