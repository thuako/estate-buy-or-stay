import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from estate_harness.calculators import (
    bounded_break_even,
    calculate_scenario,
    compare_housing_strategies,
    discounted_cash_flow,
    pricing_horizon,
    stock_flow,
)


@pytest.fixture
def household():
    return {
        "horizon_months": 48,
        "lease_end_month": 7,
        "initial_liquid_assets_krw": 2000,
        "rental_deposit_krw": 100,
        "deposit_available_month": 7,
        "existing_debt_krw": 0,
        "existing_debt_terminal_krw": 0,
        "monthly_nonhousing_surplus_krw": 100,
        "monthly_rent_krw": 10,
        "monthly_ownership_cost_krw": 2,
        "investment_monthly_rate": 0,
        "mortgage_principal_krw": 600,
        "mortgage_monthly_rate": 0,
        "mortgage_term_months": 60,
        "purchase_cost_rate": 0,
        "purchase_fixed_cost_krw": 0,
        "sale_cost_rate": 0,
        "sale_fixed_cost_krw": 0,
        "moving_cost_krw": 0,
        "house_price_path_krw": {"0": 1000, "7": 1000, "24": 1000, "36": 1000, "48": 1000},
        "affordability": {
            "annual_household_income_krw": 2400,
            "lender_confirmed_borrowing_capacity_krw": 600,
            "monthly_housing_payment_limit_krw": 30,
            "emergency_reserve_krw": 10,
            "max_purchase_price_krw": 1100,
            "transaction_costs_included_in_budget": True,
        },
    }


def rows_by_name(result):
    return {row["strategy"]: row for row in result["strategies"]}


def test_dcf_includes_terminal_at_matching_horizon():
    value = discounted_cash_flow([110, 121], ["0.1", "0.1"], 1210)
    assert value["fundamental_value_krw"] == 1200
    assert value["discounted_rents_krw"] == 200
    assert value["discounted_terminal_value_krw"] == 1000
    assert value["terminal_sensitivity_krw"] == {"minus_10_percent": 1100, "plus_10_percent": 1300}


def test_dcf_rejects_mismatched_periods_and_invalid_discount():
    with pytest.raises(ValueError, match="equal"):
        discounted_cash_flow([1, 2], [0], 100)
    with pytest.raises(ValueError, match="greater than -1"):
        discounted_cash_flow([1], [-1], 100)


def test_first_crossing_handles_nonmonotonic_path():
    value = pricing_horizon(100, {"0": 90, "12": 105, "24": 95, "36": 110})
    assert value["first_crossing_month"] == 12
    assert value["non_monotonic"] is True
    assert value["falls_below_after_crossing"] is True
    assert value["resolution"] == "supplied_months_only"


def test_no_crossing_does_not_extrapolate():
    value = pricing_horizon(100, {"0": 90, "24": 91, "36": 92})
    assert value["status"] == "not_reached_within_horizon"
    assert value["first_crossing_month"] is None
    assert value["last_evaluated_month"] == 36
    assert pricing_horizon(100, {"0": 110, "12": 105})["non_monotonic"] is False


def test_stock_does_not_count_vacancy_as_new_supply():
    value = stock_flow(
        100,
        [
            {
                "period": "2027",
                "completions": 10,
                "demolitions": 3,
                "net_conversions": -2,
                "available_vacant_units": 20,
            }
        ],
    )
    assert value["final_stock"] == 105
    assert value["periods"][0]["available_vacant_units"] == 20
    with pytest.raises(ValueError, match="not stock additions"):
        stock_flow(
            100,
            [
                {
                    "period": "2027",
                    "completions": 0,
                    "demolitions": 0,
                    "net_conversions": 0,
                    "vacancies_added": 20,
                }
            ],
        )


def test_stock_rejects_duplicate_period_and_negative_inventory():
    flow = {"period": "2027", "completions": 0, "demolitions": 3, "net_conversions": 0}
    with pytest.raises(ValueError, match="unique"):
        stock_flow(100, [flow, flow])
    with pytest.raises(ValueError, match="negative"):
        stock_flow(2, [flow])


def test_principal_is_cash_outflow_and_debt_reduction_once(household):
    rows = rows_by_name(compare_housing_strategies(household))
    bought = rows["buy_now"]
    assert bought["total_principal_krw"] == 480
    assert bought["terminal_mortgage_balance_krw"] == 120
    assert bought["terminal_net_worth_krw"] == 2100 + 4800 - 70 - 96
    assert rows["keep_renting"]["terminal_net_worth_krw"] == 2100 + 4800 - 480
    debt_free = deepcopy(household)
    debt_free["mortgage_principal_krw"] = 0
    no_debt = rows_by_name(compare_housing_strategies(debt_free))
    # At zero interest and investment return, amortization has no wealth cost.
    assert no_debt["buy_now"]["terminal_net_worth_krw"] == bought["terminal_net_worth_krw"]


