"""Monthly, equal-starting-assets buy/rent scenario accounting.

This is a deterministic cash-flow calculator, not tax or lending advice. All
prices, tax/fee rates, loan terms, and investment returns are caller assumptions.
Rents/deposits are constant; a current lease persists until its end when buying
early, and is assumed renewable on the same terms while waiting. A new mortgage
is fixed-rate level-payment amortizing. Existing-debt service must already be
deducted from monthly_nonhousing_surplus_krw; its ending balance is supplied.
No implicit bridge financing, rent-loan product, tax relief, or utility score is
invented. Utility and local legal/lender eligibility need separate assessment.

Purchases/deposit release occur at the start of a month; cash earns its stated
return during that month, followed by surplus and housing outflows. The final
valuation follows horizon_months completed months, with modeled sale costs.
Principal payments reduce both cash and debt and are never expensed twice.
Opportunity cost appears only through actual remaining-cash investment returns.
"""

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal, localcontext

from .common import decimal, integer, krw


def _payment(principal: Decimal, rate: Decimal, term: int) -> Decimal:
    if not principal:
        return Decimal(0)
    if not rate:
        return principal / term
    return principal * rate / (1 - (1 + rate) ** -term)


def compare_housing_strategies(inputs: dict) -> dict:
    """Compare buy now, lease end, month 24/36, and rent at one end date.

    Monetary arithmetic is still possible when lender/income/budget confirmation
    is missing; affordability then remains unknown. Missing accounting inputs
    return unknown instead of silently becoming zero. Any cash deficit makes
    the arithmetic financially infeasible, with no invented borrowing facility.
    All money is rounded only when serialized; rates are explicitly monthly.
    """
    required = (
        "horizon_months",
        "lease_end_month",
        "initial_liquid_assets_krw",
        "rental_deposit_krw",
        "deposit_available_month",
        "monthly_nonhousing_surplus_krw",
        "monthly_rent_krw",
        "monthly_ownership_cost_krw",
        "investment_monthly_rate",
        "mortgage_principal_krw",
        "mortgage_monthly_rate",
        "mortgage_term_months",
        "purchase_cost_rate",
        "purchase_fixed_cost_krw",
        "sale_cost_rate",
        "sale_fixed_cost_krw",
        "moving_cost_krw",
        "house_price_path_krw",
        "existing_debt_krw",
        "existing_debt_terminal_krw",
    )
    missing = [key for key in required if inputs.get(key) is None]
    if missing:
        return {"status": "unknown", "missing_inputs": missing, "strategies": []}
    with localcontext() as context:
        context.prec = 40
        return _compare(inputs)


