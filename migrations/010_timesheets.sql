-- Working time is independent of construction volumes and work-order events.
-- An integer nanohour stores nine decimal places exactly; no REAL arithmetic.
CREATE TABLE attendance_codes (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE CHECK(length(trim(code)) BETWEEN 1 AND 20),
 name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
 counts_as_work INTEGER NOT NULL CHECK(typeof(counts_as_work)='integer' AND counts_as_work IN (0,1)),
 requires_hours INTEGER NOT NULL CHECK(typeof(requires_hours)='integer' AND requires_hours IN (0,1)),
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(typeof(is_active)='integer' AND is_active IN (0,1)),
 CHECK(requires_hours=0 OR counts_as_work=1)
);
INSERT INTO attendance_codes(code,name,counts_as_work,requires_hours) VALUES
 ('Я','Явка',1,1),('В','Выходной',0,0),('ОТ','Отпуск',0,0),
 ('Б','Временная нетрудоспособность',0,0),('Н','Неявка с неуточнённой причиной',0,0);
CREATE TABLE timesheets (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 year INTEGER NOT NULL CHECK(typeof(year)='integer' AND year BETWEEN 1 AND 9999),
 month INTEGER NOT NULL CHECK(typeof(month)='integer' AND month BETWEEN 1 AND 12),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(project_id,contractor_id,year,month),
 FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE TABLE timesheet_versions (
 id INTEGER PRIMARY KEY, timesheet_id INTEGER NOT NULL REFERENCES timesheets(id),
 version_no INTEGER NOT NULL CHECK(typeof(version_no)='integer' AND version_no>0),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','approved','cancelled')),
 base_version_id INTEGER REFERENCES timesheet_versions(id),
 correction_reason TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(revision)='integer' AND revision>=0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 submitted_at TEXT, approved_at TEXT, cancelled_at TEXT,
 UNIQUE(timesheet_id,version_no),
 CHECK(base_version_id IS NULL OR (base_version_id!=id AND length(trim(correction_reason))>0)),
 CHECK((status='draft' AND submitted_at IS NULL AND approved_at IS NULL AND cancelled_at IS NULL)
  OR (status='submitted' AND submitted_at IS NOT NULL AND approved_at IS NULL AND cancelled_at IS NULL)
  OR (status='approved' AND submitted_at IS NOT NULL AND approved_at IS NOT NULL AND cancelled_at IS NULL)
  OR (status='cancelled' AND approved_at IS NULL AND cancelled_at IS NOT NULL))
);
CREATE UNIQUE INDEX timesheet_one_open_version ON timesheet_versions(timesheet_id) WHERE status IN ('draft','submitted');
CREATE INDEX timesheet_versions_history ON timesheet_versions(timesheet_id,version_no);
CREATE TABLE timesheet_workers (
 version_id INTEGER NOT NULL REFERENCES timesheet_versions(id), employee_id INTEGER NOT NULL REFERENCES employees(id),
 full_name TEXT NOT NULL CHECK(length(trim(full_name))>0), specialty_name TEXT NOT NULL,
 brigade_number TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', PRIMARY KEY(version_id,employee_id)
);
CREATE TABLE timesheet_entries (
 id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES timesheet_versions(id),
 employee_id INTEGER NOT NULL REFERENCES employees(id),
 work_date TEXT NOT NULL CHECK(length(work_date)=10 AND work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  AND substr(work_date,1,4)>='0001' AND date(work_date,'+0 days') IS NOT NULL AND date(work_date,'+0 days')=work_date),
 attendance_code_id INTEGER NOT NULL REFERENCES attendance_codes(id), code_snapshot TEXT NOT NULL,
 counts_as_work INTEGER NOT NULL CHECK(typeof(counts_as_work)='integer' AND counts_as_work IN (0,1)),
 requires_hours INTEGER NOT NULL CHECK(typeof(requires_hours)='integer' AND requires_hours IN (0,1)),
 hours_nanos INTEGER NOT NULL CHECK(typeof(hours_nanos)='integer' AND hours_nanos BETWEEN 0 AND 24000000000),
 comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(version_id,employee_id,work_date),
 FOREIGN KEY(version_id,employee_id) REFERENCES timesheet_workers(version_id,employee_id),
 CHECK(counts_as_work=1 OR hours_nanos=0), CHECK(requires_hours=0 OR counts_as_work=1)
);
CREATE INDEX timesheet_employee_date ON timesheet_entries(employee_id,work_date,version_id);
CREATE TABLE timesheet_events (
 id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES timesheet_versions(id),
 operation TEXT NOT NULL CHECK(length(trim(operation))>0), recorded_at TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX timesheet_events_history ON timesheet_events(version_id,id);
CREATE TABLE timesheet_operations (
 id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id))>0), version_id INTEGER NOT NULL REFERENCES timesheet_versions(id),
 kind TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE timesheet_previews (
 id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id))>0), version_id INTEGER NOT NULL REFERENCES timesheet_versions(id),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0), kind TEXT NOT NULL,
 filename TEXT NOT NULL DEFAULT '', changes_json TEXT NOT NULL, summary_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'preview' CHECK(status IN ('preview','applied','cancelled')),
 created_at TEXT NOT NULL, completed_at TEXT,
 CHECK((status='preview' AND completed_at IS NULL) OR (status IN ('applied','cancelled') AND completed_at IS NOT NULL))
);
CREATE TRIGGER timesheets_identity BEFORE UPDATE ON timesheets
WHEN NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id OR NEW.contractor_id IS NOT OLD.contractor_id
 OR NEW.year IS NOT OLD.year OR NEW.month IS NOT OLD.month OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Контекст табеля неизменяем'); END;
