from decimal import Decimal, localcontext
import pytest

from app.decimal_utils import canonical, exact_sum, multiply
from app.calculation_engine.labor import planned_labor

D = Decimal


def test_planned_labor():
    assert planned_labor(D("1000"), D("0.8")) == D("800")


def test_exact_product_and_sum_even_with_low_caller_precision():
    with localcontext() as ctx:
        ctx.prec = 6
        actual = multiply(D("999999999999999999.999999999"), D("999999999999999999.999999999"))
        assert actual == D("999999999999999999999999998000000000.000000000000000001")
        assert exact_sum([D("999999999999999999.999999999"), D("0.000000001")]) == D("1000000000000000000")


def test_decimal_tenths_are_exact():
    assert exact_sum([D("0.1")] * 10) == D("1")
    assert planned_labor(D("0.1"), D("0.2")) == D("0.02")


@pytest.mark.parametrize("value", [1.1, True, D("NaN"), D("Infinity"), "1000000000000000000", "0.0000000001"])
def test_storage_rejects_inexact_or_out_of_range_input(value):
    with pytest.raises((ValueError, ArithmeticError)):
        canonical(value)


def test_canonical_storage_format():
    assert canonical("001.2300") == "1.23"
    assert canonical("-0.00") == "0"


@pytest.mark.parametrize("quantity,norm", [("-1", "1"), ("1", "0"), ("1", "-1")])
def test_labor_rejects_invalid_parameters(quantity, norm):
    with pytest.raises(ValueError):
        planned_labor(D(quantity), D(norm))
