-- Additive document module. No existing IDs, plans or quantity journals change.
CREATE TABLE contractors (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
    short_name TEXT NOT NULL DEFAULT '', inn TEXT NOT NULL DEFAULT '',
    contact_person TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(id,project_id)
);
CREATE TABLE specialties (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
    code TEXT NOT NULL UNIQUE CHECK(length(trim(code)) BETWEEN 1 AND 80)
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    contractor_id INTEGER, full_name TEXT NOT NULL CHECK(length(trim(full_name)) BETWEEN 1 AND 200),
    specialty_id INTEGER REFERENCES specialties(id), position TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    UNIQUE(id,project_id), FOREIGN KEY(contractor_id,project_id) REFERENCES contractors(id,project_id)
);
CREATE TABLE work_order_sequences (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    last_number INTEGER NOT NULL CHECK(typeof(last_number)='integer' AND last_number>0)
);
CREATE TABLE work_orders (
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
    draft_key TEXT NOT NULL UNIQUE,
    UNIQUE(project_id,order_number),
    FOREIGN KEY(contractor_id,project_id) REFERENCES contractors(id,project_id),
    FOREIGN KEY(issued_by_employee_id,project_id) REFERENCES employees(id,project_id),
    FOREIGN KEY(foreman_employee_id,project_id) REFERENCES employees(id,project_id),
    CHECK((issued_at IS NULL AND order_number IS NULL) OR
          (issued_at IS NOT NULL AND order_number IS NOT NULL AND length(header_snapshot)>0))
);
CREATE INDEX work_orders_registry ON work_orders(project_id,shift_date,status,contractor_id);
CREATE TABLE work_order_lines (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    work_item_id INTEGER NOT NULL REFERENCES work_items(id), plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
    planned_quantity TEXT NOT NULL CHECK(typeof(planned_quantity)='text' AND planned_quantity!='0' AND instr(planned_quantity,char(0))=0
        AND length(planned_quantity) BETWEEN 1 AND 28 AND planned_quantity NOT GLOB '*[^0-9.]*'
        AND length(planned_quantity)-length(replace(planned_quantity,'.',''))<=1
        AND length(CASE WHEN instr(planned_quantity,'.')=0 THEN planned_quantity ELSE substr(planned_quantity,1,instr(planned_quantity,'.')-1) END) BETWEEN 1 AND 18
        AND (planned_quantity='0' OR substr(planned_quantity,1,1) BETWEEN '1' AND '9' OR substr(planned_quantity,1,2)='0.')
        AND (instr(planned_quantity,'.')=0 OR (length(planned_quantity)-instr(planned_quantity,'.') BETWEEN 1 AND 9 AND substr(planned_quantity,-1) BETWEEN '1' AND '9'))),
    unit_snapshot TEXT NOT NULL, work_name_snapshot TEXT NOT NULL, location_snapshot TEXT NOT NULL,
    sort_order INTEGER NOT NULL CHECK(typeof(sort_order)='integer'), remarks TEXT NOT NULL DEFAULT '',
    UNIQUE(work_order_id,work_item_id),
    FOREIGN KEY(plan_version_id,work_item_id) REFERENCES plan_lines(plan_version_id,work_item_id)
);
CREATE INDEX work_order_lines_item ON work_order_lines(work_item_id,work_order_id);
CREATE TABLE work_order_crew (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    specialty_id INTEGER NOT NULL REFERENCES specialties(id), planned_count INTEGER CHECK(planned_count IS NULL OR (typeof(planned_count)='integer' AND planned_count>0)),
    comment TEXT NOT NULL DEFAULT '', specialty_name_snapshot TEXT NOT NULL,
    employees_snapshot TEXT NOT NULL DEFAULT '[]', UNIQUE(work_order_id,specialty_id)
);
CREATE TABLE work_order_crew_members (
    id INTEGER PRIMARY KEY, crew_id INTEGER NOT NULL REFERENCES work_order_crew(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id), UNIQUE(crew_id,employee_id)
);
CREATE TABLE work_order_approvals (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    action TEXT NOT NULL CHECK(action='issued'), employee_id INTEGER NOT NULL REFERENCES employees(id),
    employee_name_snapshot TEXT NOT NULL, performed_at TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
    UNIQUE(work_order_id,action)
);
CREATE TABLE work_order_status_history (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    old_status TEXT, new_status TEXT NOT NULL, changed_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER work_orders_insert_guard BEFORE INSERT ON work_orders
WHEN NEW.status!='draft' OR NEW.order_number IS NOT NULL OR NEW.issued_at IS NOT NULL
BEGIN SELECT RAISE(ABORT,'Создавайте черновик наряда'); END;
CREATE TRIGGER work_orders_update_guard BEFORE UPDATE ON work_orders
BEGIN
    SELECT CASE WHEN NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.created_at!=OLD.created_at OR NEW.draft_key!=OLD.draft_key
        THEN RAISE(ABORT,'Идентификаторы наряда неизменяемы') END;
    SELECT CASE WHEN NOT ((OLD.status='draft' AND NEW.status IN ('draft','issued','cancelled')) OR (OLD.status='issued' AND NEW.status='cancelled'))
        THEN RAISE(ABORT,'Переход статуса недоступен на этапе 3.2') END;
    SELECT CASE WHEN NEW.status='cancelled' AND length(trim(NEW.cancellation_reason))=0
        THEN RAISE(ABORT,'Укажите основание отмены') END;
    SELECT CASE WHEN OLD.status='draft' AND NEW.status!='issued' AND (NEW.order_number IS NOT NULL OR NEW.issued_at IS NOT NULL OR NEW.header_snapshot!='')
        THEN RAISE(ABORT,'Черновик не занимает номер') END;
    SELECT CASE WHEN OLD.status='issued' AND
        (NEW.order_number IS NOT OLD.order_number OR NEW.shift_date!=OLD.shift_date OR NEW.shift_start!=OLD.shift_start OR NEW.shift_end!=OLD.shift_end
        OR NEW.contractor_id!=OLD.contractor_id OR NEW.issued_by_employee_id IS NOT OLD.issued_by_employee_id
        OR NEW.foreman_employee_id IS NOT OLD.foreman_employee_id OR NEW.notes!=OLD.notes OR NEW.issued_at IS NOT OLD.issued_at
        OR NEW.header_snapshot!=OLD.header_snapshot OR NEW.closed_at IS NOT OLD.closed_at OR NEW.revision!=OLD.revision)
        THEN RAISE(ABORT,'Выданный наряд неизменяем') END;
    SELECT CASE WHEN NEW.status='issued' AND (NEW.issued_by_employee_id IS NULL OR NEW.foreman_employee_id IS NULL
        OR NEW.order_number IS NULL OR NEW.issued_at IS NULL OR NEW.header_snapshot='' OR NOT EXISTS(SELECT 1 FROM work_order_lines WHERE work_order_id=NEW.id))
        THEN RAISE(ABORT,'Наряд не готов к выдаче') END;
END;
CREATE TRIGGER work_orders_no_delete BEFORE DELETE ON work_orders
BEGIN SELECT RAISE(ABORT,'Отмените наряд с сохранением истории'); END;
CREATE TRIGGER work_order_status_created AFTER INSERT ON work_orders
BEGIN INSERT INTO work_order_status_history(work_order_id,old_status,new_status,changed_at,reason)
    VALUES(NEW.id,NULL,'draft',NEW.created_at,'Создание черновика'); END;
CREATE TRIGGER work_order_status_changed AFTER UPDATE OF status ON work_orders WHEN NEW.status!=OLD.status
BEGIN INSERT INTO work_order_status_history(work_order_id,old_status,new_status,changed_at,reason)
    VALUES(NEW.id,OLD.status,NEW.status,NEW.updated_at,CASE WHEN NEW.status='cancelled' THEN NEW.cancellation_reason ELSE 'Выдача наряда' END); END;
CREATE TRIGGER work_order_sequences_no_delete BEFORE DELETE ON work_order_sequences
BEGIN SELECT RAISE(ABORT,'Счётчик нарядов нельзя сбросить'); END;
CREATE TRIGGER work_order_sequences_no_rewind BEFORE UPDATE ON work_order_sequences
WHEN NEW.project_id!=OLD.project_id OR NEW.last_number<=OLD.last_number
BEGIN SELECT RAISE(ABORT,'Счётчик нарядов нельзя уменьшить'); END;

CREATE TRIGGER work_order_lines_insert_draft_guard BEFORE INSERT ON work_order_lines
WHEN COALESCE((SELECT status FROM work_orders WHERE id=NEW.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_lines_update_draft_guard BEFORE UPDATE ON work_order_lines
WHEN COALESCE((SELECT status FROM work_orders WHERE id=OLD.work_order_id),'missing')!='draft' OR COALESCE((SELECT status FROM work_orders WHERE id=NEW.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_lines_delete_draft_guard BEFORE DELETE ON work_order_lines
WHEN COALESCE((SELECT status FROM work_orders WHERE id=OLD.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_insert_draft_guard BEFORE INSERT ON work_order_crew
WHEN COALESCE((SELECT status FROM work_orders WHERE id=NEW.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_update_draft_guard BEFORE UPDATE ON work_order_crew
WHEN COALESCE((SELECT status FROM work_orders WHERE id=OLD.work_order_id),'missing')!='draft' OR COALESCE((SELECT status FROM work_orders WHERE id=NEW.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_delete_draft_guard BEFORE DELETE ON work_order_crew
WHEN COALESCE((SELECT status FROM work_orders WHERE id=OLD.work_order_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_members_insert_draft_guard BEFORE INSERT ON work_order_crew_members
WHEN COALESCE((SELECT o.status FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id WHERE c.id=NEW.crew_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_members_update_draft_guard BEFORE UPDATE ON work_order_crew_members
WHEN COALESCE((SELECT o.status FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id WHERE c.id=OLD.crew_id),'missing')!='draft' OR COALESCE((SELECT o.status FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id WHERE c.id=NEW.crew_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_crew_members_delete_draft_guard BEFORE DELETE ON work_order_crew_members
WHEN COALESCE((SELECT o.status FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id WHERE c.id=OLD.crew_id),'missing')!='draft'
BEGIN SELECT RAISE(ABORT,'Строки выданного наряда неизменяемы'); END;
CREATE TRIGGER work_order_lines_insert_scope BEFORE INSERT ON work_order_lines
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN work_items w ON w.id=NEW.work_item_id
 JOIN buildings b ON b.id=w.building_id JOIN plan_versions v ON v.id=NEW.plan_version_id
 WHERE o.id=NEW.work_order_id AND b.project_id=o.project_id AND v.project_id=o.project_id
 AND w.discipline='CONSTRUCTION' AND v.discipline=w.discipline AND v.status='APPROVED')
BEGIN SELECT RAISE(ABORT,'Работа или версия плана не принадлежит объекту наряда'); END;
CREATE TRIGGER work_order_members_insert_scope BEFORE INSERT ON work_order_crew_members
WHEN NOT EXISTS(SELECT 1 FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id
 JOIN employees e ON e.id=NEW.employee_id WHERE c.id=NEW.crew_id AND e.project_id=o.project_id
 AND e.specialty_id=c.specialty_id AND (e.contractor_id IS NULL OR e.contractor_id=o.contractor_id))
BEGIN SELECT RAISE(ABORT,'Работник не соответствует объекту, подрядчику или специальности'); END;
CREATE TRIGGER work_order_lines_update_scope BEFORE UPDATE ON work_order_lines
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN work_items w ON w.id=NEW.work_item_id
 JOIN buildings b ON b.id=w.building_id JOIN plan_versions v ON v.id=NEW.plan_version_id
 WHERE o.id=NEW.work_order_id AND b.project_id=o.project_id AND v.project_id=o.project_id
 AND w.discipline='CONSTRUCTION' AND v.discipline=w.discipline AND v.status='APPROVED')
BEGIN SELECT RAISE(ABORT,'Работа или версия плана не принадлежит объекту наряда'); END;
CREATE TRIGGER work_order_members_update_scope BEFORE UPDATE ON work_order_crew_members
WHEN NOT EXISTS(SELECT 1 FROM work_order_crew c JOIN work_orders o ON o.id=c.work_order_id
 JOIN employees e ON e.id=NEW.employee_id WHERE c.id=NEW.crew_id AND e.project_id=o.project_id
 AND e.specialty_id=c.specialty_id AND (e.contractor_id IS NULL OR e.contractor_id=o.contractor_id))
BEGIN SELECT RAISE(ABORT,'Работник не соответствует объекту, подрядчику или специальности'); END;
CREATE TRIGGER work_order_status_history_no_update BEFORE UPDATE ON work_order_status_history
BEGIN SELECT RAISE(ABORT,'История документа неизменяема'); END;
CREATE TRIGGER work_order_status_history_no_delete BEFORE DELETE ON work_order_status_history
BEGIN SELECT RAISE(ABORT,'История документа неизменяема'); END;
CREATE TRIGGER work_order_approvals_no_update BEFORE UPDATE ON work_order_approvals
BEGIN SELECT RAISE(ABORT,'История документа неизменяема'); END;
CREATE TRIGGER work_order_approvals_no_delete BEFORE DELETE ON work_order_approvals
BEGIN SELECT RAISE(ABORT,'История документа неизменяема'); END;
CREATE TRIGGER work_order_approvals_scope BEFORE INSERT ON work_order_approvals
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN employees e ON e.id=NEW.employee_id
 WHERE o.id=NEW.work_order_id AND e.project_id=o.project_id AND o.status='issued' AND e.id=o.issued_by_employee_id)
BEGIN SELECT RAISE(ABORT,'Выдачу регистрирует ответственное лицо текущего объекта'); END;
CREATE TRIGGER contractors_identity BEFORE UPDATE ON contractors WHEN OLD.id!=NEW.id OR OLD.project_id!=NEW.project_id
BEGIN SELECT RAISE(ABORT,'Принадлежность записи объекту неизменяема'); END;
CREATE TRIGGER employees_identity BEFORE UPDATE ON employees WHEN OLD.id!=NEW.id OR OLD.project_id!=NEW.project_id
BEGIN SELECT RAISE(ABORT,'Принадлежность записи объекту неизменяема'); END;
CREATE TRIGGER audit_contractors_insert AFTER INSERT ON contractors
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('contractors',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.short_name=' || quote(NEW.short_name) || '; ' || 'NEW.inn=' || quote(NEW.inn) || '; ' || 'NEW.contact_person=' || quote(NEW.contact_person) || '; ' || 'NEW.phone=' || quote(NEW.phone) || '; ' || 'NEW.is_active=' || quote(NEW.is_active) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at)); END;
CREATE TRIGGER audit_contractors_update AFTER UPDATE ON contractors
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('contractors',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.short_name=' || quote(OLD.short_name) || '; ' || 'OLD.inn=' || quote(OLD.inn) || '; ' || 'OLD.contact_person=' || quote(OLD.contact_person) || '; ' || 'OLD.phone=' || quote(OLD.phone) || '; ' || 'OLD.is_active=' || quote(OLD.is_active) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.short_name=' || quote(NEW.short_name) || '; ' || 'NEW.inn=' || quote(NEW.inn) || '; ' || 'NEW.contact_person=' || quote(NEW.contact_person) || '; ' || 'NEW.phone=' || quote(NEW.phone) || '; ' || 'NEW.is_active=' || quote(NEW.is_active) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at)); END;
CREATE TRIGGER audit_contractors_delete AFTER DELETE ON contractors
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('contractors',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.short_name=' || quote(OLD.short_name) || '; ' || 'OLD.inn=' || quote(OLD.inn) || '; ' || 'OLD.contact_person=' || quote(OLD.contact_person) || '; ' || 'OLD.phone=' || quote(OLD.phone) || '; ' || 'OLD.is_active=' || quote(OLD.is_active) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at)); END;
CREATE TRIGGER audit_specialties_insert AFTER INSERT ON specialties
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('specialties',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.code=' || quote(NEW.code)); END;
CREATE TRIGGER audit_specialties_update AFTER UPDATE ON specialties
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('specialties',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.code=' || quote(OLD.code) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.name=' || quote(NEW.name) || '; ' || 'NEW.code=' || quote(NEW.code)); END;
CREATE TRIGGER audit_specialties_delete AFTER DELETE ON specialties
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('specialties',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.name=' || quote(OLD.name) || '; ' || 'OLD.code=' || quote(OLD.code)); END;
CREATE TRIGGER audit_employees_insert AFTER INSERT ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.full_name=' || quote(NEW.full_name) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.position=' || quote(NEW.position) || '; ' || 'NEW.is_active=' || quote(NEW.is_active)); END;
CREATE TRIGGER audit_employees_update AFTER UPDATE ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.full_name=' || quote(OLD.full_name) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.position=' || quote(OLD.position) || '; ' || 'OLD.is_active=' || quote(OLD.is_active) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.full_name=' || quote(NEW.full_name) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.position=' || quote(NEW.position) || '; ' || 'NEW.is_active=' || quote(NEW.is_active)); END;
CREATE TRIGGER audit_employees_delete AFTER DELETE ON employees
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('employees',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.full_name=' || quote(OLD.full_name) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.position=' || quote(OLD.position) || '; ' || 'OLD.is_active=' || quote(OLD.is_active)); END;
CREATE TRIGGER audit_work_orders_insert AFTER INSERT ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.order_number=' || quote(NEW.order_number) || '; ' || 'NEW.shift_date=' || quote(NEW.shift_date) || '; ' || 'NEW.shift_start=' || quote(NEW.shift_start) || '; ' || 'NEW.shift_end=' || quote(NEW.shift_end) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.issued_by_employee_id=' || quote(NEW.issued_by_employee_id) || '; ' || 'NEW.foreman_employee_id=' || quote(NEW.foreman_employee_id) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.notes=' || quote(NEW.notes) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at) || '; ' || 'NEW.issued_at=' || quote(NEW.issued_at) || '; ' || 'NEW.closed_at=' || quote(NEW.closed_at) || '; ' || 'NEW.header_snapshot=' || quote(NEW.header_snapshot) || '; ' || 'NEW.cancellation_reason=' || quote(NEW.cancellation_reason) || '; ' || 'NEW.revision=' || quote(NEW.revision) || '; ' || 'NEW.draft_key=' || quote(NEW.draft_key)); END;
CREATE TRIGGER audit_work_orders_update AFTER UPDATE ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.order_number=' || quote(OLD.order_number) || '; ' || 'OLD.shift_date=' || quote(OLD.shift_date) || '; ' || 'OLD.shift_start=' || quote(OLD.shift_start) || '; ' || 'OLD.shift_end=' || quote(OLD.shift_end) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.issued_by_employee_id=' || quote(OLD.issued_by_employee_id) || '; ' || 'OLD.foreman_employee_id=' || quote(OLD.foreman_employee_id) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.notes=' || quote(OLD.notes) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'OLD.issued_at=' || quote(OLD.issued_at) || '; ' || 'OLD.closed_at=' || quote(OLD.closed_at) || '; ' || 'OLD.header_snapshot=' || quote(OLD.header_snapshot) || '; ' || 'OLD.cancellation_reason=' || quote(OLD.cancellation_reason) || '; ' || 'OLD.revision=' || quote(OLD.revision) || '; ' || 'OLD.draft_key=' || quote(OLD.draft_key) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.project_id=' || quote(NEW.project_id) || '; ' || 'NEW.order_number=' || quote(NEW.order_number) || '; ' || 'NEW.shift_date=' || quote(NEW.shift_date) || '; ' || 'NEW.shift_start=' || quote(NEW.shift_start) || '; ' || 'NEW.shift_end=' || quote(NEW.shift_end) || '; ' || 'NEW.contractor_id=' || quote(NEW.contractor_id) || '; ' || 'NEW.issued_by_employee_id=' || quote(NEW.issued_by_employee_id) || '; ' || 'NEW.foreman_employee_id=' || quote(NEW.foreman_employee_id) || '; ' || 'NEW.status=' || quote(NEW.status) || '; ' || 'NEW.notes=' || quote(NEW.notes) || '; ' || 'NEW.created_at=' || quote(NEW.created_at) || '; ' || 'NEW.updated_at=' || quote(NEW.updated_at) || '; ' || 'NEW.issued_at=' || quote(NEW.issued_at) || '; ' || 'NEW.closed_at=' || quote(NEW.closed_at) || '; ' || 'NEW.header_snapshot=' || quote(NEW.header_snapshot) || '; ' || 'NEW.cancellation_reason=' || quote(NEW.cancellation_reason) || '; ' || 'NEW.revision=' || quote(NEW.revision) || '; ' || 'NEW.draft_key=' || quote(NEW.draft_key)); END;
CREATE TRIGGER audit_work_orders_delete AFTER DELETE ON work_orders
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_orders',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.project_id=' || quote(OLD.project_id) || '; ' || 'OLD.order_number=' || quote(OLD.order_number) || '; ' || 'OLD.shift_date=' || quote(OLD.shift_date) || '; ' || 'OLD.shift_start=' || quote(OLD.shift_start) || '; ' || 'OLD.shift_end=' || quote(OLD.shift_end) || '; ' || 'OLD.contractor_id=' || quote(OLD.contractor_id) || '; ' || 'OLD.issued_by_employee_id=' || quote(OLD.issued_by_employee_id) || '; ' || 'OLD.foreman_employee_id=' || quote(OLD.foreman_employee_id) || '; ' || 'OLD.status=' || quote(OLD.status) || '; ' || 'OLD.notes=' || quote(OLD.notes) || '; ' || 'OLD.created_at=' || quote(OLD.created_at) || '; ' || 'OLD.updated_at=' || quote(OLD.updated_at) || '; ' || 'OLD.issued_at=' || quote(OLD.issued_at) || '; ' || 'OLD.closed_at=' || quote(OLD.closed_at) || '; ' || 'OLD.header_snapshot=' || quote(OLD.header_snapshot) || '; ' || 'OLD.cancellation_reason=' || quote(OLD.cancellation_reason) || '; ' || 'OLD.revision=' || quote(OLD.revision) || '; ' || 'OLD.draft_key=' || quote(OLD.draft_key)); END;
CREATE TRIGGER audit_work_order_lines_insert AFTER INSERT ON work_order_lines
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_lines',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.plan_version_id=' || quote(NEW.plan_version_id) || '; ' || 'NEW.planned_quantity=' || quote(NEW.planned_quantity) || '; ' || 'NEW.unit_snapshot=' || quote(NEW.unit_snapshot) || '; ' || 'NEW.work_name_snapshot=' || quote(NEW.work_name_snapshot) || '; ' || 'NEW.location_snapshot=' || quote(NEW.location_snapshot) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order) || '; ' || 'NEW.remarks=' || quote(NEW.remarks)); END;
CREATE TRIGGER audit_work_order_lines_update AFTER UPDATE ON work_order_lines
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_lines',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.plan_version_id=' || quote(OLD.plan_version_id) || '; ' || 'OLD.planned_quantity=' || quote(OLD.planned_quantity) || '; ' || 'OLD.unit_snapshot=' || quote(OLD.unit_snapshot) || '; ' || 'OLD.work_name_snapshot=' || quote(OLD.work_name_snapshot) || '; ' || 'OLD.location_snapshot=' || quote(OLD.location_snapshot) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order) || '; ' || 'OLD.remarks=' || quote(OLD.remarks) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.plan_version_id=' || quote(NEW.plan_version_id) || '; ' || 'NEW.planned_quantity=' || quote(NEW.planned_quantity) || '; ' || 'NEW.unit_snapshot=' || quote(NEW.unit_snapshot) || '; ' || 'NEW.work_name_snapshot=' || quote(NEW.work_name_snapshot) || '; ' || 'NEW.location_snapshot=' || quote(NEW.location_snapshot) || '; ' || 'NEW.sort_order=' || quote(NEW.sort_order) || '; ' || 'NEW.remarks=' || quote(NEW.remarks)); END;
CREATE TRIGGER audit_work_order_lines_delete AFTER DELETE ON work_order_lines
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_lines',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.work_item_id=' || quote(OLD.work_item_id) || '; ' || 'OLD.plan_version_id=' || quote(OLD.plan_version_id) || '; ' || 'OLD.planned_quantity=' || quote(OLD.planned_quantity) || '; ' || 'OLD.unit_snapshot=' || quote(OLD.unit_snapshot) || '; ' || 'OLD.work_name_snapshot=' || quote(OLD.work_name_snapshot) || '; ' || 'OLD.location_snapshot=' || quote(OLD.location_snapshot) || '; ' || 'OLD.sort_order=' || quote(OLD.sort_order) || '; ' || 'OLD.remarks=' || quote(OLD.remarks)); END;
CREATE TRIGGER audit_work_order_crew_insert AFTER INSERT ON work_order_crew
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.planned_count=' || quote(NEW.planned_count) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.specialty_name_snapshot=' || quote(NEW.specialty_name_snapshot) || '; ' || 'NEW.employees_snapshot=' || quote(NEW.employees_snapshot)); END;
CREATE TRIGGER audit_work_order_crew_update AFTER UPDATE ON work_order_crew
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.planned_count=' || quote(OLD.planned_count) || '; ' || 'OLD.comment=' || quote(OLD.comment) || '; ' || 'OLD.specialty_name_snapshot=' || quote(OLD.specialty_name_snapshot) || '; ' || 'OLD.employees_snapshot=' || quote(OLD.employees_snapshot) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.specialty_id=' || quote(NEW.specialty_id) || '; ' || 'NEW.planned_count=' || quote(NEW.planned_count) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.specialty_name_snapshot=' || quote(NEW.specialty_name_snapshot) || '; ' || 'NEW.employees_snapshot=' || quote(NEW.employees_snapshot)); END;
CREATE TRIGGER audit_work_order_crew_delete AFTER DELETE ON work_order_crew
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.specialty_id=' || quote(OLD.specialty_id) || '; ' || 'OLD.planned_count=' || quote(OLD.planned_count) || '; ' || 'OLD.comment=' || quote(OLD.comment) || '; ' || 'OLD.specialty_name_snapshot=' || quote(OLD.specialty_name_snapshot) || '; ' || 'OLD.employees_snapshot=' || quote(OLD.employees_snapshot)); END;
CREATE TRIGGER audit_work_order_crew_members_insert AFTER INSERT ON work_order_crew_members
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew_members',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.crew_id=' || quote(NEW.crew_id) || '; ' || 'NEW.employee_id=' || quote(NEW.employee_id)); END;
CREATE TRIGGER audit_work_order_crew_members_update AFTER UPDATE ON work_order_crew_members
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew_members',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.crew_id=' || quote(OLD.crew_id) || '; ' || 'OLD.employee_id=' || quote(OLD.employee_id) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.crew_id=' || quote(NEW.crew_id) || '; ' || 'NEW.employee_id=' || quote(NEW.employee_id)); END;
CREATE TRIGGER audit_work_order_crew_members_delete AFTER DELETE ON work_order_crew_members
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_crew_members',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.crew_id=' || quote(OLD.crew_id) || '; ' || 'OLD.employee_id=' || quote(OLD.employee_id)); END;
CREATE TRIGGER audit_work_order_approvals_insert AFTER INSERT ON work_order_approvals
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_approvals',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.action=' || quote(NEW.action) || '; ' || 'NEW.employee_id=' || quote(NEW.employee_id) || '; ' || 'NEW.employee_name_snapshot=' || quote(NEW.employee_name_snapshot) || '; ' || 'NEW.performed_at=' || quote(NEW.performed_at) || '; ' || 'NEW.comment=' || quote(NEW.comment)); END;
CREATE TRIGGER audit_work_order_approvals_update AFTER UPDATE ON work_order_approvals
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_approvals',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.action=' || quote(OLD.action) || '; ' || 'OLD.employee_id=' || quote(OLD.employee_id) || '; ' || 'OLD.employee_name_snapshot=' || quote(OLD.employee_name_snapshot) || '; ' || 'OLD.performed_at=' || quote(OLD.performed_at) || '; ' || 'OLD.comment=' || quote(OLD.comment) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.action=' || quote(NEW.action) || '; ' || 'NEW.employee_id=' || quote(NEW.employee_id) || '; ' || 'NEW.employee_name_snapshot=' || quote(NEW.employee_name_snapshot) || '; ' || 'NEW.performed_at=' || quote(NEW.performed_at) || '; ' || 'NEW.comment=' || quote(NEW.comment)); END;
CREATE TRIGGER audit_work_order_approvals_delete AFTER DELETE ON work_order_approvals
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_approvals',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.action=' || quote(OLD.action) || '; ' || 'OLD.employee_id=' || quote(OLD.employee_id) || '; ' || 'OLD.employee_name_snapshot=' || quote(OLD.employee_name_snapshot) || '; ' || 'OLD.performed_at=' || quote(OLD.performed_at) || '; ' || 'OLD.comment=' || quote(OLD.comment)); END;
CREATE TRIGGER audit_work_order_status_history_insert AFTER INSERT ON work_order_status_history
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_status_history',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.old_status=' || quote(NEW.old_status) || '; ' || 'NEW.new_status=' || quote(NEW.new_status) || '; ' || 'NEW.changed_at=' || quote(NEW.changed_at) || '; ' || 'NEW.reason=' || quote(NEW.reason)); END;
CREATE TRIGGER audit_work_order_status_history_update AFTER UPDATE ON work_order_status_history
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_status_history',NEW.id,'UPDATE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.old_status=' || quote(OLD.old_status) || '; ' || 'OLD.new_status=' || quote(OLD.new_status) || '; ' || 'OLD.changed_at=' || quote(OLD.changed_at) || '; ' || 'OLD.reason=' || quote(OLD.reason) || '; ' || 'NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.old_status=' || quote(NEW.old_status) || '; ' || 'NEW.new_status=' || quote(NEW.new_status) || '; ' || 'NEW.changed_at=' || quote(NEW.changed_at) || '; ' || 'NEW.reason=' || quote(NEW.reason)); END;
CREATE TRIGGER audit_work_order_status_history_delete AFTER DELETE ON work_order_status_history
BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES('work_order_status_history',OLD.id,'DELETE',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','OLD.id=' || quote(OLD.id) || '; ' || 'OLD.work_order_id=' || quote(OLD.work_order_id) || '; ' || 'OLD.old_status=' || quote(OLD.old_status) || '; ' || 'OLD.new_status=' || quote(OLD.new_status) || '; ' || 'OLD.changed_at=' || quote(OLD.changed_at) || '; ' || 'OLD.reason=' || quote(OLD.reason)); END;
