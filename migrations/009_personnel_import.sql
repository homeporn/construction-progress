-- Stage 4.1. Canonical people and employers; project availability is separate.
-- Applied atomically with checked backup, original-column fingerprints and FK check.
CREATE TABLE contractor_projects (
 id INTEGER PRIMARY KEY, contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 project_id INTEGER NOT NULL REFERENCES projects(id), registered_at TEXT,
 UNIQUE(contractor_id,project_id)
);
INSERT INTO contractor_projects(contractor_id,project_id) SELECT id,project_id FROM contractors;
CREATE TABLE employee_projects (
 id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
 project_id INTEGER NOT NULL REFERENCES projects(id), registered_at TEXT,
 UNIQUE(employee_id,project_id)
);
INSERT INTO employee_projects(employee_id,project_id) SELECT id,project_id FROM employees;
CREATE TABLE employee_project_periods (
 id INTEGER PRIMARY KEY, employee_project_id INTEGER NOT NULL REFERENCES employee_projects(id),
 start_date TEXT, end_date TEXT, recorded_at TEXT, closed_at TEXT,
 CHECK(start_date IS NULL OR (length(start_date)=10 AND date(start_date)=start_date)),
 CHECK(end_date IS NULL OR (length(end_date)=10 AND date(end_date)=end_date)),
 CHECK(end_date IS NULL OR start_date IS NULL OR end_date>=start_date),
 CHECK((end_date IS NULL AND closed_at IS NULL) OR (end_date IS NOT NULL AND closed_at IS NOT NULL))
);
INSERT INTO employee_project_periods(employee_project_id) SELECT id FROM employee_projects;


CREATE TABLE employees_41 (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    contractor_id INTEGER, full_name TEXT NOT NULL CHECK(length(trim(full_name)) BETWEEN 1 AND 200),
    specialty_id INTEGER REFERENCES specialties(id), position TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    UNIQUE(id,project_id), FOREIGN KEY(contractor_id) REFERENCES contractors(id)
);

INSERT INTO employees_41("id","project_id","contractor_id","full_name","specialty_id","position","is_active") SELECT "id","project_id","contractor_id","full_name","specialty_id","position","is_active" FROM employees;

DROP TABLE employees;

ALTER TABLE employees_41 RENAME TO employees;

ALTER TABLE employees ADD COLUMN created_at TEXT;

ALTER TABLE employees ADD COLUMN updated_at TEXT;

ALTER TABLE employees ADD COLUMN needs_details INTEGER NOT NULL DEFAULT 0 CHECK(needs_details IN (0,1));

UPDATE employees SET needs_details=1 WHERE contractor_id IS NULL OR specialty_id IS NULL;

CREATE TRIGGER audit_employees_delete AFTER DELETE ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.full_name=' || quote(OLD.full_name) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.position=' || quote(OLD.position) || '; ' || 'OLD.is_active=' || quote(OLD.is_active)); END;

CREATE TRIGGER audit_employees_insert AFTER INSERT ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.full_name=' || quote(NEW.full_name) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.position=' || quote(NEW.position) || '; ' || 'NEW.is_active=' || quote(NEW.is_active)); END;

CREATE TRIGGER audit_employees_update AFTER UPDATE ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.full_name=' || quote(OLD.full_name) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.position=' || quote(OLD.position) || '; ' || 'OLD.is_active=' || quote(OLD.is_active) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.full_name=' || quote(NEW.full_name) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.position=' || quote(NEW.position) || '; ' || 'NEW.is_active=' || quote(NEW.is_active)); END;

CREATE TRIGGER employees_identity BEFORE UPDATE ON employees WHEN OLD.id!=NEW.id OR OLD.project_id!=NEW.project_id
BEGIN SELECT RAISE(ABORT,'Принадлежность записи объекту неизменяема'); END;

