-- Portable SQLite >=3.24; canonical decimal TEXT, never REAL or SQL SUM(TEXT).
-- UTC timestamps have fixed microsecond precision; business dates are ISO calendars.
CREATE TABLE buildings (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL CHECK(length(trim(code))>0) UNIQUE,
    name TEXT NOT NULL CHECK(length(trim(name))>0)
);
CREATE TABLE floors (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    floor_code TEXT NOT NULL CHECK(length(trim(floor_code))>0),
    sort_order INTEGER NOT NULL CHECK(typeof(sort_order)='integer'),
    UNIQUE(building_id,floor_code),
    UNIQUE(id,building_id)
);
CREATE TABLE work_catalog (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL CHECK(length(trim(code))>0) UNIQUE,
    work_type TEXT NOT NULL CHECK(length(trim(work_type))>0),
    element TEXT NOT NULL CHECK(length(trim(element))>0),
    name TEXT NOT NULL CHECK(length(trim(name))>0),
    unit TEXT NOT NULL CHECK(length(trim(unit))>0)
);
CREATE TABLE work_items (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    floor_id INTEGER NOT NULL,
    catalog_id INTEGER NOT NULL REFERENCES work_catalog(id),
    discipline TEXT NOT NULL CHECK(discipline IN ('DESIGN','CONSTRUCTION')),
    FOREIGN KEY(floor_id,building_id) REFERENCES floors(id,building_id),
    UNIQUE(building_id,floor_id,catalog_id,discipline)
);
CREATE TABLE plan_versions (
    id INTEGER PRIMARY KEY,
    discipline TEXT NOT NULL CHECK(discipline IN ('DESIGN','CONSTRUCTION')),
    version_no INTEGER NOT NULL CHECK(typeof(version_no)='integer' AND version_no>0),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED')),
    approved_at TEXT CHECK(approved_at IS NULL OR (typeof(approved_at)='text' AND length(approved_at)=27
        AND substr(approved_at,11,1)='T' AND substr(approved_at,20,1)='.' AND substr(approved_at,27,1)='Z'
        AND substr(approved_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        AND substr(approved_at,21,6) NOT GLOB '*[^0-9]*'
        AND substr(approved_at,12,2) BETWEEN '00' AND '23'
        AND substr(approved_at,15,2) BETWEEN '00' AND '59'
        AND substr(approved_at,18,2) BETWEEN '00' AND '59'
        AND (typeof(substr(approved_at,1,10))='text' AND length(substr(approved_at,1,10))=10
        AND substr(approved_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(substr(approved_at,1,10),1,4)>='0001'
        AND date(substr(approved_at,1,10),'+0 days') IS NOT NULL AND date(substr(approved_at,1,10),'+0 days')=substr(approved_at,1,10)))),
    created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)=27
        AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='.' AND substr(created_at,27,1)='Z'
        AND substr(created_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        AND substr(created_at,21,6) NOT GLOB '*[^0-9]*'
        AND substr(created_at,12,2) BETWEEN '00' AND '23'
        AND substr(created_at,15,2) BETWEEN '00' AND '59'
        AND substr(created_at,18,2) BETWEEN '00' AND '59'
        AND (typeof(substr(created_at,1,10))='text' AND length(substr(created_at,1,10))=10
        AND substr(created_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(substr(created_at,1,10),1,4)>='0001'
        AND date(substr(created_at,1,10),'+0 days') IS NOT NULL AND date(substr(created_at,1,10),'+0 days')=substr(created_at,1,10))),
    CHECK((status='DRAFT' AND approved_at IS NULL) OR
          (status='APPROVED' AND approved_at IS NOT NULL AND approved_at>=created_at)),
    UNIQUE(discipline,version_no)
);
CREATE TABLE plan_lines (
    id INTEGER PRIMARY KEY,
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
    work_item_id INTEGER NOT NULL REFERENCES work_items(id),
    planned_quantity TEXT NOT NULL CHECK(typeof(planned_quantity)='text' AND instr(planned_quantity,char(0))=0 AND length(planned_quantity) BETWEEN 1 AND 28
    AND planned_quantity NOT GLOB '*[^0-9.]*'
    AND length(planned_quantity)-length(replace(planned_quantity,'.','')) <= 1
    AND length((CASE WHEN instr(planned_quantity,'.')=0 THEN planned_quantity ELSE substr(planned_quantity,1,instr(planned_quantity,'.')-1) END)) BETWEEN 1 AND 18
    AND ((CASE WHEN instr(planned_quantity,'.')=0 THEN planned_quantity ELSE substr(planned_quantity,1,instr(planned_quantity,'.')-1) END)='0' OR substr((CASE WHEN instr(planned_quantity,'.')=0 THEN planned_quantity ELSE substr(planned_quantity,1,instr(planned_quantity,'.')-1) END),1,1) BETWEEN '1' AND '9')
    AND (instr(planned_quantity,'.')=0 OR (length(planned_quantity)-instr(planned_quantity,'.') BETWEEN 1 AND 9 AND substr(planned_quantity,-1) BETWEEN '1' AND '9'))
    AND planned_quantity != '-0'),
    labor_norm TEXT CHECK(labor_norm IS NULL OR (typeof(labor_norm)='text' AND instr(labor_norm,char(0))=0 AND length(labor_norm) BETWEEN 1 AND 28
    AND labor_norm NOT GLOB '*[^0-9.]*'
    AND length(labor_norm)-length(replace(labor_norm,'.','')) <= 1
    AND length((CASE WHEN instr(labor_norm,'.')=0 THEN labor_norm ELSE substr(labor_norm,1,instr(labor_norm,'.')-1) END)) BETWEEN 1 AND 18
    AND ((CASE WHEN instr(labor_norm,'.')=0 THEN labor_norm ELSE substr(labor_norm,1,instr(labor_norm,'.')-1) END)='0' OR substr((CASE WHEN instr(labor_norm,'.')=0 THEN labor_norm ELSE substr(labor_norm,1,instr(labor_norm,'.')-1) END),1,1) BETWEEN '1' AND '9')
    AND (instr(labor_norm,'.')=0 OR (length(labor_norm)-instr(labor_norm,'.') BETWEEN 1 AND 9 AND substr(labor_norm,-1) BETWEEN '1' AND '9'))
    AND labor_norm != '-0')),
    unit TEXT NOT NULL CHECK(length(trim(unit))>0),
    UNIQUE(plan_version_id,work_item_id)
);
CREATE TABLE fact_events (
    id INTEGER PRIMARY KEY,
    work_item_id INTEGER NOT NULL REFERENCES work_items(id),
    quantity_delta TEXT NOT NULL CHECK((typeof(quantity_delta)='text' AND instr(quantity_delta,char(0))=0 AND length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END)) BETWEEN 1 AND 28
    AND (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) NOT GLOB '*[^0-9.]*'
    AND length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END))-length(replace((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.','')) <= 1
    AND length((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END)) BETWEEN 1 AND 18
    AND ((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END)='0' OR substr((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END),1,1) BETWEEN '1' AND '9')
    AND (instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 OR (length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END))-instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.') BETWEEN 1 AND 9 AND substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),-1) BETWEEN '1' AND '9'))
    AND quantity_delta != '-0') AND quantity_delta!='0'),
    work_date TEXT NOT NULL CHECK(typeof(work_date)='text' AND length(work_date)=10
        AND work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(work_date,1,4)>='0001'
        AND date(work_date,'+0 days') IS NOT NULL AND date(work_date,'+0 days')=work_date),
    recorded_at TEXT NOT NULL CHECK(typeof(recorded_at)='text' AND length(recorded_at)=27
        AND substr(recorded_at,11,1)='T' AND substr(recorded_at,20,1)='.' AND substr(recorded_at,27,1)='Z'
        AND substr(recorded_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        AND substr(recorded_at,21,6) NOT GLOB '*[^0-9]*'
        AND substr(recorded_at,12,2) BETWEEN '00' AND '23'
        AND substr(recorded_at,15,2) BETWEEN '00' AND '59'
        AND substr(recorded_at,18,2) BETWEEN '00' AND '59'
        AND (typeof(substr(recorded_at,1,10))='text' AND length(substr(recorded_at,1,10))=10
        AND substr(recorded_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(substr(recorded_at,1,10),1,4)>='0001'
        AND date(substr(recorded_at,1,10),'+0 days') IS NOT NULL AND date(substr(recorded_at,1,10),'+0 days')=substr(recorded_at,1,10))),
    comment TEXT NOT NULL DEFAULT '',
    correction_of_id INTEGER REFERENCES fact_events(id),
    event_key TEXT NOT NULL CHECK(length(trim(event_key))>0) UNIQUE,
    unit TEXT NOT NULL CHECK(length(trim(unit))>0),
    CHECK(work_date<=substr(recorded_at,1,10)),
    CHECK(substr(quantity_delta,1,1)!='-' OR correction_of_id IS NOT NULL),
    CHECK(correction_of_id IS NULL OR correction_of_id!=id)
    
);
CREATE INDEX idx_fact_events_slice ON fact_events(work_item_id,work_date,recorded_at);
CREATE TRIGGER fact_events_correction BEFORE INSERT ON fact_events
WHEN NEW.correction_of_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM fact_events p WHERE p.id=NEW.correction_of_id
        AND p.work_item_id=NEW.work_item_id AND p.unit=NEW.unit
        AND p.recorded_at<=NEW.recorded_at AND p.work_date<=NEW.work_date
    ) THEN RAISE(ABORT,'Invalid correction reference or chronology') END;
END;
CREATE TRIGGER fact_events_unit BEFORE INSERT ON fact_events
BEGIN
    SELECT CASE WHEN NEW.unit != (SELECT c.unit FROM work_items w JOIN work_catalog c
        ON c.id=w.catalog_id WHERE w.id=NEW.work_item_id)
        THEN RAISE(ABORT,'Incompatible event unit') END;
END;
CREATE TRIGGER fact_events_no_update BEFORE UPDATE ON fact_events
BEGIN
    SELECT RAISE(ABORT,'Events are append-only; use a correction');
END;
CREATE TRIGGER fact_events_no_delete BEFORE DELETE ON fact_events
BEGIN
    SELECT RAISE(ABORT,'Events are append-only; use a correction');
END;
CREATE TABLE confirmation_events (
    id INTEGER PRIMARY KEY,
    work_item_id INTEGER NOT NULL REFERENCES work_items(id),
    quantity_delta TEXT NOT NULL CHECK((typeof(quantity_delta)='text' AND instr(quantity_delta,char(0))=0 AND length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END)) BETWEEN 1 AND 28
    AND (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) NOT GLOB '*[^0-9.]*'
    AND length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END))-length(replace((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.','')) <= 1
    AND length((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END)) BETWEEN 1 AND 18
    AND ((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END)='0' OR substr((CASE WHEN instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 THEN (CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END) ELSE substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),1,instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')-1) END),1,1) BETWEEN '1' AND '9')
    AND (instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.')=0 OR (length((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END))-instr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),'.') BETWEEN 1 AND 9 AND substr((CASE WHEN substr(quantity_delta,1,1)='-' THEN substr(quantity_delta,2) ELSE quantity_delta END),-1) BETWEEN '1' AND '9'))
    AND quantity_delta != '-0') AND quantity_delta!='0'),
    confirmed_date TEXT NOT NULL CHECK(typeof(confirmed_date)='text' AND length(confirmed_date)=10
        AND confirmed_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(confirmed_date,1,4)>='0001'
        AND date(confirmed_date,'+0 days') IS NOT NULL AND date(confirmed_date,'+0 days')=confirmed_date),
    recorded_at TEXT NOT NULL CHECK(typeof(recorded_at)='text' AND length(recorded_at)=27
        AND substr(recorded_at,11,1)='T' AND substr(recorded_at,20,1)='.' AND substr(recorded_at,27,1)='Z'
        AND substr(recorded_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        AND substr(recorded_at,21,6) NOT GLOB '*[^0-9]*'
        AND substr(recorded_at,12,2) BETWEEN '00' AND '23'
        AND substr(recorded_at,15,2) BETWEEN '00' AND '59'
        AND substr(recorded_at,18,2) BETWEEN '00' AND '59'
        AND (typeof(substr(recorded_at,1,10))='text' AND length(substr(recorded_at,1,10))=10
        AND substr(recorded_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND substr(substr(recorded_at,1,10),1,4)>='0001'
        AND date(substr(recorded_at,1,10),'+0 days') IS NOT NULL AND date(substr(recorded_at,1,10),'+0 days')=substr(recorded_at,1,10))),
    document_ref TEXT NOT NULL,
    correction_of_id INTEGER REFERENCES confirmation_events(id),
    event_key TEXT NOT NULL CHECK(length(trim(event_key))>0) UNIQUE,
    unit TEXT NOT NULL CHECK(length(trim(unit))>0),
    CHECK(confirmed_date<=substr(recorded_at,1,10)),
    CHECK(substr(quantity_delta,1,1)!='-' OR correction_of_id IS NOT NULL),
    CHECK(correction_of_id IS NULL OR correction_of_id!=id)
    , CHECK(length(trim(document_ref))>0)
);
CREATE INDEX idx_confirmation_events_slice ON confirmation_events(work_item_id,confirmed_date,recorded_at);
CREATE TRIGGER confirmation_events_correction BEFORE INSERT ON confirmation_events
WHEN NEW.correction_of_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM confirmation_events p WHERE p.id=NEW.correction_of_id
        AND p.work_item_id=NEW.work_item_id AND p.unit=NEW.unit
        AND p.recorded_at<=NEW.recorded_at AND p.confirmed_date<=NEW.confirmed_date
    ) THEN RAISE(ABORT,'Invalid correction reference or chronology') END;
END;
CREATE TRIGGER confirmation_events_unit BEFORE INSERT ON confirmation_events
BEGIN
    SELECT CASE WHEN NEW.unit != (SELECT c.unit FROM work_items w JOIN work_catalog c
        ON c.id=w.catalog_id WHERE w.id=NEW.work_item_id)
        THEN RAISE(ABORT,'Incompatible event unit') END;
END;
CREATE TRIGGER confirmation_events_no_update BEFORE UPDATE ON confirmation_events
BEGIN
    SELECT RAISE(ABORT,'Events are append-only; use a correction');
END;
CREATE TRIGGER confirmation_events_no_delete BEFORE DELETE ON confirmation_events
BEGIN
    SELECT RAISE(ABORT,'Events are append-only; use a correction');
END;
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('INSERT','UPDATE','DELETE')),
    recorded_at TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT,'Audit is append-only');
END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT,'Audit is append-only');
END;
CREATE TRIGGER plan_must_start_draft BEFORE INSERT ON plan_versions
WHEN NEW.status!='DRAFT'
BEGIN
    SELECT RAISE(ABORT,'Create a draft before approval');
END;
CREATE TRIGGER approved_plan_no_update BEFORE UPDATE ON plan_versions
WHEN OLD.status='APPROVED'
BEGIN
    SELECT RAISE(ABORT,'Approved plan is immutable');
END;
CREATE TRIGGER approved_plan_no_delete BEFORE DELETE ON plan_versions
WHEN OLD.status='APPROVED'
BEGIN
    SELECT RAISE(ABORT,'Approved plan is immutable');
END;
CREATE TRIGGER plan_identity_immutable BEFORE UPDATE ON plan_versions
WHEN NEW.id!=OLD.id OR NEW.discipline!=OLD.discipline OR NEW.version_no!=OLD.version_no
    OR NEW.created_at!=OLD.created_at
BEGIN
    SELECT RAISE(ABORT,'Plan identity is immutable');
END;
CREATE TRIGGER approval_validation BEFORE UPDATE ON plan_versions
WHEN NEW.status='APPROVED'
BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM plan_lines WHERE plan_version_id=OLD.id)
        THEN RAISE(ABORT,'Cannot approve an empty plan') END;
    SELECT CASE WHEN EXISTS(SELECT 1 FROM plan_lines WHERE plan_version_id=OLD.id
        AND (planned_quantity='0' OR labor_norm IS NULL OR labor_norm='0'))
        THEN RAISE(ABORT,'Approval requires positive quantities and labor norms') END;
    SELECT CASE WHEN EXISTS(SELECT 1 FROM plan_versions WHERE discipline=NEW.discipline
        AND status='APPROVED' AND (approved_at>NEW.approved_at OR version_no>NEW.version_no))
        THEN RAISE(ABORT,'Approval must follow version order and chronology') END;
END;
CREATE INDEX idx_plan_active ON plan_versions(discipline,status,approved_at,version_no);
CREATE TRIGGER plan_line_insert_guard BEFORE INSERT ON plan_lines
WHEN (SELECT status FROM plan_versions WHERE id=NEW.plan_version_id)!='DRAFT'
BEGIN
    SELECT RAISE(ABORT,'Approved plan lines are immutable');
END;
CREATE TRIGGER plan_line_insert_consistency BEFORE INSERT ON plan_lines
BEGIN
    SELECT CASE WHEN (SELECT discipline FROM work_items WHERE id=NEW.work_item_id)
        !=(SELECT discipline FROM plan_versions WHERE id=NEW.plan_version_id)
        THEN RAISE(ABORT,'Plan discipline mismatch') END;
    SELECT CASE WHEN NEW.unit!=(SELECT c.unit FROM work_items w JOIN work_catalog c
        ON w.catalog_id=c.id WHERE w.id=NEW.work_item_id)
        THEN RAISE(ABORT,'Plan unit mismatch') END;
END;
CREATE TRIGGER plan_line_update_guard BEFORE UPDATE ON plan_lines
WHEN (SELECT status FROM plan_versions WHERE id=NEW.plan_version_id)!='DRAFT' OR (SELECT status FROM plan_versions WHERE id=OLD.plan_version_id)!='DRAFT'
BEGIN
    SELECT RAISE(ABORT,'Approved plan lines are immutable');
END;
CREATE TRIGGER plan_line_update_consistency BEFORE UPDATE ON plan_lines
BEGIN
    SELECT CASE WHEN (SELECT discipline FROM work_items WHERE id=NEW.work_item_id)
        !=(SELECT discipline FROM plan_versions WHERE id=NEW.plan_version_id)
        THEN RAISE(ABORT,'Plan discipline mismatch') END;
    SELECT CASE WHEN NEW.unit!=(SELECT c.unit FROM work_items w JOIN work_catalog c
        ON w.catalog_id=c.id WHERE w.id=NEW.work_item_id)
        THEN RAISE(ABORT,'Plan unit mismatch') END;
END;
CREATE TRIGGER plan_line_delete_guard BEFORE DELETE ON plan_lines
WHEN (SELECT status FROM plan_versions WHERE id=OLD.plan_version_id)!='DRAFT'
BEGIN
    SELECT RAISE(ABORT,'Approved plan lines are immutable');
END;
CREATE TRIGGER work_items_stable BEFORE UPDATE ON work_items
WHEN EXISTS(SELECT 1 FROM plan_lines WHERE work_item_id=OLD.id) OR EXISTS(SELECT 1 FROM fact_events WHERE work_item_id=OLD.id) OR EXISTS(SELECT 1 FROM confirmation_events WHERE work_item_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT,'Referenced classification is immutable; create a new entry');
END;
CREATE TRIGGER work_catalog_stable BEFORE UPDATE ON work_catalog
WHEN EXISTS(SELECT 1 FROM work_items WHERE catalog_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT,'Referenced classification is immutable; create a new entry');
END;
CREATE TRIGGER floors_stable BEFORE UPDATE ON floors
WHEN EXISTS(SELECT 1 FROM work_items WHERE floor_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT,'Referenced classification is immutable; create a new entry');
END;
CREATE TRIGGER buildings_stable BEFORE UPDATE ON buildings
WHEN EXISTS(SELECT 1 FROM work_items WHERE building_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT,'Referenced classification is immutable; create a new entry');
END;
CREATE TRIGGER audit_buildings_insert AFTER INSERT ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.code=' || quote(NEW.code) || '; ' || 'NEW.name=' || quote(NEW.name));
END;
CREATE TRIGGER audit_buildings_update AFTER UPDATE ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.code=' || quote(OLD.code) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.name=' || quote(NEW.name));
END;
CREATE TRIGGER audit_buildings_delete AFTER DELETE ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.code=' || quote(OLD.code) || '; ' || 'OLD.name=' || quote(OLD.name));
END;
CREATE TRIGGER audit_floors_insert AFTER INSERT ON floors
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('floors',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.building_id=' || quote(NEW.building_id) || '; ' || 'NEW.floor_code=' || quote(NEW.floor_code) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order));
END;
CREATE TRIGGER audit_floors_update AFTER UPDATE ON floors
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('floors',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.building_id=' || quote(OLD.building_id) || '; ' || 'OLD.floor_code=' || quote(OLD.floor_code) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order) || '; ' || 'NEW.building_id=' || quote(NEW.building_id) || '; ' || 'NEW.floor_code=' || quote(NEW.floor_code) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order));
END;
CREATE TRIGGER audit_floors_delete AFTER DELETE ON floors
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('floors',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.building_id=' || quote(OLD.building_id) || '; ' || 'OLD.floor_code=' || quote(OLD.floor_code) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order));
END;
CREATE TRIGGER audit_work_catalog_insert AFTER INSERT ON work_catalog
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_catalog',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.code=' || quote(NEW.code) || '; ' || 'NEW.work_type=' || quote(NEW.work_type) || '; ' || 'NEW.element=' || quote(NEW.element) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_work_catalog_update AFTER UPDATE ON work_catalog
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_catalog',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.code=' || quote(OLD.code) || '; ' || 'OLD.work_type=' || quote(OLD.work_type) || '; ' || 'OLD.element=' || quote(OLD.element) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.unit=' || quote(OLD.unit) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.work_type=' || quote(NEW.work_type) || '; ' || 'NEW.element=' || quote(NEW.element) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_work_catalog_delete AFTER DELETE ON work_catalog
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_catalog',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.code=' || quote(OLD.code) || '; ' || 'OLD.work_type=' || quote(OLD.work_type) || '; ' || 'OLD.element=' || quote(OLD.element) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.unit=' || quote(OLD.unit));
END;
CREATE TRIGGER audit_work_items_insert AFTER INSERT ON work_items
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_items',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.building_id=' || quote(NEW.building_id) || '; ' || 'NEW.floor_id=' || quote(NEW.floor_id) || '; ' || 'NEW.catalog_id=' || quote(NEW.catalog_id) || '; ' || 'NEW.discipline=' || quote(NEW.discipline));
END;
CREATE TRIGGER audit_work_items_update AFTER UPDATE ON work_items
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_items',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.building_id=' || quote(OLD.building_id) || '; ' || 'OLD.floor_id=' || quote(OLD.floor_id) || '; ' || 'OLD.catalog_id=' || quote(OLD.catalog_id) || '; ' || 'OLD.discipline=' || quote(OLD.discipline) || '; ' || 'NEW.building_id=' || quote(NEW.building_id) || '; ' || 'NEW.floor_id=' || quote(NEW.floor_id) || '; ' || 'NEW.catalog_id=' || quote(NEW.catalog_id) || '; ' || 'NEW.discipline=' || quote(NEW.discipline));
END;
CREATE TRIGGER audit_work_items_delete AFTER DELETE ON work_items
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('work_items',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.building_id=' || quote(OLD.building_id) || '; ' || 'OLD.floor_id=' || quote(OLD.floor_id) || '; ' || 'OLD.catalog_id=' || quote(OLD.catalog_id) || '; ' || 'OLD.discipline=' || quote(OLD.discipline));
END;
CREATE TRIGGER audit_plan_versions_insert AFTER INSERT ON plan_versions
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_versions',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.discipline=' || quote(NEW.discipline) || '; ' || 'NEW.version_no=' || quote(NEW.version_no) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.approved_at=' || quote(NEW.approved_at) || '; ' || 'NEW.created_at=' || quote(NEW.created_at));
END;
CREATE TRIGGER audit_plan_versions_update AFTER UPDATE ON plan_versions
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_versions',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.discipline=' || quote(OLD.discipline) || '; ' || 'OLD.version_no=' || quote(OLD.version_no) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.approved_at=' || quote(OLD.approved_at) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'NEW.discipline=' || quote(NEW.discipline) || '; ' || 'NEW.version_no=' || quote(NEW.version_no) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.approved_at=' || quote(NEW.approved_at) || '; ' || 'NEW.created_at=' || quote(NEW.created_at));
END;
CREATE TRIGGER audit_plan_versions_delete AFTER DELETE ON plan_versions
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_versions',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.discipline=' || quote(OLD.discipline) || '; ' || 'OLD.version_no=' || quote(OLD.version_no) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.approved_at=' || quote(OLD.approved_at) || '; ' || 'OLD.created_at=' || quote(OLD.created_at));
END;
CREATE TRIGGER audit_plan_lines_insert AFTER INSERT ON plan_lines
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_lines',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.plan_version_id=' || quote(NEW.plan_version_id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.planned_quantity=' || quote(NEW.planned_quantity) || '; ' || 'NEW.labor_norm=' || quote(NEW.labor_norm) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_plan_lines_update AFTER UPDATE ON plan_lines
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_lines',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.plan_version_id=' || quote(OLD.plan_version_id) || '; ' || 'OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.planned_quantity=' || quote(OLD.planned_quantity) || '; ' || 'OLD.labor_norm=' || quote(OLD.labor_norm) || '; ' || 'OLD.unit=' || quote(OLD.unit) || '; ' || 'NEW.plan_version_id=' || quote(NEW.plan_version_id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.planned_quantity=' || quote(NEW.planned_quantity) || '; ' || 'NEW.labor_norm=' || quote(NEW.labor_norm) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_plan_lines_delete AFTER DELETE ON plan_lines
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('plan_lines',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.plan_version_id=' || quote(OLD.plan_version_id) || '; ' || 'OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.planned_quantity=' || quote(OLD.planned_quantity) || '; ' || 'OLD.labor_norm=' || quote(OLD.labor_norm) || '; ' || 'OLD.unit=' || quote(OLD.unit));
END;
CREATE TRIGGER audit_fact_events_insert AFTER INSERT ON fact_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('fact_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.work_date=' || quote(NEW.work_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_fact_events_update AFTER UPDATE ON fact_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('fact_events',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.quantity_delta=' || quote(OLD.quantity_delta) || '; ' || 'OLD.work_date=' || quote(OLD.work_date) || '; ' || 'OLD.recorded_at=' || quote(OLD.recorded_at) || '; ' || 'OLD.comment=' || quote(OLD.comment) || '; ' || 'OLD.correction_of_id=' || quote(OLD.correction_of_id) || '; ' || 'OLD.event_key=' || quote(OLD.event_key) || '; ' || 'OLD.unit=' || quote(OLD.unit) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.work_date=' || quote(NEW.work_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_fact_events_delete AFTER DELETE ON fact_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('fact_events',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.quantity_delta=' || quote(OLD.quantity_delta) || '; ' || 'OLD.work_date=' || quote(OLD.work_date) || '; ' || 'OLD.recorded_at=' || quote(OLD.recorded_at) || '; ' || 'OLD.comment=' || quote(OLD.comment) || '; ' || 'OLD.correction_of_id=' || quote(OLD.correction_of_id) || '; ' || 'OLD.event_key=' || quote(OLD.event_key) || '; ' || 'OLD.unit=' || quote(OLD.unit));
END;
CREATE TRIGGER audit_confirmation_events_insert AFTER INSERT ON confirmation_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('confirmation_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.confirmed_date=' || quote(NEW.confirmed_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.document_ref=' || quote(NEW.document_ref) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_confirmation_events_update AFTER UPDATE ON confirmation_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('confirmation_events',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.quantity_delta=' || quote(OLD.quantity_delta) || '; ' || 'OLD.confirmed_date=' || quote(OLD.confirmed_date) || '; ' || 'OLD.recorded_at=' || quote(OLD.recorded_at) || '; ' || 'OLD.document_ref=' || quote(OLD.document_ref) || '; ' || 'OLD.correction_of_id=' || quote(OLD.correction_of_id) || '; ' || 'OLD.event_key=' || quote(OLD.event_key) || '; ' || 'OLD.unit=' || quote(OLD.unit) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.confirmed_date=' || quote(NEW.confirmed_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.document_ref=' || quote(NEW.document_ref) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit));
END;
CREATE TRIGGER audit_confirmation_events_delete AFTER DELETE ON confirmation_events
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('confirmation_events',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.quantity_delta=' || quote(OLD.quantity_delta) || '; ' || 'OLD.confirmed_date=' || quote(OLD.confirmed_date) || '; ' || 'OLD.recorded_at=' || quote(OLD.recorded_at) || '; ' || 'OLD.document_ref=' || quote(OLD.document_ref) || '; ' || 'OLD.correction_of_id=' || quote(OLD.correction_of_id) || '; ' || 'OLD.event_key=' || quote(OLD.event_key) || '; ' || 'OLD.unit=' || quote(OLD.unit));
END;