def test_all_strategies_start_equal_and_use_same_end_date(household):
    result = compare_housing_strategies(household)
    assert result["horizon_months"] == 48
    assert result["initial_net_worth_krw"] == 2100
    rows = rows_by_name(result)
    assert {row["buy_month"] for row in rows.values()} == {0, 7, 24, 36, None}
    for row in rows.values():
        assert row["terminal_net_worth_krw"] == (
            result["initial_net_worth_krw"] + 4800 - row["total_rent_krw"] - row["total_ownership_costs_krw"]
        )


def test_renter_investment_gain_is_the_only_opportunity_cost(household):
    household.update(
        {
            "horizon_months": 12,
            "lease_end_month": 0,
            "initial_liquid_assets_krw": 100,
            "rental_deposit_krw": 0,
            "deposit_available_month": 0,
            "monthly_nonhousing_surplus_krw": 0,
            "monthly_rent_krw": 0,
            "monthly_ownership_cost_krw": 0,
            "mortgage_principal_krw": 0,
            "investment_monthly_rate": "0.01",
            "house_price_path_krw": {"0": 100, "12": 100},
        }
    )
    household["affordability"]["emergency_reserve_krw"] = 0
    rows = rows_by_name(compare_housing_strategies(household))
    assert rows["buy_now"]["terminal_net_worth_krw"] == 100
    assert rows["keep_renting"]["terminal_net_worth_krw"] == 113
    assert rows["buy_now"]["net_worth_difference_vs_renting_krw"] == -13
    assert rows["buy_after_24_months"]["status"] == "outside_valuation_horizon"


def test_lease_deposit_cannot_fund_purchase_before_its_release(household):
    household["initial_liquid_assets_krw"] = 350
    rows = rows_by_name(compare_housing_strategies(household))
    assert "unfunded_cash_deficit" in rows["buy_now"]["constraint_failures"]
    assert rows["buy_now"]["comparison_valid"] is False
    assert rows["buy_at_lease_end"]["affordability"] == "passes_supplied_constraints"
    assert rows["buy_now"]["total_rent_krw"] == 70


def test_missing_income_and_lender_capacity_leave_affordability_unknown(household):
    del household["affordability"]["annual_household_income_krw"]
    household["affordability"]["lender_confirmed_borrowing_capacity_krw"] = None
    row = rows_by_name(compare_housing_strategies(household))["buy_now"]
    assert row["affordability"] == "unknown"
    assert row["terminal_net_worth_krw"] == 6734
    assert "annual_household_income_krw" in row["missing_affordability_inputs"]
    del household["initial_liquid_assets_krw"]
    assert compare_housing_strategies(household)["status"] == "unknown"


def test_budget_applies_transaction_costs_only_when_confirmed(household):
    household["purchase_cost_rate"] = "0.15"
    rows = rows_by_name(compare_housing_strategies(household))
    assert "exceeds_purchase_budget" in rows["buy_now"]["constraint_failures"]
    household["affordability"]["transaction_costs_included_in_budget"] = False
    rows = rows_by_name(compare_housing_strategies(household))
    assert "exceeds_purchase_budget" not in rows["buy_now"]["constraint_failures"]


def test_general_wealth_identity_with_interest_fees_and_resale(household):
    household.update(
        {
            "mortgage_monthly_rate": "0.01",
            "investment_monthly_rate": "0.002",
            "purchase_cost_rate": "0.02",
            "sale_cost_rate": "0.03",
            "moving_cost_krw": 15,
        }
    )
    household["house_price_path_krw"]["48"] = 1200
    result = compare_housing_strategies(household)
    row = rows_by_name(result)["buy_now"]
    expected = (
        2100
        + 4800
        + 200
        + row["total_investment_income_krw"]
        - row["total_rent_krw"]
        - row["total_interest_krw"]
        - row["total_ownership_costs_krw"]
        - row["total_purchase_costs_krw"]
        - row["terminal_sale_cost_krw"]
    )
    assert abs(row["terminal_net_worth_krw"] - expected) <= 2


def test_break_even_is_bounded_and_does_not_claim_absent_roots():
    assert bounded_break_even(lambda x: 100 - x, 0, 200)["price_krw"] == 100
    assert bounded_break_even(lambda x: x + 1, 0, 100)["status"] == "not_bracketed"
    # Two interior roots with same-sign endpoints require a different search.
    assert bounded_break_even(lambda x: (x - 2) * (x - 4), 0, 6)["status"] == "not_bracketed"
    result = bounded_break_even(lambda x: x - Decimal("1.234"), 0, 100, max_iterations=1)
    assert result["status"] == "iteration_limit"
    assert result["evaluations"] == 3


def test_json_example_runs_and_serializes():
    path = Path(__file__).resolve().parents[1] / "examples" / "scenario.json"
    scenario = json.loads(path.read_text())
    result = calculate_scenario(scenario)
    json.dumps(result, allow_nan=False)
    assert result["claim_type"] == "calculation"
    assert set(result["valuation"]["snapshots"]) == {"0", "24", "36"}
    assert len(result["household"]["strategies"]) == 5
    assert result["pricing_horizon"]["low_assumption"]["status"] == "not_reached_within_horizon"
    assert result["break_even"]["evaluations"] <= 62


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", None])
def test_nonfinite_and_boolean_financial_inputs_are_rejected(bad):
    with pytest.raises(ValueError):
        discounted_cash_flow([100], [bad], 1000)
