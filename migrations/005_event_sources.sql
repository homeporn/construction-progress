-- Add provenance without replacing events, quantities, timestamps or identities.
-- Old sources were not recorded; do not invent a manual origin for historical rows.
ALTER TABLE fact_events ADD COLUMN source_type TEXT NOT NULL DEFAULT 'legacy'
    CHECK(typeof(source_type)='text' AND length(source_type) BETWEEN 1 AND 80
          AND source_type NOT GLOB '*[^a-z0-9_]*');
ALTER TABLE confirmation_events ADD COLUMN source_type TEXT NOT NULL DEFAULT 'legacy'
    CHECK(typeof(source_type)='text' AND length(source_type) BETWEEN 1 AND 80
          AND source_type NOT GLOB '*[^a-z0-9_]*');
DROP TRIGGER audit_fact_events_insert;
CREATE TRIGGER audit_fact_events_insert AFTER INSERT ON fact_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('fact_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z',
        'NEW.work_item_id=' || quote(NEW.work_item_id) || '; NEW.quantity_delta=' || quote(NEW.quantity_delta)
        || '; NEW.work_date=' || quote(NEW.work_date) || '; NEW.recorded_at=' || quote(NEW.recorded_at)
        || '; NEW.comment=' || quote(NEW.comment) || '; NEW.correction_of_id=' || quote(NEW.correction_of_id)
        || '; NEW.event_key=' || quote(NEW.event_key) || '; NEW.unit=' || quote(NEW.unit)
        || '; NEW.source_type=' || quote(NEW.source_type));
END;
DROP TRIGGER audit_confirmation_events_insert;
CREATE TRIGGER audit_confirmation_events_insert AFTER INSERT ON confirmation_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('confirmation_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z',
        'NEW.work_item_id=' || quote(NEW.work_item_id) || '; NEW.quantity_delta=' || quote(NEW.quantity_delta)
        || '; NEW.confirmed_date=' || quote(NEW.confirmed_date) || '; NEW.recorded_at=' || quote(NEW.recorded_at)
        || '; NEW.document_ref=' || quote(NEW.document_ref) || '; NEW.comment=' || quote(NEW.comment)
        || '; NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; NEW.event_key=' || quote(NEW.event_key)
        || '; NEW.unit=' || quote(NEW.unit) || '; NEW.source_type=' || quote(NEW.source_type));
END;
