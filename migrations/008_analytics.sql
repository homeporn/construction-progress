-- Analytics are views over the existing plan and event journals.
ALTER TABLE work_catalog ADD COLUMN reference_labor_norm TEXT CHECK(reference_labor_norm IS NULL OR (typeof(reference_labor_norm)='text' AND instr(reference_labor_norm,char(0))=0 AND length(reference_labor_norm) BETWEEN 1 AND 28
    AND reference_labor_norm NOT GLOB '*[^0-9.]*'
    AND length(reference_labor_norm)-length(replace(reference_labor_norm,'.','')) <= 1
    AND length((CASE WHEN instr(reference_labor_norm,'.')=0 THEN reference_labor_norm ELSE substr(reference_labor_norm,1,instr(reference_labor_norm,'.')-1) END)) BETWEEN 1 AND 18
    AND ((CASE WHEN instr(reference_labor_norm,'.')=0 THEN reference_labor_norm ELSE substr(reference_labor_norm,1,instr(reference_labor_norm,'.')-1) END)='0' OR substr((CASE WHEN instr(reference_labor_norm,'.')=0 THEN reference_labor_norm ELSE substr(reference_labor_norm,1,instr(reference_labor_norm,'.')-1) END),1,1) BETWEEN '1' AND '9')
    AND (instr(reference_labor_norm,'.')=0 OR (length(reference_labor_norm)-instr(reference_labor_norm,'.') BETWEEN 1 AND 9 AND substr(reference_labor_norm,-1) BETWEEN '1' AND '9'))
    AND reference_labor_norm != '-0'));
DROP TRIGGER work_catalog_stable;
CREATE TRIGGER work_catalog_stable BEFORE UPDATE ON work_catalog
WHEN EXISTS(SELECT 1 FROM work_items WHERE catalog_id=OLD.id) AND
 (NEW.id IS NOT OLD.id OR NEW.code IS NOT OLD.code OR NEW.name IS NOT OLD.name OR NEW.unit IS NOT OLD.unit
  OR NEW.element IS NOT OLD.element OR NEW.work_type IS NOT OLD.work_type)
BEGIN SELECT RAISE(ABORT,'Referenced classification is immutable; create a new entry'); END;
CREATE TRIGGER catalog_reference_positive BEFORE UPDATE OF reference_labor_norm ON work_catalog
WHEN NEW.reference_labor_norm='0' BEGIN SELECT RAISE(ABORT,'Справочный норматив должен быть положительным'); END;
CREATE TRIGGER catalog_reference_positive_insert BEFORE INSERT ON work_catalog
WHEN NEW.reference_labor_norm='0' BEGIN SELECT RAISE(ABORT,'Справочный норматив должен быть положительным'); END;
CREATE TABLE analytical_groups (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 created_at TEXT NOT NULL, create_key TEXT NOT NULL UNIQUE
);
CREATE TABLE analytical_group_versions (
 id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES analytical_groups(id),
 version_no INTEGER NOT NULL CHECK(version_no>0), name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
 catalog_id INTEGER NOT NULL REFERENCES work_catalog(id), unit TEXT NOT NULL,
 discipline TEXT NOT NULL CHECK(discipline IN ('CONSTRUCTION','DESIGN')),
 base_building_id INTEGER REFERENCES buildings(id), archived INTEGER NOT NULL CHECK(archived IN (0,1)),
 changed_at TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE,
 sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)), UNIQUE(group_id,version_no)
);
CREATE INDEX analytical_group_time ON analytical_group_versions(group_id,changed_at,version_no);
CREATE TABLE analytical_group_members (
 id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES analytical_group_versions(id),
 work_item_id INTEGER NOT NULL REFERENCES work_items(id), operation TEXT NOT NULL CHECK(operation IN ('include','exclude')),
 UNIQUE(version_id,work_item_id)
);
CREATE TRIGGER analytical_version_scope BEFORE INSERT ON analytical_group_versions
BEGIN
 SELECT CASE WHEN NEW.sealed!=0 THEN RAISE(ABORT,'Новая редакция сначала заполняется, затем фиксируется') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM work_catalog c WHERE c.id=NEW.catalog_id AND c.unit=NEW.unit)
  THEN RAISE(ABORT,'Единица группы несовместима с кодом работ') END;
 SELECT CASE WHEN NEW.base_building_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM buildings b JOIN analytical_groups g ON g.project_id=b.project_id
  WHERE g.id=NEW.group_id AND b.id=NEW.base_building_id) THEN RAISE(ABORT,'Корпус группы принадлежит другому объекту') END;
 SELECT CASE WHEN NEW.version_no!=(SELECT COALESCE(MAX(v.version_no),0)+1 FROM analytical_group_versions v WHERE v.group_id=NEW.group_id)
  OR NEW.changed_at<(SELECT g.created_at FROM analytical_groups g WHERE g.id=NEW.group_id)
  OR NEW.changed_at<COALESCE((SELECT MAX(v.changed_at) FROM analytical_group_versions v WHERE v.group_id=NEW.group_id),'')
  THEN RAISE(ABORT,'Нарушена последовательность редакций группы') END;
