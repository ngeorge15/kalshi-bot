"""Kalshi's quadratic fee formula: known points, rounding boundaries, validation."""
import pytest

from src.paper.fees import KNOWN_FEE_TYPES, trading_fee_cents


def test_known_points_match_kalshis_published_examples():
    # $1.75 fee on 100 contracts at 50c is the widely-cited worked example
    # for the taker formula: 0.07 * 100 * 0.5 * 0.5 = 1.75 dollars exactly.
    assert trading_fee_cents(50, 100, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 175
    # Single contract at 50c: 0.07*0.5*0.5 = 0.0175 dollars = 1.75 cents, rounds up to 2.
    assert trading_fee_cents(50, 1, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 2


def test_extreme_prices_round_up_to_one_cent():
    # 0.07*1*99 = 6.93 cents at price=1 (or symmetrically price=99); rounds up to 1 cent.
    assert trading_fee_cents(1, 1, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 1
    assert trading_fee_cents(99, 1, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 1
    assert (trading_fee_cents(1, 100, fee_type="quadratic", fee_multiplier=1, is_maker=False)
            == trading_fee_cents(99, 100, fee_type="quadratic", fee_multiplier=1, is_maker=False))


def test_rounding_boundary_exact_cent_does_not_round_up_further():
    # 100 contracts at 50c gives an exact 175 (no fractional remainder to ceil away).
    assert trading_fee_cents(50, 100, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 175
    # 4 contracts at 50c: 0.07*4*0.25 = 0.07 dollars = 7 cents exactly.
    assert trading_fee_cents(50, 4, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 7
    # 5 contracts at 50c: 0.07*5*0.25 = 0.0875 dollars = 8.75 cents, rounds up to 9.
    assert trading_fee_cents(50, 5, fee_type="quadratic", fee_multiplier=1, is_maker=False) == 9


def test_quadratic_series_ignores_maker_flag():
    taker = trading_fee_cents(50, 10, fee_type="quadratic", fee_multiplier=1, is_maker=False)
    maker = trading_fee_cents(50, 10, fee_type="quadratic", fee_multiplier=1, is_maker=True)
    assert taker == maker == 18  # 0.07*10*0.25 = 0.175 dollars = 17.5 cents -> 18


def test_maker_fee_types_discount_to_one_quarter_of_taker():
    taker = trading_fee_cents(50, 100, fee_type="quadratic_with_maker_fees", fee_multiplier=1, is_maker=False)
    maker = trading_fee_cents(50, 100, fee_type="quadratic_with_maker_fees", fee_multiplier=1, is_maker=True)
    assert taker == 175
    # 0.0175*100*0.25 = 0.4375 dollars = 43.75 cents -> rounds up to 44.
    assert maker == 44
    assert trading_fee_cents(50, 100, fee_type="quadratic_with_combo_maker_fees",
                              fee_multiplier=1, is_maker=True) == maker


def test_fee_multiplier_scales_linearly():
    base = trading_fee_cents(50, 100, fee_type="quadratic", fee_multiplier=1, is_maker=False)
    doubled = trading_fee_cents(50, 100, fee_type="quadratic", fee_multiplier=2, is_maker=False)
    assert doubled == base * 2 == 350


@pytest.mark.parametrize("price_cents", [0, 100, -1, 50.0])
def test_rejects_invalid_price(price_cents):
    with pytest.raises(ValueError):
        trading_fee_cents(price_cents, 1, fee_type="quadratic", fee_multiplier=1, is_maker=False)


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_rejects_invalid_count(count):
    with pytest.raises(ValueError):
        trading_fee_cents(50, count, fee_type="quadratic", fee_multiplier=1, is_maker=False)


def test_rejects_unknown_fee_type():
    with pytest.raises(ValueError, match="Unsupported fee_type"):
        trading_fee_cents(50, 1, fee_type="flat", fee_multiplier=1, is_maker=False)
    with pytest.raises(ValueError):
        trading_fee_cents(50, 1, fee_type="bogus", fee_multiplier=1, is_maker=False)


@pytest.mark.parametrize("multiplier", [0, -1, float("nan"), float("inf"), True])
def test_rejects_invalid_fee_multiplier(multiplier):
    with pytest.raises(ValueError):
        trading_fee_cents(50, 1, fee_type="quadratic", fee_multiplier=multiplier, is_maker=False)


def test_known_fee_types_is_the_validated_set():
    assert KNOWN_FEE_TYPES == {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}
