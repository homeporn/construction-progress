from app.decimal_utils import decimal_value
from app.models import Discipline
from .models import Issue, Status


def validate(item):
    issues = []
    if item.discipline not in (Discipline.DESIGN, Discipline.CONSTRUCTION):
        issues.append(Issue("DISCIPLINE", "Неизвестная дисциплина"))
    if not item.in_plan or item.planned_quantity is None:
        issues.append(Issue("MISSING_PLAN_LINE", "Позиция отсутствует в версии плана"))
    if item.labor_norm is None:
        issues.append(Issue("MISSING_NORM", "Не задан норматив трудоёмкости"))
    for field in ("planned_quantity", "labor_norm", "actual", "confirmed"):
        value = getattr(item, field)
        if value is None:
            if field in ("actual", "confirmed"):
                issues.append(Issue("DECIMAL", field + ": требуется конечный Decimal"))
            continue
        try:
            checked = decimal_value(value)
            if not hasattr(value, "as_tuple"):
                raise ValueError("Calculation inputs must be Decimal")
        except (ValueError, TypeError, ArithmeticError):
            issues.append(Issue("DECIMAL", field + ": требуется конечный Decimal"))
            continue
        if field in ("planned_quantity", "labor_norm") and checked <= 0:
            issues.append(Issue("ZERO_PLAN" if field == "planned_quantity" else "INVALID_NORM",
                                field + ": значение должно быть положительным"))
        if field in ("actual", "confirmed") and checked < 0:
            issues.append(Issue("NEGATIVE_" + field.upper(), "Отрицательный накопительный объём"))
    if any(i.code == "DECIMAL" for i in issues):
        return tuple(issues)
    if item.confirmed > item.actual:
        issues.append(Issue("CONFIRMED_EXCEEDS_ACTUAL", "Подтверждение превышает факт"))
    if not item.unit or item.unit != item.plan_unit:
        issues.append(Issue("INCOMPATIBLE_UNITS", "Единица события не совпадает с единицей плана"))
    if item.planned_quantity is not None and item.actual > item.planned_quantity:
        issues.append(Issue("OVERRUN", "Факт превышает план; прогресс ограничен 100%", Status.WARNING))
    return tuple(issues)