CREATE TABLE work_orders_41 (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    order_number TEXT, shift_date TEXT NOT NULL CHECK(length(shift_date)=10
        AND shift_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND substr(shift_date,1,4)>='0001'
        AND date(shift_date,'+0 days') IS NOT NULL AND date(shift_date,'+0 days')=shift_date),
    shift_start TEXT NOT NULL CHECK(length(shift_start)=5 AND shift_start GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(shift_start,1,2)<='23'),
    shift_end TEXT NOT NULL CHECK(length(shift_end)=5 AND shift_end GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(shift_end,1,2)<='23' AND shift_end!=shift_start),
    contractor_id INTEGER NOT NULL, issued_by_employee_id INTEGER, foreman_employee_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','issued','in_progress','submitted','accepted','closed','cancelled')),
    notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    issued_at TEXT, closed_at TEXT, header_snapshot TEXT NOT NULL DEFAULT '',
    cancellation_reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
    draft_key TEXT NOT NULL UNIQUE, workflow_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id,order_number),
    FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id),
    FOREIGN KEY(issued_by_employee_id,project_id) REFERENCES employee_projects(employee_id,project_id),
    FOREIGN KEY(foreman_employee_id,project_id) REFERENCES employee_projects(employee_id,project_id),
    CHECK((issued_at IS NULL AND order_number IS NULL) OR
          (issued_at IS NOT NULL AND order_number IS NOT NULL AND length(header_snapshot)>0))
);

INSERT INTO work_orders_41("id","project_id","order_number","shift_date","shift_start","shift_end","contractor_id","issued_by_employee_id","foreman_employee_id","status","notes","created_at","updated_at","issued_at","closed_at","header_snapshot","cancellation_reason","revision","draft_key","workflow_reason") SELECT "id","project_id","order_number","shift_date","shift_start","shift_end","contractor_id","issued_by_employee_id","foreman_employee_id","status","notes","created_at","updated_at","issued_at","closed_at","header_snapshot","cancellation_reason","revision","draft_key","workflow_reason" FROM work_orders;

DROP TABLE work_orders;

ALTER TABLE work_orders_41 RENAME TO work_orders;

CREATE INDEX work_orders_registry ON work_orders(project_id,shift_date,status,contractor_id);

CREATE TRIGGER audit_order_workflow_reason AFTER UPDATE OF workflow_reason ON work_orders WHEN OLD.workflow_reason!=NEW.workflow_reason BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('work_orders',NEW.id,'UPDATE',NEW.updated_at,'OLD.workflow_reason=' || quote(OLD.workflow_reason) || '; NEW.workflow_reason=' || quote(NEW.workflow_reason)); END;

CREATE TRIGGER audit_work_orders_delete AFTER DELETE ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.order_number=' || quote(OLD.order_number) || '; ' || 'OLD.shift_date=' || quote(OLD.shift_date) || '; ' || 'OLD.shift_start=' || quote(OLD.shift_start) || '; ' || 'OLD.shift_end=' || quote(OLD.shift_end) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.issued_by_employee_id=' || quote(OLD.issued_by_employee_id) || '; ' || 'OLD.foreman_employee_id=' || quote(OLD.foreman_employee_id) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.notes=' || quote(OLD.notes) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'OLD.issued_at=' || quote(OLD.issued_at) || '; ' || 'OLD.closed_at=' || quote(OLD.closed_at) || '; ' || 'OLD.header_snapshot=' || quote(OLD.header_snapshot) || '; ' || 'OLD.cancellation_reason=' || quote(OLD.cancellation_reason) || '; ' || 'OLD.revision=' || quote(OLD.revision) || '; ' || 'OLD.draft_key=' || quote(OLD.draft_key)); END;

CREATE TRIGGER audit_work_orders_insert AFTER INSERT ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.order_number=' || quote(NEW.order_number) || '; ' || 'NEW.shift_date=' || quote(NEW.shift_date) || '; ' || 'NEW.shift_start=' || quote(NEW.shift_start) || '; ' || 'NEW.shift_end=' || quote(NEW.shift_end) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.issued_by_employee_id=' || quote(NEW.issued_by_employee_id) || '; ' || 'NEW.foreman_employee_id=' || quote(NEW.foreman_employee_id) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.notes=' || quote(NEW.notes) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at) || '; ' || 'NEW.issued_at=' || quote(NEW.issued_at) || '; ' || 'NEW.closed_at=' || quote(NEW.closed_at) || '; ' || 'NEW.header_snapshot=' || quote(NEW.header_snapshot) || '; ' || 'NEW.cancellation_reason=' || quote(NEW.cancellation_reason) || '; ' || 'NEW.revision=' || quote(NEW.revision) || '; ' || 'NEW.draft_key=' || quote(NEW.draft_key)); END;

