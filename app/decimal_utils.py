"""Exact storage and arithmetic; rounding is confined to final ratios."""
import re
from decimal import Decimal, Inexact, ROUND_HALF_EVEN, localcontext
from typing import Iterable

CANONICAL = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,8}[1-9])?\Z")
ZERO = Decimal("0")


def decimal_value(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Use Decimal, int or a decimal string, never float/bool")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise ValueError("A finite decimal is required")
    return result


def canonical(value):
    value = decimal_value(value)
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    if value == 0:
        result = "0"
    if not CANONICAL.fullmatch(result):
        raise ValueError("Storage permits 18 integer and 9 fractional digits")
    return result


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    values = list(values)
    if not values:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = max(60, max(x.adjusted() for x in values)
                       - min(x.as_tuple().exponent for x in values)
                       + len(str(len(values))) + 3)
        ctx.traps[Inexact] = True
        return sum(values, ZERO)


def multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = max(60, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2)
        ctx.traps[Inexact] = True
        return left * right


def subtract(left: Decimal, right: Decimal) -> Decimal:
    return exact_sum([left, right.copy_negate()])


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    # Non-terminating decimal ratios cannot be represented exactly.
    # No ratio is ever used to accumulate labor or calculate another group.
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        ctx.traps[Inexact] = False
        return numerator / denominator
