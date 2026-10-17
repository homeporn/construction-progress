from datetime import date, datetime, time, timezone


def day(value):
    if isinstance(value, datetime):
        raise ValueError("Expected a calendar date, not a timestamp")
    if isinstance(value, date):
        return value.isoformat()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Expected YYYY-MM-DD")
    return value


def instant(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def end_of_day(value):
    return instant(datetime.combine(date.fromisoformat(day(value)), time.max, timezone.utc))