CREATE TRIGGER audit_work_orders_update AFTER UPDATE ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.order_number=' || quote(OLD.order_number) || '; ' || 'OLD.shift_date=' || quote(OLD.shift_date) || '; ' || 'OLD.shift_start=' || quote(OLD.shift_start) || '; ' || 'OLD.shift_end=' || quote(OLD.shift_end) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.issued_by_employee_id=' || quote(OLD.issued_by_employee_id) || '; ' || 'OLD.foreman_employee_id=' || quote(OLD.foreman_employee_id) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.notes=' || quote(OLD.notes) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'OLD.issued_at=' || quote(OLD.issued_at) || '; ' || 'OLD.closed_at=' || quote(OLD.closed_at) || '; ' || 'OLD.header_snapshot=' || quote(OLD.header_snapshot) || '; ' || 'OLD.cancellation_reason=' || quote(OLD.cancellation_reason) || '; ' || 'OLD.revision=' || quote(OLD.revision) || '; ' || 'OLD.draft_key=' || quote(OLD.draft_key) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.order_number=' || quote(NEW.order_number) || '; ' || 'NEW.shift_date=' || quote(NEW.shift_date) || '; ' || 'NEW.shift_start=' || quote(NEW.shift_start) || '; ' || 'NEW.shift_end=' || quote(NEW.shift_end) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.issued_by_employee_id=' || quote(NEW.issued_by_employee_id) || '; ' || 'NEW.foreman_employee_id=' || quote(NEW.foreman_employee_id) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.notes=' || quote(NEW.notes) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at) || '; ' || 'NEW.issued_at=' || quote(NEW.issued_at) || '; ' || 'NEW.closed_at=' || quote(NEW.closed_at) || '; ' || 'NEW.header_snapshot=' || quote(NEW.header_snapshot) || '; ' || 'NEW.cancellation_reason=' || quote(NEW.cancellation_reason) || '; ' || 'NEW.revision=' || quote(NEW.revision) || '; ' || 'NEW.draft_key=' || quote(NEW.draft_key)); END;

CREATE TRIGGER work_order_status_changed AFTER UPDATE OF status ON work_orders WHEN NEW.status!=OLD.status
BEGIN INSERT INTO work_order_status_history(work_order_id,old_status,new_status,changed_at,reason)
    VALUES(NEW.id,OLD.status,NEW.status,NEW.updated_at,
        CASE WHEN NEW.status='cancelled' THEN NEW.cancellation_reason WHEN NEW.status='issued' THEN 'Выдача наряда' ELSE NEW.workflow_reason END); END;

CREATE TRIGGER work_order_status_created AFTER INSERT ON work_orders
BEGIN INSERT INTO work_order_status_history(work_order_id,old_status,new_status,changed_at,reason)
    VALUES(NEW.id,NULL,'draft',NEW.created_at,'Создание черновика'); END;

CREATE TRIGGER work_orders_insert_guard BEFORE INSERT ON work_orders
WHEN NEW.status!='draft' OR NEW.order_number IS NOT NULL OR NEW.issued_at IS NOT NULL
BEGIN SELECT RAISE(ABORT,'Создавайте черновик наряда'); END;

CREATE TRIGGER work_orders_no_delete BEFORE DELETE ON work_orders
BEGIN SELECT RAISE(ABORT,'Отмените наряд с сохранением истории'); END;

