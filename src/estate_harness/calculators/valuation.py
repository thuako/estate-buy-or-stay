"""Conditional DCF and discrete pricing horizons; these functions do not forecast.

Every rent, rate, terminal value, and future path must be supplied. Rates are per
cash-flow period, rents are *net rent* (never a jeonse deposit), and terminal value
is measured at the end of the final period. No development premium is added.
"""

from decimal import Decimal, localcontext
from itertools import pairwise

from .common import decimal, integer, krw, ratio


def discounted_cash_flow(
    net_rents_krw: list,
    discount_rates: list,
    terminal_value_krw: int | str,
) -> dict:
    """Discount end-of-period net rents and terminal value using matched rates.

    The caller specifies the period convention in its scenario. Negative net
    rents are allowed; terminal value must be nonnegative and rates exceed -1.
    Terminal +/-10% sensitivity changes only terminal value, not forecasts.
    """
    if not net_rents_krw or len(net_rents_krw) != len(discount_rates):
        raise ValueError("net_rents_krw and discount_rates need equal, nonzero lengths")
    with localcontext() as context:
        context.prec = 40
        discount = Decimal(1)
        pv_rents = Decimal(0)
        for rent, rate in zip(net_rents_krw, discount_rates, strict=True):
            rate = decimal(rate, "discount_rate")
            if rate <= -1:
                raise ValueError("each discount_rate must be greater than -1")
            discount *= 1 + rate
            pv_rents += decimal(rent, "net_rent_krw") / discount
        pv_terminal = decimal(terminal_value_krw, "terminal_value_krw", minimum=0) / discount
        value = pv_rents + pv_terminal
        return {
            "fundamental_value_krw": krw(value),
            "discounted_rents_krw": krw(pv_rents),
            "discounted_terminal_value_krw": krw(pv_terminal),
            "terminal_value_share": ratio(pv_terminal / value) if value else None,
            "terminal_sensitivity_krw": {
                "minus_10_percent": krw(pv_rents + pv_terminal * Decimal("0.9")),
                "plus_10_percent": krw(pv_rents + pv_terminal * Decimal("1.1")),
            },
            "periods": len(net_rents_krw),
        }


def pricing_horizon(current_price_krw: int | str, value_path_krw: dict) -> dict:
    """Find the first supplied month with fundamental value >= current price.

    No interpolation/extrapolation or monotonicity assumption is made. A later
    fall below price is reported separately, so first crossing is not described
    as permanent convergence. Not reaching price means only within this grid.
    """
    price = decimal(current_price_krw, "current_price_krw", minimum=0)
    if not value_path_krw:
        raise ValueError("value_path_krw cannot be empty")
    path = sorted(
        (integer(month, "month"), decimal(value, "fundamental_value_krw", minimum=0))
        for month, value in value_path_krw.items()
    )
    if len({month for month, _ in path}) != len(path) or path[0][0] != 0:
        raise ValueError("value path needs distinct months and must start at month 0")
    first = next((month for month, value in path if value >= price), None)
    decreasing = any(right[1] < left[1] for left, right in pairwise(path))
    increasing = any(right[1] > left[1] for left, right in pairwise(path))
    return {
        "status": "reached_on_grid" if first is not None else "not_reached_within_horizon",
        "first_crossing_month": first,
        "first_crossing_years": ratio(Decimal(first) / 12) if first is not None else None,
        "non_monotonic": decreasing and increasing,
        "nondecreasing": not decreasing,
        "falls_below_after_crossing": first is not None
        and any(month > first and value < price for month, value in path),
        "last_evaluated_month": path[-1][0],
        "gap_ratios": {str(month): ratio(price / value - 1) if value else None for month, value in path},
        "resolution": "supplied_months_only",
    }
