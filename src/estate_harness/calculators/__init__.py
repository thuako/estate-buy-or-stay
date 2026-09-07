"""Versioned, deterministic arithmetic for research agents.

``calculate_scenario(payload)`` accepts any combination of ``valuation``,
``pricing_horizon``, ``stock``, ``household``, and ``break_even``. The checked-in
examples/scenario.json is the input contract. Missing sections are not invented.
All outputs are scenario calculations, never verified facts or recommendations.
No network, model, tax tables, or current lending rules are used here.
"""

from .common import integer
from .housing import bounded_break_even, break_even_purchase_price, compare_housing_strategies
from .inventory import stock_flow
from .valuation import discounted_cash_flow, pricing_horizon

CALCULATOR_VERSION = "0.1.0"


def calculate_scenario(payload: dict) -> dict:
    """Calculate supplied sections, validating explicit monthly/period conventions.

    ``valuation.snapshots`` must provide DCF inputs at months 0, 24, and 36. Each
    snapshot describes future rents *from that date*, not a present-discounted
    version of today's value. Additional snapshots are allowed. ``period_months``
    is descriptive and must match the rent/rate convention supplied by the user.
    Missing affordability details yield unknown instead of being inferred.
    """
    if not isinstance(payload, dict):
        raise TypeError("scenario must be a JSON object")
    sections = {"valuation", "pricing_horizon", "stock", "household", "break_even"} & payload.keys()
    if not sections:
        raise ValueError("scenario needs at least one calculator section")
    result = {
        "calculator_version": CALCULATOR_VERSION,
        "claim_type": "calculation",
        "basis": "caller_supplied_assumptions",
        "currency": "KRW",
    }
    if "valuation" in payload:
        valuation = payload["valuation"]
        snapshots = {
            str(integer(key, "snapshot month")): value for key, value in valuation["snapshots"].items()
        }
        if not {"0", "24", "36"}.issubset(snapshots):
            raise ValueError("valuation snapshots must include months 0, 24, and 36")
        result["valuation"] = {
            "period_months": integer(valuation["period_months"], "period_months", minimum=1),
            "snapshots": {month: discounted_cash_flow(**snapshot) for month, snapshot in snapshots.items()},
        }
    if "pricing_horizon" in payload:
        values = payload["pricing_horizon"]
        if "paths_krw" in values:
            if not values["paths_krw"]:
                raise ValueError("paths_krw must contain at least one named path")
            result["pricing_horizon"] = {
                name: pricing_horizon(values["current_price_krw"], path)
                for name, path in values["paths_krw"].items()
            }
        else:
            result["pricing_horizon"] = pricing_horizon(**values)
    if "stock" in payload:
        result["stock"] = stock_flow(**payload["stock"])
    if "household" in payload:
        result["household"] = compare_housing_strategies(payload["household"])
    if "break_even" in payload:
        if "household" not in payload:
            raise ValueError("break_even requires a household section")
        result["break_even"] = break_even_purchase_price(payload["household"], payload["break_even"])
    return result


__all__ = [
    "bounded_break_even",
    "break_even_purchase_price",
    "calculate_scenario",
    "compare_housing_strategies",
    "discounted_cash_flow",
    "pricing_horizon",
    "stock_flow",
]
