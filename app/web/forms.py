"""Server-side parsing; browsers and JavaScript are never trusted validators."""
import re
from decimal import Decimal

from app.decimal_utils import canonical
from app.models import Discipline
from app.repositories.base import ValidationError
from app.time_utils import day, instant


class InputError(ValidationError):
    pass


def text(data, name, label, *, required=True, limit=200):
    value = data.get(name, "").strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise InputError("{}: заполните поле (не более {} символов).".format(label, limit))
    return value


def integer(data, name, label, *, optional=False, signed=False):
    value = data.get(name, "").strip()
    if optional and not value:
        return None
    if not re.fullmatch(r"-?[0-9]{1,9}" if signed else r"[0-9]{1,9}", value):
        raise InputError(label + ": требуется целое число.")
    result = int(value)
    if not signed and result <= 0:
        raise InputError(label + ": выберите запись из списка.")
    return result


def quantity(data, name, label, *, optional=False, signed=False):
    value = data.get(name, "").strip().replace(",", ".")
    if optional and not value:
        return None
    if not re.fullmatch(r"-?[0-9]{1,18}(?:\.[0-9]{1,9})?", value):
        raise InputError(label + ": введите десятичное число, до 18 целых и 9 дробных разрядов.")
    result = Decimal(canonical(value))
    if not signed and result < 0:
        raise InputError(label + ": отрицательное значение запрещено.")
    return result


def discipline(data):
    try:
        return Discipline(data.get("discipline", "CONSTRUCTION"))
    except ValueError:
        raise InputError("Выберите направление: СМР или проектирование.") from None


def business_date(data, name, *, default=None):
    try:
        result = day(data.get(name) or default)
    except (ValueError, TypeError):
        raise InputError("Дата: требуется действительная дата в формате ГГГГ-ММ-ДД.") from None
    return result


def report_dates(data):
    now = instant()
    report_date = business_date(data, "report_date", default=now[:10])
    try:
        cutoff = instant(data.get("knowledge_cutoff") or now)
    except (ValueError, TypeError, OverflowError):
        raise InputError("Дата отсечения знаний: укажите дату и время с часовым поясом, например 2026-01-31T18:00:00Z.") from None
    return report_date, cutoff


def item_filters(data):
    return dict(building_id=integer(data, "building_id", "Корпус", optional=True),
                floor_id=integer(data, "floor_id", "Этаж", optional=True),
                element=text(data, "element", "Элемент", required=False),
                work_type=text(data, "work_type", "Вид работ", required=False))
