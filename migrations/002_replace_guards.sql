-- REPLACE deletes conflicting records before insertion. Guard collisions explicitly,
-- even for another SQLite connection whose recursive_triggers setting is OFF.
CREATE TRIGGER facts_no_replace BEFORE INSERT ON fact_events
WHEN EXISTS(SELECT 1 FROM fact_events WHERE id=NEW.id OR event_key=NEW.event_key)
BEGIN
    SELECT RAISE(ABORT,'Duplicate event or attempted replacement');
END;
CREATE TRIGGER confirmations_no_replace BEFORE INSERT ON confirmation_events
WHEN EXISTS(SELECT 1 FROM confirmation_events WHERE id=NEW.id OR event_key=NEW.event_key)
BEGIN
    SELECT RAISE(ABORT,'Duplicate event or attempted replacement');
END;
CREATE TRIGGER versions_no_replace BEFORE INSERT ON plan_versions
WHEN EXISTS(SELECT 1 FROM plan_versions WHERE id=NEW.id
    OR (discipline=NEW.discipline AND version_no=NEW.version_no))
BEGIN
    SELECT RAISE(ABORT,'Plan version identity already exists');
END;
CREATE TRIGGER lines_no_replace BEFORE INSERT ON plan_lines
WHEN EXISTS(SELECT 1 FROM plan_lines WHERE id=NEW.id
    OR (plan_version_id=NEW.plan_version_id AND work_item_id=NEW.work_item_id))
BEGIN
    SELECT RAISE(ABORT,'Plan line identity already exists; update drafts explicitly');
END;
CREATE TRIGGER buildings_no_replace BEFORE INSERT ON buildings
WHEN EXISTS(SELECT 1 FROM buildings WHERE id=NEW.id OR code=NEW.code)
BEGIN
    SELECT RAISE(ABORT,'Building identity already exists');
END;
CREATE TRIGGER floors_no_replace BEFORE INSERT ON floors
WHEN EXISTS(SELECT 1 FROM floors WHERE id=NEW.id
    OR (building_id=NEW.building_id AND floor_code=NEW.floor_code))
BEGIN
    SELECT RAISE(ABORT,'Floor identity already exists');
END;
CREATE TRIGGER catalog_no_replace BEFORE INSERT ON work_catalog
WHEN EXISTS(SELECT 1 FROM work_catalog WHERE id=NEW.id OR code=NEW.code)
BEGIN
    SELECT RAISE(ABORT,'Catalog identity already exists');
END;
CREATE TRIGGER items_no_replace BEFORE INSERT ON work_items
WHEN EXISTS(SELECT 1 FROM work_items WHERE id=NEW.id OR
    (building_id=NEW.building_id AND floor_id=NEW.floor_id
     AND catalog_id=NEW.catalog_id AND discipline=NEW.discipline))
BEGIN
    SELECT RAISE(ABORT,'Work item identity already exists');
END;
CREATE TRIGGER audit_no_replace BEFORE INSERT ON audit_log
WHEN EXISTS(SELECT 1 FROM audit_log WHERE id=NEW.id)
BEGIN
    SELECT RAISE(ABORT,'Audit is append-only');
END;