def _compare(inputs: dict) -> dict:
    horizon = integer(inputs["horizon_months"], "horizon_months", minimum=1)
    if horizon > 1200:
        raise ValueError("horizon_months must be <= 1200")
    lease_end = integer(inputs["lease_end_month"], "lease_end_month")
    release = integer(inputs["deposit_available_month"], "deposit_available_month")
    term = integer(inputs["mortgage_term_months"], "mortgage_term_months", minimum=1)
    money_keys = (
        "initial_liquid_assets_krw",
        "rental_deposit_krw",
        "monthly_rent_krw",
        "monthly_ownership_cost_krw",
        "mortgage_principal_krw",
        "purchase_fixed_cost_krw",
        "sale_fixed_cost_krw",
        "moving_cost_krw",
        "existing_debt_krw",
        "existing_debt_terminal_krw",
    )
    amounts = {key: decimal(inputs[key], key, minimum=0) for key in money_keys}
    surplus = decimal(inputs["monthly_nonhousing_surplus_krw"], "monthly_nonhousing_surplus_krw")
    invest = decimal(inputs["investment_monthly_rate"], "investment_monthly_rate")
    loan_rate = decimal(inputs["mortgage_monthly_rate"], "mortgage_monthly_rate", minimum=0)
    if invest <= -1:
        raise ValueError("investment_monthly_rate must be > -1")
    purchase_rate = decimal(inputs["purchase_cost_rate"], "purchase_cost_rate", minimum=0)
    sale_rate = decimal(inputs["sale_cost_rate"], "sale_cost_rate", minimum=0)
    if purchase_rate > 1 or sale_rate > 1:
        raise ValueError("purchase_cost_rate and sale_cost_rate must be <= 1")
    prices = {
        integer(month, "price month"): decimal(value, "house price", minimum=0)
        for month, value in inputs["house_price_path_krw"].items()
    }
    if len(prices) != len(inputs["house_price_path_krw"]):
        raise ValueError("house price path contains duplicate months")
    strategies = [
        ("buy_now", 0),
        ("buy_at_lease_end", lease_end),
        ("buy_after_24_months", 24),
        ("buy_after_36_months", 36),
        ("keep_renting", None),
    ]
    needed_months = {horizon} | {month for _, month in strategies if month is not None and month < horizon}
    missing_prices = sorted(needed_months - prices.keys())
    if missing_prices:
        raise ValueError(f"house_price_path_krw requires explicit prices at months {missing_prices}")
    conditions = inputs.get("affordability", {})
    condition_keys = (
        "annual_household_income_krw",
        "lender_confirmed_borrowing_capacity_krw",
        "monthly_housing_payment_limit_krw",
        "emergency_reserve_krw",
        "max_purchase_price_krw",
        "transaction_costs_included_in_budget",
    )
    missing_conditions = [key for key in condition_keys if conditions.get(key) is None]
    for key in condition_keys[:-1]:
        if conditions.get(key) is not None:
            decimal(conditions[key], key, minimum=0)
    if conditions.get("transaction_costs_included_in_budget") is not None and not isinstance(
        conditions["transaction_costs_included_in_budget"], bool
    ):
        raise ValueError("transaction_costs_included_in_budget must be boolean or null")
    rows = []
    for name, buy_month in strategies:
        if buy_month is not None and buy_month >= horizon:
            rows.append({"strategy": name, "buy_month": buy_month, "status": "outside_valuation_horizon"})
            continue
        cash = amounts["initial_liquid_assets_krw"]
        deposit = amounts["rental_deposit_krw"]
        balance = Decimal(0)
        minimum_cash = cash
        totals = {
            key: Decimal(0)
            for key in (
                "investment_income",
                "rent",
                "interest",
                "principal",
                "ownership_costs",
                "purchase_costs",
            )
        }
        maximum_monthly_payment = Decimal(0)
        bought = False
        purchase_price = prices[buy_month] if buy_month is not None else Decimal(0)
        purchase_fees = (
            purchase_price * purchase_rate + amounts["purchase_fixed_cost_krw"]
            if buy_month is not None
            else Decimal(0)
        )
        # Release cannot precede purchase/lease exit or the explicit availability assumption.
        release_month = max(buy_month, lease_end, release) if buy_month is not None else None
        loan_payment = _payment(amounts["mortgage_principal_krw"], loan_rate, term)
        for month in range(horizon):
            if release_month is not None and month == release_month:
                cash += deposit
                deposit = Decimal(0)
            if month == buy_month:
                bought = True
                balance = amounts["mortgage_principal_krw"]
                cash -= purchase_price + purchase_fees + amounts["moving_cost_krw"] - balance
                totals["purchase_costs"] += purchase_fees + amounts["moving_cost_krw"]
                minimum_cash = min(minimum_cash, cash)
            # Negative cash is an unfunded gap, never an authorized credit line.
            investment_income = max(cash, Decimal(0)) * invest
            cash += investment_income
            totals["investment_income"] += investment_income
            rent = amounts["monthly_rent_krw"] if (not bought or month < lease_end) else Decimal(0)
            ownership = amounts["monthly_ownership_cost_krw"] if bought else Decimal(0)
            interest = balance * loan_rate
            payment = min(loan_payment, balance + interest) if balance else Decimal(0)
            principal = payment - interest
            balance -= principal
            housing_outflow = rent + ownership + payment
            maximum_monthly_payment = max(maximum_monthly_payment, housing_outflow)
            cash += surplus - housing_outflow
            minimum_cash = min(minimum_cash, cash)
            totals["rent"] += rent
            totals["ownership_costs"] += ownership
            totals["interest"] += interest
            totals["principal"] += principal
        # A deposit due exactly at the final instant is released before valuation.
        if release_month == horizon:
            cash += deposit
            deposit = Decimal(0)
        home_value = prices[horizon] if bought else Decimal(0)
        sale_cost = home_value * sale_rate + amounts["sale_fixed_cost_krw"] if bought else Decimal(0)
        ending_wealth = (
            cash + deposit + home_value - sale_cost - balance - amounts["existing_debt_terminal_krw"]
        )
        failures = []
        if minimum_cash < 0:
            failures.append("unfunded_cash_deficit")
        if bought:
            if amounts["mortgage_principal_krw"] > purchase_price:
                failures.append("mortgage_exceeds_purchase_price")
            if conditions.get("lender_confirmed_borrowing_capacity_krw") is not None and amounts[
                "mortgage_principal_krw"
            ] > decimal(conditions["lender_confirmed_borrowing_capacity_krw"]):
                failures.append("exceeds_confirmed_borrowing_capacity")
            if conditions.get("max_purchase_price_krw") is not None:
                budget_amount = (
                    purchase_price + purchase_fees
                    if conditions.get("transaction_costs_included_in_budget")
                    else purchase_price
                )
                if budget_amount > decimal(conditions["max_purchase_price_krw"]):
                    failures.append("exceeds_purchase_budget")
        if conditions.get("emergency_reserve_krw") is not None and minimum_cash < decimal(
            conditions["emergency_reserve_krw"]
        ):
            failures.append("below_emergency_reserve")
        if conditions.get(
            "monthly_housing_payment_limit_krw"
        ) is not None and maximum_monthly_payment > decimal(conditions["monthly_housing_payment_limit_krw"]):
            failures.append("exceeds_monthly_housing_payment_limit")
        feasibility = (
            "infeasible" if failures else ("unknown" if missing_conditions else "passes_supplied_constraints")
        )
        rows.append(
            {
                "strategy": name,
                "buy_month": buy_month,
                "status": "calculated",
                "affordability": feasibility,
                "missing_affordability_inputs": missing_conditions,
                "constraint_failures": failures,
                "terminal_liquid_assets_krw": krw(cash),
                "terminal_rental_deposit_krw": krw(deposit),
                "terminal_home_value_krw": krw(home_value),
                "terminal_mortgage_balance_krw": krw(balance),
                "terminal_existing_debt_krw": krw(amounts["existing_debt_terminal_krw"]),
                "terminal_sale_cost_krw": krw(sale_cost),
                "terminal_net_worth_krw": krw(ending_wealth),
                "minimum_liquid_assets_krw": krw(minimum_cash),
                "maximum_monthly_housing_outflow_krw": krw(maximum_monthly_payment),
                "comparison_valid": not failures,
                **{f"total_{key}_krw": krw(value) for key, value in totals.items()},
            }
        )
    rental = next(row for row in rows if row["strategy"] == "keep_renting")
    for row in rows:
        if row["status"] == "calculated":
            row["net_worth_difference_vs_renting_krw"] = (
                row["terminal_net_worth_krw"] - rental["terminal_net_worth_krw"]
            )
    return {
        "status": "calculated",
        "horizon_months": horizon,
        "initial_net_worth_krw": krw(
            amounts["initial_liquid_assets_krw"]
            + amounts["rental_deposit_krw"]
            - amounts["existing_debt_krw"]
        ),
        "strategies": rows,
        "accounting": "cash_plus_home_plus_deposit_minus_outstanding_debt_minus_sale_costs",
        "limitations": [
            "Conditional arithmetic; no market forecasts or automatic recommendation.",
            "Affordability checks only supplied constraints and do not constitute lender approval.",
            "Constant rental terms and fixed-rate level-payment mortgage; no implicit bridge financing.",
            "Existing debt service is included in nonhousing surplus; terminal existing debt is supplied.",
            "Rows with constraint failures are hypothetical and cannot be funded under the supplied assumptions.",
            "Investment income is after-tax by assumption; housing utility is assessed separately.",
        ],
    }


