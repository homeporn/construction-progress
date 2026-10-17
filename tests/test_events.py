import sqlite3
import pytest

from app.repositories.base import ValidationError


def test_confirmation_exceeding_actual_rolls_back_event_and_audit(db, events, item, plan):
    events.add_fact(item, "10", "2026-01-10", event_key="fact", recorded_at="2026-01-10T12:00:00Z")
    audit_count = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValidationError, match="confirmation exceeds actual"):
        events.add_confirmation(item, "11", "2026-01-10", event_key="confirmation", document_ref="Акт",
                                recorded_at="2026-01-11T12:00:00Z")
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audit_count


def test_duplicate_event_key_rejected_even_with_different_registration_time(db, events, item):
    events.add_fact(item, "10", "2026-01-10", event_key="same", recorded_at="2026-01-10T12:00:00Z")
    with pytest.raises(ValidationError):
        events.add_fact(item, "10", "2026-01-10", event_key="same", recorded_at="2026-01-11T12:00:00Z")
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0] == 1


def test_backdated_correction_cannot_invalidate_an_earlier_business_slice(db, events, item):
    original = events.add_fact(item, "100", "2026-01-10", event_key="f1", recorded_at="2026-01-10T12:00:00Z")
    events.add_confirmation(item, "90", "2026-01-11", event_key="c1", document_ref="Акт",
                            recorded_at="2026-01-11T12:00:00Z")
    events.add_fact(item, "100", "2026-01-20", event_key="f2", recorded_at="2026-01-20T12:00:00Z")
    # Today's total would remain valid (180 >= 90), but January 11 would be 80 < 90.
    with pytest.raises(ValidationError):
        events.add_fact(item, "-20", "2026-01-10", event_key="fix", correction_of_id=original,
                        recorded_at="2026-02-01T12:00:00Z")


def test_correct_confirmation_before_reducing_actual(db, events, item):
    actual = events.add_fact(item, "100", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    confirmed = events.add_confirmation(item, "90", "2026-01-11", event_key="c", document_ref="Акт",
                                        recorded_at="2026-01-11T12:00:00Z")
    events.add_confirmation(item, "-20", "2026-01-11", event_key="cf", document_ref="Корректирующий акт",
                            correction_of_id=confirmed, recorded_at="2026-02-01T12:00:00Z")
    events.add_fact(item, "-20", "2026-01-10", event_key="ff", correction_of_id=actual,
                    recorded_at="2026-02-01T12:01:00Z")
    assert db.execute("SELECT count(*) FROM fact_events").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM confirmation_events").fetchone()[0] == 2


def test_correction_cannot_make_cumulative_actual_negative(events, item):
    original = events.add_fact(item, "10", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    with pytest.raises(ValidationError):
        events.add_fact(item, "-11", "2026-01-10", event_key="fix", correction_of_id=original,
                        recorded_at="2026-01-11T12:00:00Z")


@pytest.mark.parametrize("table", ["fact_events", "confirmation_events"])
@pytest.mark.parametrize("operation", ["UPDATE {table} SET quantity_delta='1'", "DELETE FROM {table}"])
def test_event_journals_are_append_only(db, events, item, table, operation):
    events.add_fact(item, "10", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    events.add_confirmation(item, "5", "2026-01-10", event_key="c", document_ref="Акт",
                            recorded_at="2026-01-10T12:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(operation.format(table=table))


def test_invalid_correction_reference_unit_and_chronology(events, item):
    original = events.add_fact(item, "10", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    for kwargs in ({"correction_of_id": 999},
                   {"correction_of_id": original, "work_date": "2026-01-09"},
                   {"correction_of_id": original, "recorded_at": "2026-01-10T11:00:00Z"},
                   {"unit": "шт"}):
        values = dict(work_date="2026-01-10", recorded_at="2026-01-11T12:00:00Z")
        values.update(kwargs)
        with pytest.raises(ValidationError):
            events.add_fact(item, "-1", event_key="bad", **values)


def test_confirmation_requires_document_and_negative_requires_reference(events, item):
    events.add_fact(item, "10", "2026-01-10", event_key="f", recorded_at="2026-01-10T12:00:00Z")
    with pytest.raises(ValidationError):
        events.add_confirmation(item, "1", "2026-01-10", event_key="c", document_ref=" ",
                                recorded_at="2026-01-10T12:00:00Z")
    with pytest.raises(ValidationError):
        events.add_fact(item, "-1", "2026-01-10", event_key="negative", recorded_at="2026-01-10T12:00:00Z")


def test_naive_timestamp_is_rejected(events, item):
    with pytest.raises(ValueError, match="timezone"):
        events.add_fact(item, "1", "2026-01-10", event_key="naive", recorded_at="2026-01-10T12:00:00")
