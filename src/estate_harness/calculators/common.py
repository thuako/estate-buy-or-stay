"""Strict decimal inputs and integer-KRW serialization for scenario arithmetic."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


def decimal(value: Any, name: str = "value", *, minimum: Decimal | int | None = None) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return number


def integer(value: Any, name: str, *, minimum: int = 0) -> int:
    number = decimal(value, name, minimum=minimum)
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def krw(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