def bounded_break_even(
    difference: Callable[[Decimal], Decimal],
    lower: int | str,
    upper: int | str,
    *,
    tolerance_krw: int = 1,
    max_iterations: int = 80,
) -> dict:
    """Bisect one sign-bracketed, continuous difference within explicit bounds.

    The caller must establish continuity and monotonicity to interpret a unique
    break-even price. Same-sign endpoints return 'not_bracketed', not proof that
    no roots exist; no extrapolation or unbounded search is performed.
    """
    low = decimal(lower, "lower", minimum=0)
    high = decimal(upper, "upper", minimum=0)
    tolerance = decimal(tolerance_krw, "tolerance_krw", minimum=1)
    iterations = integer(max_iterations, "max_iterations", minimum=1)
    if high <= low or iterations > 200:
        raise ValueError("upper must exceed lower and max_iterations must be <= 200")
    f_low, f_high = (
        decimal(difference(low), "lower difference"),
        decimal(difference(high), "upper difference"),
    )
    evaluations = 2
    if not f_low or not f_high:
        root = low if not f_low else high
        return {
            "status": "found",
            "price_krw": krw(root),
            "bracket_krw": [krw(root), krw(root)],
            "evaluations": evaluations,
        }
    if f_low * f_high > 0:
        return {
            "status": "not_bracketed",
            "price_krw": None,
            "bracket_krw": [krw(low), krw(high)],
            "evaluations": evaluations,
        }
    for _ in range(iterations):
        middle = (low + high) / 2
        f_middle = decimal(difference(middle), "midpoint difference")
        evaluations += 1
        if not f_middle or high - low <= tolerance:
            return {
                "status": "found",
                "price_krw": krw(middle),
                "bracket_krw": [krw(low), krw(high)],
                "evaluations": evaluations,
            }
        if f_low * f_middle < 0:
            high = middle
        else:
            low, f_low = middle, f_middle
    return {
        "status": "iteration_limit",
        "price_krw": None,
        "bracket_krw": [krw(low), krw(high)],
        "evaluations": evaluations,
    }