CREATE TRIGGER timesheets_no_delete BEFORE DELETE ON timesheets
BEGIN SELECT RAISE(ABORT,'История табеля сохраняется; используйте отмену'); END;
CREATE TRIGGER timesheet_version_insert BEFORE INSERT ON timesheet_versions
BEGIN
 SELECT CASE WHEN NEW.status!='draft' OR NEW.version_no!=(SELECT COALESCE(MAX(version_no),0)+1 FROM timesheet_versions WHERE timesheet_id=NEW.timesheet_id)
  THEN RAISE(ABORT,'Новая версия табеля создаётся последовательным черновиком') END;
 SELECT CASE WHEN NEW.base_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM timesheet_versions b
  WHERE b.id=NEW.base_version_id AND b.timesheet_id=NEW.timesheet_id AND b.status='approved')
  THEN RAISE(ABORT,'Основание корректировки не является утверждённой версией этого табеля') END;
 SELECT CASE WHEN NEW.base_version_id IS NOT (SELECT id FROM timesheet_versions WHERE timesheet_id=NEW.timesheet_id AND status='approved' ORDER BY version_no DESC LIMIT 1)
  THEN RAISE(ABORT,'Корректировка должна заменять последнюю утверждённую версию') END;
END;
CREATE TRIGGER timesheet_version_update BEFORE UPDATE ON timesheet_versions
WHEN OLD.status IN ('approved','cancelled') OR NEW.id IS NOT OLD.id OR NEW.timesheet_id IS NOT OLD.timesheet_id
 OR NEW.version_no IS NOT OLD.version_no OR NEW.base_version_id IS NOT OLD.base_version_id
 OR NEW.correction_reason IS NOT OLD.correction_reason OR NEW.created_at IS NOT OLD.created_at OR NEW.revision<OLD.revision
 OR (NEW.status!=OLD.status AND NOT (OLD.status='draft' AND NEW.status IN ('submitted','cancelled')
  OR OLD.status='submitted' AND NEW.status IN ('draft','approved','cancelled')))
BEGIN SELECT RAISE(ABORT,'Утверждённая версия неизменяема; проверьте переход статуса табеля'); END;
CREATE TRIGGER timesheet_version_no_delete BEFORE DELETE ON timesheet_versions
BEGIN SELECT RAISE(ABORT,'Версии табеля не удаляются'); END;
CREATE TRIGGER timesheet_workers_insert BEFORE INSERT ON timesheet_workers
WHEN NOT EXISTS(SELECT 1 FROM timesheet_versions v JOIN timesheets t ON t.id=v.timesheet_id
 JOIN employee_projects ep ON ep.project_id=t.project_id AND ep.employee_id=NEW.employee_id
 JOIN employees e ON e.id=ep.employee_id WHERE v.id=NEW.version_id AND v.status='draft'
 AND (e.contractor_id=t.contractor_id OR EXISTS(SELECT 1 FROM timesheet_workers b WHERE b.version_id=v.base_version_id AND b.employee_id=NEW.employee_id)))
BEGIN SELECT RAISE(ABORT,'Работник не соответствует объекту, подрядчику или редактируемой версии'); END;
CREATE TRIGGER timesheet_workers_update BEFORE UPDATE ON timesheet_workers
WHEN NEW.version_id IS NOT OLD.version_id OR NEW.employee_id IS NOT OLD.employee_id
 OR NOT EXISTS(SELECT 1 FROM timesheet_versions WHERE id=OLD.version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'Снимок работника можно менять только в черновике'); END;