CREATE TRIGGER work_orders_update_guard BEFORE UPDATE ON work_orders
BEGIN
    SELECT CASE WHEN NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.created_at!=OLD.created_at OR NEW.draft_key!=OLD.draft_key
        THEN RAISE(ABORT,'Идентификаторы наряда неизменяемы') END;
    SELECT CASE WHEN NOT ((OLD.status='draft' AND NEW.status IN ('draft','issued','cancelled'))
        OR (OLD.status='issued' AND NEW.status IN ('in_progress','cancelled'))
        OR (OLD.status='in_progress' AND NEW.status IN ('submitted','cancelled'))
        OR (OLD.status='submitted' AND NEW.status='accepted') OR (OLD.status='accepted' AND NEW.status='closed'))
        THEN RAISE(ABORT,'Недопустимый переход статуса наряда') END;
    SELECT CASE WHEN NEW.status='cancelled' AND length(trim(NEW.cancellation_reason))=0
        THEN RAISE(ABORT,'Укажите основание отмены') END;
    SELECT CASE WHEN OLD.status='draft' AND NEW.status!='issued' AND (NEW.order_number IS NOT NULL OR NEW.issued_at IS NOT NULL OR NEW.header_snapshot!='')
        THEN RAISE(ABORT,'Черновик не занимает номер') END;
    SELECT CASE WHEN OLD.status!='draft' AND
        (NEW.order_number IS NOT OLD.order_number OR NEW.shift_date!=OLD.shift_date OR NEW.shift_start!=OLD.shift_start OR NEW.shift_end!=OLD.shift_end
        OR NEW.contractor_id!=OLD.contractor_id OR NEW.issued_by_employee_id IS NOT OLD.issued_by_employee_id
        OR NEW.foreman_employee_id IS NOT OLD.foreman_employee_id OR NEW.notes!=OLD.notes OR NEW.issued_at IS NOT OLD.issued_at
        OR NEW.header_snapshot!=OLD.header_snapshot OR NEW.revision!=OLD.revision)
        THEN RAISE(ABORT,'Выданный наряд неизменяем') END;
    SELECT CASE WHEN NEW.closed_at IS NOT OLD.closed_at AND NOT (OLD.status='accepted' AND NEW.status='closed' AND NEW.closed_at=NEW.updated_at)
        THEN RAISE(ABORT,'Дата закрытия назначается при закрытии') END;
    SELECT CASE WHEN OLD.status!='draft' AND NEW.updated_at<OLD.updated_at
        THEN RAISE(ABORT,'Дата действия не может предшествовать истории наряда') END;
    SELECT CASE WHEN NEW.status='issued' AND (NEW.issued_by_employee_id IS NULL OR NEW.foreman_employee_id IS NULL
        OR NEW.order_number IS NULL OR NEW.issued_at IS NULL OR NEW.header_snapshot='' OR NOT EXISTS(SELECT 1 FROM work_order_lines WHERE work_order_id=NEW.id))
        THEN RAISE(ABORT,'Наряд не готов к выдаче') END;
    SELECT CASE WHEN NEW.status='in_progress' AND NOT EXISTS(SELECT 1 FROM fact_events f JOIN work_order_lines l ON l.id=f.work_order_line_id
        WHERE l.work_order_id=NEW.id AND f.correction_of_id IS NULL AND substr(f.quantity_delta,1,1)!='-')
        THEN RAISE(ABORT,'Нет факта, связанного с нарядом') END;
END;

CREATE TABLE specialty_name_keys (
 normalized_name TEXT PRIMARY KEY, specialty_id INTEGER NOT NULL REFERENCES specialties(id)
);
CREATE TABLE brigades (
 id INTEGER PRIMARY KEY, contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 number TEXT NOT NULL CHECK(typeof(number)='text' AND length(trim(number)) BETWEEN 1 AND 80),
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
 UNIQUE(contractor_id,number)
);
CREATE TABLE brigade_assignments (
 id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
 brigade_id INTEGER NOT NULL REFERENCES brigades(id),
 start_date TEXT NOT NULL CHECK(length(start_date)=10 AND date(start_date)=start_date),
 end_date TEXT, recorded_at TEXT NOT NULL, closed_at TEXT,
 CHECK(end_date IS NULL OR (length(end_date)=10 AND date(end_date)=end_date AND end_date>=start_date)),
 CHECK((end_date IS NULL AND closed_at IS NULL) OR (end_date IS NOT NULL AND closed_at IS NOT NULL))
);
CREATE INDEX brigade_membership_time ON brigade_assignments(employee_id,start_date,end_date);
CREATE INDEX brigade_composition_time ON brigade_assignments(brigade_id,start_date,end_date);
CREATE INDEX employee_project_time ON employee_project_periods(employee_project_id,start_date,end_date);
ALTER TABLE work_order_crew_members ADD COLUMN brigade_id INTEGER REFERENCES brigades(id);
ALTER TABLE work_order_crew_members ADD COLUMN brigade_number_snapshot TEXT NOT NULL DEFAULT '';
CREATE TABLE personnel_import_sessions (
 id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), contractor_id INTEGER NOT NULL,
 effective_date TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', file_digest TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('context','uploaded','reviewed','preview','committed','cancelled')),
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 confirmed_at TEXT, report_json TEXT NOT NULL DEFAULT '{}', preview_signature TEXT,
 FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE INDEX personnel_import_repeat ON personnel_import_sessions(project_id,contractor_id,file_digest,status);
