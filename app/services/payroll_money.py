"""Decimal money boundaries and exact, deterministic minor-unit allocation."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction

from app.decimal_utils import decimal_value


def to_minor(value):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError('Денежная сумма: требуется Decimal, целое или строка')
    number = decimal_value(value)
    with localcontext() as context:
        context.prec = max(60, len(number.as_tuple().digits) + abs(number.as_tuple().exponent) + 4)
        return int((number * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_minor(value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('Сумма должна быть целым числом копеек')
    number = Decimal(value).as_tuple()
    return Decimal((number.sign, number.digits, number.exponent - 2))


def split_money(total_minor, weights):
    """Largest remainders use exact fractions; equal remainders prefer lower ID."""
    if not isinstance(total_minor, int) or isinstance(total_minor, bool):
        raise ValueError('Сумма должна быть целым числом копеек')
    values = {key: Fraction(decimal_value(value)) for key, value in weights.items()}
    if any(value < 0 for value in values.values()) or not values or sum(values.values()) <= 0:
        raise ValueError('Укажите неотрицательные веса с положительной суммой')
    total_weight = sum(values.values())
    shares = {key: abs(total_minor) * value / total_weight for key, value in values.items()}
    result = {key: share.numerator // share.denominator for key, share in shares.items()}
    remainder = abs(total_minor) - sum(result.values())
    order = sorted(result, key=lambda key: (-(shares[key] - result[key]), key))
    for key in order[:remainder]:
        result[key] += 1
    sign = -1 if total_minor < 0 else 1
    return {key: sign * result[key] for key in sorted(result)}
