"""Reference productivity, separate from measured execution and scheduling."""
from app.decimal_utils import decimal_value, multiply, ratio


def normative_output(headcount=None, shift_hours=None, labor_norm=None):
    if headcount in (None, '') or shift_hours in (None, '') or labor_norm in (None, ''):
        return None
    people, hours, norm = map(decimal_value, (headcount, shift_hours, labor_norm))
    if people<=0 or people!=people.to_integral_value() or hours<=0 or hours>24 or norm<=0:
        raise ValueError('Укажите положительную целую численность, часы смены до 24 и положительный норматив')
    return ratio(multiply(people,hours),norm)
