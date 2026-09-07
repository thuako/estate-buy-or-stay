"""Physical dwelling stock, separate from listings, vacant units, and demand."""

from .common import integer


def stock_flow(initial_stock: int, flows: list[dict]) -> dict:
    """Apply completions - demolitions + net conversions once per period.

    Planned/started units and newly listed vacancies cannot increase stock.
    Counts must use one geography, dwelling type, and nonoverlapping periods;
    this function cannot independently verify source coverage or project IDs.
    """
    stock = integer(initial_stock, "initial_stock")
    rows = []
    periods = set()
    for flow in flows:
        forbidden = {"planned", "starts", "vacancies_added", "new_listings"} & flow.keys()
        if forbidden:
            raise ValueError(f"not stock additions: {', '.join(sorted(forbidden))}")
        period = str(flow["period"])
        if period in periods:
            raise ValueError("stock flow periods must be unique")
        periods.add(period)
        completions = integer(flow["completions"], "completions")
        demolitions = integer(flow["demolitions"], "demolitions")
        conversions = integer(flow["net_conversions"], "net_conversions", minimum=-stock - completions)
        start = stock
        stock += completions - demolitions + conversions
        if stock < 0:
            raise ValueError("physical dwelling stock cannot be negative")
        vacancy = flow.get("available_vacant_units")
        if vacancy is not None:
            vacancy = integer(vacancy, "available_vacant_units")
            if vacancy > stock:
                raise ValueError("available vacancies cannot exceed physical stock")
        rows.append(
            {
                "period": period,
                "opening_stock": start,
                "completions": completions,
                "demolitions": demolitions,
                "net_conversions": conversions,
                "closing_stock": stock,
                "available_vacant_units": vacancy,
            }
        )
    return {"initial_stock": integer(initial_stock, "initial_stock"), "final_stock": stock, "periods": rows}