CREATE TABLE personnel_import_rows (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES personnel_import_sessions(id),
 row_number INTEGER NOT NULL, full_name TEXT NOT NULL, brigade_number TEXT NOT NULL,
 specialty_name TEXT NOT NULL, fingerprint TEXT NOT NULL, errors_json TEXT NOT NULL DEFAULT '[]',
 decision TEXT NOT NULL DEFAULT '' CHECK(decision IN ('','create','link','update','skip')),
 employee_id INTEGER REFERENCES employees(id), clear_brigade INTEGER NOT NULL DEFAULT 0 CHECK(clear_brigade IN (0,1)),
 acknowledge INTEGER NOT NULL DEFAULT 0 CHECK(acknowledge IN (0,1)),
 UNIQUE(session_id,row_number)
);
CREATE TABLE personnel_import_mappings (
 id INTEGER PRIMARY KEY, contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 file_digest TEXT NOT NULL, row_number INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 employee_id INTEGER NOT NULL REFERENCES employees(id), confirmed_signature TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES personnel_import_sessions(id), confirmed_at TEXT NOT NULL,
 UNIQUE(contractor_id,file_digest,row_number,fingerprint)
);
CREATE TABLE personnel_import_log (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES personnel_import_sessions(id),
 action TEXT NOT NULL, recorded_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER employee_no_physical_delete BEFORE DELETE ON employees
BEGIN SELECT RAISE(ABORT,'Работники не удаляются физически; используйте архив'); END;
CREATE TRIGGER brigade_no_delete BEFORE DELETE ON brigades
BEGIN SELECT RAISE(ABORT,'История бригад сохраняется; используйте архив'); END;
CREATE TRIGGER brigade_identity BEFORE UPDATE ON brigades
WHEN OLD.id!=NEW.id OR OLD.contractor_id!=NEW.contractor_id OR OLD.created_at!=NEW.created_at
BEGIN SELECT RAISE(ABORT,'Идентификатор и подрядчик бригады неизменяемы'); END;
CREATE TRIGGER employee_project_scope BEFORE INSERT ON employee_projects
WHEN NOT EXISTS(SELECT 1 FROM employees e WHERE e.id=NEW.employee_id AND
 (e.contractor_id IS NULL AND e.project_id=NEW.project_id OR EXISTS(SELECT 1 FROM contractor_projects cp WHERE cp.contractor_id=e.contractor_id AND cp.project_id=NEW.project_id)))
BEGIN SELECT RAISE(ABORT,'Подрядчик работника недоступен на объекте'); END;
CREATE TRIGGER employee_contract_scope BEFORE UPDATE OF contractor_id ON employees
WHEN NEW.contractor_id IS NOT OLD.contractor_id AND (
 EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=OLD.id AND NOT EXISTS(SELECT 1 FROM contractor_projects cp WHERE cp.contractor_id=NEW.contractor_id AND cp.project_id=ep.project_id))
 OR EXISTS(SELECT 1 FROM brigade_assignments a WHERE a.employee_id=OLD.id AND a.end_date IS NULL))
