-- Display order does not change floor identity or historical classification.
DROP TRIGGER floors_stable;
CREATE TRIGGER floors_stable BEFORE UPDATE ON floors
WHEN (NEW.id!=OLD.id OR NEW.building_id!=OLD.building_id OR NEW.floor_code!=OLD.floor_code)
    AND EXISTS(SELECT 1 FROM work_items WHERE floor_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT,'Referenced floor identity is immutable');
END;

-- A separate user comment complements, but never replaces, the document reference.
ALTER TABLE confirmation_events ADD COLUMN comment TEXT NOT NULL DEFAULT '';
DROP TRIGGER audit_confirmation_events_insert;
CREATE TRIGGER audit_confirmation_events_insert AFTER INSERT ON confirmation_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('confirmation_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z',
        'NEW.work_item_id=' || quote(NEW.work_item_id) || '; NEW.quantity_delta=' || quote(NEW.quantity_delta)
        || '; NEW.confirmed_date=' || quote(NEW.confirmed_date) || '; NEW.recorded_at=' || quote(NEW.recorded_at)
        || '; NEW.document_ref=' || quote(NEW.document_ref) || '; NEW.comment=' || quote(NEW.comment)
        || '; NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; NEW.event_key=' || quote(NEW.event_key)
        || '; NEW.unit=' || quote(NEW.unit));
END;
