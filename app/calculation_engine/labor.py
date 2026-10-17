from app.decimal_utils import decimal_value, multiply


def planned_labor(quantity, norm):
    quantity, norm = decimal_value(quantity), decimal_value(norm)
    if quantity < 0 or norm <= 0:
        raise ValueError("Quantity must be nonnegative and norm positive")
    return multiply(quantity, norm)


def earned_labor(earned_quantity, norm):
    return planned_labor(earned_quantity, norm)