BEGIN SELECT RAISE(ABORT,'Сначала завершите назначение в бригаду и обеспечьте доступность подрядчика на всех объектах работника'); END;
CREATE TRIGGER brigade_assignment_scope BEFORE INSERT ON brigade_assignments
WHEN NOT EXISTS(SELECT 1 FROM employees e JOIN brigades b ON b.contractor_id=e.contractor_id
 WHERE e.id=NEW.employee_id AND b.id=NEW.brigade_id AND e.is_active=1 AND b.is_active=1)
 OR EXISTS(SELECT 1 FROM brigade_assignments a WHERE a.employee_id=NEW.employee_id
 AND a.start_date<COALESCE(NEW.end_date,'9999-12-31') AND NEW.start_date<COALESCE(a.end_date,'9999-12-31'))
BEGIN SELECT RAISE(ABORT,'Конфликт подрядчика или пересечение периодов бригады'); END;
CREATE TRIGGER employee_period_overlap BEFORE INSERT ON employee_project_periods
WHEN EXISTS(SELECT 1 FROM employee_project_periods p WHERE p.employee_project_id=NEW.employee_project_id
 AND COALESCE(p.start_date,'0001-01-01')<COALESCE(NEW.end_date,'9999-12-31')
 AND COALESCE(NEW.start_date,'0001-01-01')<COALESCE(p.end_date,'9999-12-31'))
BEGIN SELECT RAISE(ABORT,'Периоды доступности на объекте пересекаются'); END;


DROP TRIGGER work_order_members_insert_scope;

CREATE TRIGGER work_order_members_insert_scope BEFORE INSERT ON work_order_crew_members
WHEN NOT EXISTS(SELECT 1 FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id
 JOIN employees e ON e.id=NEW.employee_id WHERE c.id=NEW.crew_id AND EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=e.id AND ep.project_id=o.project_id)
 AND e.specialty_id=c.specialty_id AND (e.contractor_id IS NULL OR e.contractor_id=o.contractor_id))
BEGIN SELECT RAISE(ABORT,'Работник не соответствует объекту, подрядчику или специальности'); END;

DROP TRIGGER work_order_members_update_scope;

CREATE TRIGGER work_order_members_update_scope BEFORE UPDATE ON work_order_crew_members
WHEN NOT EXISTS(SELECT 1 FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id
 JOIN employees e ON e.id=NEW.employee_id WHERE c.id=NEW.crew_id AND EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=e.id AND ep.project_id=o.project_id)
 AND e.specialty_id=c.specialty_id AND (e.contractor_id IS NULL OR e.contractor_id=o.contractor_id))
BEGIN SELECT RAISE(ABORT,'Работник не соответствует объекту, подрядчику или специальности'); END;

DROP TRIGGER work_order_approvals_scope;

CREATE TRIGGER work_order_approvals_scope BEFORE INSERT ON work_order_approvals
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN employees e ON e.id=NEW.employee_id
 WHERE o.id=NEW.work_order_id AND EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=e.id AND ep.project_id=o.project_id) AND o.status='issued' AND e.id=o.issued_by_employee_id)
BEGIN SELECT RAISE(ABORT,'Выдачу регистрирует ответственное лицо текущего объекта'); END;

DROP TRIGGER order_workflow_action_scope;

CREATE TRIGGER order_workflow_action_scope BEFORE INSERT ON work_order_workflow_actions
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN employees e ON e.id=NEW.employee_id
    WHERE o.id=NEW.work_order_id AND EXISTS(SELECT 1 FROM employee_projects ep WHERE ep.employee_id=e.id AND ep.project_id=o.project_id) AND o.order_number IS NOT NULL AND o.status=NEW.action)
    OR (NEW.status_history_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM work_order_status_history h
        WHERE h.id=NEW.status_history_id AND h.work_order_id=NEW.work_order_id AND h.new_status=NEW.action))
BEGIN SELECT RAISE(ABORT,'Согласование не соответствует наряду или ответственному лицу'); END;