CREATE TRIGGER timesheet_workers_delete BEFORE DELETE ON timesheet_workers
WHEN NOT EXISTS(SELECT 1 FROM timesheet_versions WHERE id=OLD.version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'Снимок работника зафиксирован'); END;
CREATE TRIGGER timesheet_entries_insert BEFORE INSERT ON timesheet_entries
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM timesheet_versions v JOIN timesheets t ON t.id=v.timesheet_id
  WHERE v.id=NEW.version_id AND v.status='draft' AND substr(NEW.work_date,1,7)=printf('%04d-%02d',t.year,t.month))
  THEN RAISE(ABORT,'Дата записи вне месяца или версия недоступна для изменения') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM attendance_codes c WHERE c.id=NEW.attendance_code_id
  AND c.code=NEW.code_snapshot AND c.counts_as_work=NEW.counts_as_work AND c.requires_hours=NEW.requires_hours)
  AND NOT EXISTS(SELECT 1 FROM timesheet_versions v JOIN timesheet_entries b ON b.version_id=v.base_version_id
   WHERE v.id=NEW.version_id AND b.employee_id=NEW.employee_id AND b.work_date=NEW.work_date
   AND b.attendance_code_id=NEW.attendance_code_id AND b.code_snapshot=NEW.code_snapshot
   AND b.counts_as_work=NEW.counts_as_work AND b.requires_hours=NEW.requires_hours)
  THEN RAISE(ABORT,'Снимок табельного кода не соответствует справочнику или основанию корректировки') END;
END;
CREATE TRIGGER timesheet_entries_update BEFORE UPDATE ON timesheet_entries
BEGIN
 SELECT CASE WHEN NEW.id IS NOT OLD.id OR NEW.version_id IS NOT OLD.version_id OR NEW.employee_id IS NOT OLD.employee_id
  OR NEW.work_date IS NOT OLD.work_date OR NEW.created_at IS NOT OLD.created_at
  OR NOT EXISTS(SELECT 1 FROM timesheet_versions WHERE id=OLD.version_id AND status='draft')
  THEN RAISE(ABORT,'Запись времени можно менять только в черновике своего табеля') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM attendance_codes c WHERE c.id=NEW.attendance_code_id
  AND c.code=NEW.code_snapshot AND c.counts_as_work=NEW.counts_as_work AND c.requires_hours=NEW.requires_hours)
  AND NOT (NEW.attendance_code_id=OLD.attendance_code_id AND NEW.code_snapshot=OLD.code_snapshot
   AND NEW.counts_as_work=OLD.counts_as_work AND NEW.requires_hours=OLD.requires_hours)
  THEN RAISE(ABORT,'Снимок табельного кода не соответствует справочнику') END;
END;
CREATE TRIGGER timesheet_entries_delete BEFORE DELETE ON timesheet_entries
WHEN NOT EXISTS(SELECT 1 FROM timesheet_versions WHERE id=OLD.version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'Записи утверждённого табеля не удаляются'); END;
CREATE TRIGGER timesheet_events_no_update BEFORE UPDATE ON timesheet_events
BEGIN SELECT RAISE(ABORT,'Журнал табеля неизменяем'); END;
CREATE TRIGGER timesheet_events_no_delete BEFORE DELETE ON timesheet_events
BEGIN SELECT RAISE(ABORT,'Журнал табеля неизменяем'); END;
CREATE TRIGGER timesheet_operations_no_update BEFORE UPDATE ON timesheet_operations
BEGIN SELECT RAISE(ABORT,'Завершённая операция табеля неизменяема'); END;
CREATE TRIGGER timesheet_operations_no_delete BEFORE DELETE ON timesheet_operations
BEGIN SELECT RAISE(ABORT,'Завершённая операция табеля неизменяема'); END;
CREATE TRIGGER timesheet_previews_insert BEFORE INSERT ON timesheet_previews
WHEN NEW.status!='preview' OR NOT EXISTS(SELECT 1 FROM timesheet_versions WHERE id=NEW.version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'Предварительный просмотр доступен только для черновика'); END;
CREATE TRIGGER timesheet_previews_update BEFORE UPDATE ON timesheet_previews
WHEN OLD.status!='preview' OR NEW.status NOT IN ('applied','cancelled') OR NEW.id IS NOT OLD.id
 OR NEW.version_id IS NOT OLD.version_id OR NEW.revision IS NOT OLD.revision OR NEW.kind IS NOT OLD.kind
 OR NEW.filename IS NOT OLD.filename OR NEW.changes_json IS NOT OLD.changes_json
 OR NEW.summary_json IS NOT OLD.summary_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Предварительный просмотр уже завершён или изменён'); END;
CREATE TRIGGER timesheet_previews_no_delete BEFORE DELETE ON timesheet_previews
BEGIN SELECT RAISE(ABORT,'История предварительного просмотра сохраняется'); END;