END;
CREATE TRIGGER analytical_member_scope BEFORE INSERT ON analytical_group_members
WHEN NOT EXISTS(SELECT 1 FROM analytical_group_versions v JOIN analytical_groups g ON g.id=v.group_id
 JOIN work_items w ON w.id=NEW.work_item_id JOIN buildings b ON b.id=w.building_id JOIN work_catalog c ON c.id=w.catalog_id
 WHERE v.id=NEW.version_id AND v.sealed=0 AND g.project_id=b.project_id AND v.catalog_id=w.catalog_id
 AND v.unit=c.unit AND v.discipline=w.discipline)
BEGIN SELECT RAISE(ABORT,'Позиция группы несовместима или принадлежит другому объекту'); END;
CREATE TRIGGER catalog_analytical_identity BEFORE UPDATE OF code,unit ON work_catalog
WHEN (NEW.code!=OLD.code OR NEW.unit!=OLD.unit) AND EXISTS(SELECT 1 FROM analytical_group_versions v WHERE v.catalog_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'Код и единица используются в истории аналитической группы'); END;
CREATE TRIGGER analytical_versions_guard BEFORE UPDATE ON analytical_group_versions WHEN OLD.sealed!=0 OR NEW.sealed!=1 OR NEW.id IS NOT OLD.id OR NEW.group_id IS NOT OLD.group_id OR NEW.version_no IS NOT OLD.version_no OR NEW.name IS NOT OLD.name OR NEW.catalog_id IS NOT OLD.catalog_id OR NEW.unit IS NOT OLD.unit OR NEW.discipline IS NOT OLD.discipline OR NEW.base_building_id IS NOT OLD.base_building_id OR NEW.archived IS NOT OLD.archived OR NEW.changed_at IS NOT OLD.changed_at OR NEW.event_key IS NOT OLD.event_key BEGIN SELECT RAISE(ABORT,'Редакция группы неизменяема'); END;
CREATE TRIGGER analytical_groups_no_update BEFORE UPDATE ON analytical_groups BEGIN SELECT RAISE(ABORT,'История аналитических групп неизменяема'); END;
CREATE TRIGGER analytical_groups_no_delete BEFORE DELETE ON analytical_groups BEGIN SELECT RAISE(ABORT,'История аналитических групп неизменяема'); END;
CREATE TRIGGER audit_analytical_groups_insert AFTER INSERT ON analytical_groups BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('analytical_groups',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','project_id=' || quote(NEW.project_id) || '; ' || 'created_at=' || quote(NEW.created_at) || '; ' || 'create_key=' || quote(NEW.create_key)); END;
CREATE TRIGGER analytical_group_versions_no_delete BEFORE DELETE ON analytical_group_versions BEGIN SELECT RAISE(ABORT,'История аналитических групп неизменяема'); END;
CREATE TRIGGER audit_analytical_group_versions_insert AFTER INSERT ON analytical_group_versions BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('analytical_group_versions',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','group_id=' || quote(NEW.group_id) || '; ' || 'version_no=' || quote(NEW.version_no) || '; ' || 'name=' || quote(NEW.name) || '; ' || 'catalog_id=' || quote(NEW.catalog_id) || '; ' || 'unit=' || quote(NEW.unit) || '; ' || 'discipline=' || quote(NEW.discipline) || '; ' || 'base_building_id=' || quote(NEW.base_building_id) || '; ' || 'archived=' || quote(NEW.archived) || '; ' || 'changed_at=' || quote(NEW.changed_at) || '; ' || 'event_key=' || quote(NEW.event_key)); END;
CREATE TRIGGER analytical_group_members_no_update BEFORE UPDATE ON analytical_group_members BEGIN SELECT RAISE(ABORT,'История аналитических групп неизменяема'); END;
CREATE TRIGGER analytical_group_members_no_delete BEFORE DELETE ON analytical_group_members BEGIN SELECT RAISE(ABORT,'История аналитических групп неизменяема'); END;
CREATE TRIGGER audit_analytical_group_members_insert AFTER INSERT ON analytical_group_members BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('analytical_group_members',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','version_id=' || quote(NEW.version_id) || '; ' || 'work_item_id=' || quote(NEW.work_item_id) || '; ' || 'operation=' || quote(NEW.operation)); END;
CREATE TRIGGER audit_catalog_reference AFTER UPDATE OF reference_labor_norm ON work_catalog WHEN NEW.reference_labor_norm IS NOT OLD.reference_labor_norm BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_catalog',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','reference_labor_norm: ' || quote(OLD.reference_labor_norm) || ' -> ' || quote(NEW.reference_labor_norm)); END;