CREATE TRIGGER contractor_projects_no_update BEFORE UPDATE ON contractor_projects BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER contractor_projects_no_delete BEFORE DELETE ON contractor_projects BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER employee_projects_no_update BEFORE UPDATE ON employee_projects BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER employee_projects_no_delete BEFORE DELETE ON employee_projects BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER personnel_import_log_no_update BEFORE UPDATE ON personnel_import_log BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER personnel_import_log_no_delete BEFORE DELETE ON personnel_import_log BEGIN SELECT RAISE(ABORT,'История персонала неизменяема'); END;

CREATE TRIGGER brigade_assignments_no_delete BEFORE DELETE ON brigade_assignments BEGIN SELECT RAISE(ABORT,'История назначений неизменяема'); END;

CREATE TRIGGER brigade_assignments_close_only BEFORE UPDATE ON brigade_assignments WHEN NEW.id IS NOT OLD.id OR NEW.employee_id IS NOT OLD.employee_id OR NEW.brigade_id IS NOT OLD.brigade_id OR NEW.start_date IS NOT OLD.start_date OR NEW.recorded_at IS NOT OLD.recorded_at OR OLD.end_date IS NOT NULL OR NEW.end_date IS NULL OR NEW.closed_at IS NULL OR NEW.closed_at<COALESCE(OLD.recorded_at,'') BEGIN SELECT RAISE(ABORT,'Разрешено только завершение открытого назначения'); END;

CREATE TRIGGER employee_project_periods_no_delete BEFORE DELETE ON employee_project_periods BEGIN SELECT RAISE(ABORT,'История назначений неизменяема'); END;

CREATE TRIGGER employee_project_periods_close_only BEFORE UPDATE ON employee_project_periods WHEN NEW.id IS NOT OLD.id OR NEW.employee_project_id IS NOT OLD.employee_project_id OR NEW.start_date IS NOT OLD.start_date OR NEW.recorded_at IS NOT OLD.recorded_at OR OLD.end_date IS NOT NULL OR NEW.end_date IS NULL OR NEW.closed_at IS NULL OR NEW.closed_at<COALESCE(OLD.recorded_at,'') BEGIN SELECT RAISE(ABORT,'Разрешено только завершение открытого назначения'); END;

CREATE TRIGGER personnel_import_rows_sealed_update BEFORE UPDATE ON personnel_import_rows WHEN EXISTS(SELECT 1 FROM personnel_import_sessions s WHERE s.id=OLD.session_id AND s.status IN ('committed','cancelled')) BEGIN SELECT RAISE(ABORT,'Завершённый импорт неизменяем'); END;

CREATE TRIGGER personnel_import_rows_sealed_delete BEFORE DELETE ON personnel_import_rows WHEN EXISTS(SELECT 1 FROM personnel_import_sessions s WHERE s.id=OLD.session_id AND s.status IN ('committed','cancelled')) BEGIN SELECT RAISE(ABORT,'Завершённый импорт неизменяем'); END;

CREATE TRIGGER personnel_import_sessions_sealed_update BEFORE UPDATE ON personnel_import_sessions WHEN EXISTS(SELECT 1 FROM personnel_import_sessions s WHERE s.id=OLD.id AND s.status IN ('committed','cancelled')) BEGIN SELECT RAISE(ABORT,'Завершённый импорт неизменяем'); END;

CREATE TRIGGER personnel_import_sessions_sealed_delete BEFORE DELETE ON personnel_import_sessions WHEN EXISTS(SELECT 1 FROM personnel_import_sessions s WHERE s.id=OLD.id AND s.status IN ('committed','cancelled')) BEGIN SELECT RAISE(ABORT,'Завершённый импорт неизменяем'); END;

CREATE TRIGGER personnel_import_rows_sealed_insert BEFORE INSERT ON personnel_import_rows
WHEN EXISTS(SELECT 1 FROM personnel_import_sessions s WHERE s.id=NEW.session_id AND s.status IN ('committed','cancelled'))
BEGIN SELECT RAISE(ABORT,'Завершённый импорт неизменяем'); END;
CREATE TRIGGER employee_creation_immutable BEFORE UPDATE OF created_at ON employees
WHEN OLD.created_at IS NOT NULL AND NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Дата создания работника неизменяема'); END;
