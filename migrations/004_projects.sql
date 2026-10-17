-- Atomic rebuild: database.migrate disables FK enforcement only outside this
-- transaction, checks every FK before COMMIT, then restores enforcement.
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200 AND instr(name,char(0))=0),
    code TEXT NOT NULL UNIQUE CHECK(length(trim(code)) BETWEEN 1 AND 80 AND instr(code,char(0))=0),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO projects(id,name,code,created_at,updated_at)
VALUES (1,'Строительный проект','DEFAULT',strftime('%Y-%m-%dT%H:%M:%f','now')||'000Z',strftime('%Y-%m-%dT%H:%M:%f','now')||'000Z');
CREATE TABLE buildings_new (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
    code TEXT NOT NULL CHECK(length(trim(code))>0),
    name TEXT NOT NULL CHECK(length(trim(name))>0),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK(typeof(sort_order)='integer'),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    UNIQUE(project_id,code)
);
INSERT INTO buildings_new(id,project_id,code,name,sort_order) SELECT id,1,code,name,id FROM buildings;
DROP TABLE buildings;
ALTER TABLE buildings_new RENAME TO buildings;

CREATE TABLE plan_versions_new (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
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
    UNIQUE(project_id,discipline,version_no)
);
INSERT INTO plan_versions_new(id,project_id,discipline,version_no,status,approved_at,created_at)
SELECT id,1,discipline,version_no,status,approved_at,created_at FROM plan_versions;
DROP TABLE plan_versions;
ALTER TABLE plan_versions_new RENAME TO plan_versions;
CREATE INDEX idx_plan_active ON plan_versions(project_id,discipline,status,approved_at,version_no);
CREATE INDEX idx_building_project ON buildings(project_id,sort_order,id);
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
WHEN NEW.project_id!=OLD.project_id OR NEW.id!=OLD.id OR NEW.discipline!=OLD.discipline OR NEW.version_no!=OLD.version_no
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
    SELECT CASE WHEN EXISTS(SELECT 1 FROM plan_versions WHERE project_id=NEW.project_id AND discipline=NEW.discipline
        AND status='APPROVED' AND (approved_at>NEW.approved_at OR version_no>NEW.version_no))
        THEN RAISE(ABORT,'Approval must follow version order and chronology') END;
END;
CREATE TRIGGER buildings_stable BEFORE UPDATE ON buildings
WHEN NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id
BEGIN SELECT RAISE(ABORT,'Building identity is immutable'); END;
CREATE TRIGGER audit_buildings_insert AFTER INSERT ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order) || '; ' || 'NEW.status=' || quote(NEW.status));
END;
CREATE TRIGGER audit_buildings_update AFTER UPDATE ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.code=' || quote(OLD.code) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order) || '; ' || 'NEW.status=' || quote(NEW.status));
END;
CREATE TRIGGER audit_buildings_delete AFTER DELETE ON buildings
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('buildings',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.code=' || quote(OLD.code) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order) || '; ' || 'OLD.status=' || quote(OLD.status));
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
CREATE TRIGGER versions_no_replace BEFORE INSERT ON plan_versions
WHEN EXISTS(SELECT 1 FROM plan_versions WHERE id=NEW.id
    OR (project_id=NEW.project_id AND discipline=NEW.discipline AND version_no=NEW.version_no))
BEGIN
    SELECT RAISE(ABORT,'Plan version identity already exists');
END;
CREATE TRIGGER buildings_no_replace BEFORE INSERT ON buildings
WHEN EXISTS(SELECT 1 FROM buildings WHERE id=NEW.id OR (project_id=NEW.project_id AND code=NEW.code))
BEGIN
    SELECT RAISE(ABORT,'Building identity already exists');
END;

CREATE TRIGGER plan_line_project_insert BEFORE INSERT ON plan_lines
WHEN (SELECT b.project_id FROM work_items w JOIN buildings b ON b.id=w.building_id WHERE w.id=NEW.work_item_id)
    !=(SELECT project_id FROM plan_versions WHERE id=NEW.plan_version_id)
BEGIN SELECT RAISE(ABORT,'Позиция и версия относятся к разным объектам'); END;
CREATE TRIGGER plan_line_project_update BEFORE UPDATE ON plan_lines
WHEN (SELECT b.project_id FROM work_items w JOIN buildings b ON b.id=w.building_id WHERE w.id=NEW.work_item_id)
    !=(SELECT project_id FROM plan_versions WHERE id=NEW.plan_version_id)
BEGIN SELECT RAISE(ABORT,'Позиция и версия относятся к разным объектам'); END;
CREATE TRIGGER projects_no_replace BEFORE INSERT ON projects
WHEN EXISTS(SELECT 1 FROM projects WHERE id=NEW.id OR code=NEW.code)
BEGIN SELECT RAISE(ABORT,'Объект с таким кодом уже существует'); END;
CREATE TRIGGER projects_no_delete BEFORE DELETE ON projects
BEGIN SELECT RAISE(ABORT,'Используйте архивирование объекта'); END;
CREATE TRIGGER projects_identity BEFORE UPDATE ON projects
WHEN NEW.id!=OLD.id OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'Идентификатор объекта неизменяем'); END;
CREATE TRIGGER audit_projects_insert AFTER INSERT ON projects
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('projects',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.name=' || quote(NEW.name) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.description=' || quote(NEW.description) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at));
END;
CREATE TRIGGER audit_projects_update AFTER UPDATE ON projects
BEGIN
    INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details)
    VALUES ('projects',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.name=' || quote(OLD.name) || '; ' || 'OLD.code=' || quote(OLD.code) || '; ' || 'OLD.description=' || quote(OLD.description) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.code=' || quote(NEW.code) || '; ' || 'NEW.description=' || quote(NEW.description) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at));
END;