def break_even_purchase_price(household: dict, bounds: dict) -> dict:
    """Find buy-now versus renting terminal-wealth parity, holding future prices fixed.

    Fixed debt, fees and nonnegative remaining cash imply a monotonic difference.
    Endpoint cash deficits disable the calculation; eligibility constraints are
    reported independently and never interpreted as an affordable price promise.
    This price sensitivity does not forecast resale price from purchase price.
    """

    def difference(price: Decimal) -> Decimal:
        scenario = deepcopy(household)
        scenario["house_price_path_krw"] = {str(k): v for k, v in scenario["house_price_path_krw"].items()}
        scenario["house_price_path_krw"]["0"] = str(price)
        result = compare_housing_strategies(scenario)
        if result["status"] != "calculated":
            raise ValueError("break-even requires complete accounting inputs")
        buy = next(row for row in result["strategies"] if row["strategy"] == "buy_now")
        rent = next(row for row in result["strategies"] if row["strategy"] == "keep_renting")
        if (
            "unfunded_cash_deficit" in buy["constraint_failures"]
            or "unfunded_cash_deficit" in rent["constraint_failures"]
        ):
            raise ValueError("break-even bounds require funded buy-now and rental cash paths")
        if "mortgage_exceeds_purchase_price" in buy["constraint_failures"]:
            raise ValueError("break-even lower price cannot be below mortgage principal")
        return Decimal(buy["net_worth_difference_vs_renting_krw"])

    result = bounded_break_even(
        difference,
        bounds["lower_price_krw"],
        bounds["upper_price_krw"],
        tolerance_krw=bounds.get("tolerance_krw", 1),
        max_iterations=bounds.get("max_iterations", 80),
    )
    result["definition"] = "buy_now_vs_keep_renting_at_fixed_terminal_home_value"
    result["affordability_is_separate"] = True
    return result
