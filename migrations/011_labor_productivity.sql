-- Analytical allocations only. No copies of primary time or volume journals.
CREATE TABLE labor_operations (
 id TEXT PRIMARY KEY NOT NULL, project_id INTEGER NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('hours','volume')), fingerprint TEXT NOT NULL,
 recorded_at TEXT NOT NULL CHECK(length(recorded_at)=27 AND substr(recorded_at,27,1)='Z'),
 reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX labor_operations_project_time ON labor_operations(project_id,recorded_at,id);
CREATE TABLE labor_hour_allocations (
 id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES labor_operations(id),
 employee_id INTEGER NOT NULL REFERENCES employees(id), work_date TEXT NOT NULL,
 work_item_id INTEGER NOT NULL REFERENCES work_items(id), plan_version_id INTEGER NOT NULL,
 timesheet_entry_id INTEGER NOT NULL REFERENCES timesheet_entries(id),
 brigade_id INTEGER REFERENCES brigades(id), hours_nanos INTEGER NOT NULL
 CHECK(typeof(hours_nanos)='integer' AND hours_nanos BETWEEN 0 AND 24000000000),
 revision_no INTEGER NOT NULL CHECK(revision_no>0), previous_id INTEGER UNIQUE REFERENCES labor_hour_allocations(id),
 UNIQUE(employee_id,work_date,work_item_id,revision_no),
 FOREIGN KEY(plan_version_id,work_item_id) REFERENCES plan_lines(plan_version_id,work_item_id),
 CHECK((revision_no=1 AND previous_id IS NULL AND hours_nanos>0) OR (revision_no>1 AND previous_id IS NOT NULL))
);
CREATE INDEX labor_hours_operation ON labor_hour_allocations(operation_id);
CREATE INDEX labor_hours_day_employee ON labor_hour_allocations(work_date,employee_id,work_item_id,revision_no);
CREATE TABLE labor_volume_attributions (
 id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES labor_operations(id),
 fact_event_id INTEGER NOT NULL REFERENCES fact_events(id), contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 brigade_id INTEGER REFERENCES brigades(id), employee_id INTEGER REFERENCES employees(id),
 plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id), quantity TEXT NOT NULL
 CHECK(typeof(quantity)='text' AND length(quantity) BETWEEN 1 AND 28 AND quantity NOT GLOB '*[^0-9.]*'
  AND length(quantity)-length(replace(quantity,'.',''))<=1
  AND substr(quantity,1,1) GLOB '[0-9]' AND substr(quantity,-1,1) GLOB '[0-9]'),
 revision_no INTEGER NOT NULL CHECK(revision_no>0), previous_id INTEGER UNIQUE REFERENCES labor_volume_attributions(id),
 CHECK((revision_no=1 AND previous_id IS NULL AND quantity!='0') OR (revision_no>1 AND previous_id IS NOT NULL))
);
CREATE UNIQUE INDEX labor_volume_identity ON labor_volume_attributions(fact_event_id,contractor_id,COALESCE(brigade_id,0),COALESCE(employee_id,0),revision_no);
CREATE INDEX labor_volume_operation ON labor_volume_attributions(operation_id);
CREATE TRIGGER labor_hours_context BEFORE INSERT ON labor_hour_allocations BEGIN
 SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM labor_operations o JOIN work_items w ON w.id=NEW.work_item_id JOIN buildings b ON b.id=w.building_id
 JOIN plan_versions p ON p.id=NEW.plan_version_id
 JOIN timesheet_entries e ON e.id=NEW.timesheet_entry_id JOIN timesheet_versions v ON v.id=e.version_id
 JOIN timesheets t ON t.id=v.timesheet_id
 WHERE o.id=NEW.operation_id AND o.kind='hours' AND b.project_id=o.project_id AND p.project_id=o.project_id
 AND p.status='APPROVED' AND p.discipline=w.discipline AND p.approved_at<=o.recorded_at
 AND t.project_id=o.project_id AND v.status='approved' AND v.approved_at<=o.recorded_at
 AND e.employee_id=NEW.employee_id AND e.work_date=NEW.work_date AND e.counts_as_work=1
 AND NEW.hours_nanos<=e.hours_nanos
 AND (NEW.brigade_id IS NULL OR EXISTS(SELECT 1 FROM brigades g WHERE g.id=NEW.brigade_id AND g.contractor_id=t.contractor_id)))
 THEN RAISE(ABORT,'Распределение не соответствует объекту, плану или утверждённому табелю') END;
 SELECT CASE WHEN NEW.previous_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM labor_hour_allocations a JOIN labor_operations o ON o.id=a.operation_id
 JOIN labor_operations n ON n.id=NEW.operation_id WHERE a.id=NEW.previous_id AND a.employee_id=NEW.employee_id
 AND a.work_date=NEW.work_date AND a.work_item_id=NEW.work_item_id AND a.revision_no+1=NEW.revision_no
 AND o.recorded_at<=n.recorded_at AND length(trim(n.reason))>0
 AND (SELECT t.contractor_id FROM timesheet_entries e JOIN timesheet_versions v ON v.id=e.version_id JOIN timesheets t ON t.id=v.timesheet_id WHERE e.id=a.timesheet_entry_id)
 = (SELECT t.contractor_id FROM timesheet_entries e JOIN timesheet_versions v ON v.id=e.version_id JOIN timesheets t ON t.id=v.timesheet_id WHERE e.id=NEW.timesheet_entry_id))
 THEN RAISE(ABORT,'Некорректная ревизия распределения') END;
END;
CREATE TRIGGER labor_volume_context BEFORE INSERT ON labor_volume_attributions BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM labor_operations o JOIN fact_events f ON f.id=NEW.fact_event_id
 JOIN work_items w ON w.id=f.work_item_id JOIN buildings b ON b.id=w.building_id
 JOIN plan_versions p ON p.id=NEW.plan_version_id
 JOIN plan_lines l ON l.plan_version_id=p.id AND l.work_item_id=w.id
 JOIN contractor_projects c ON c.contractor_id=NEW.contractor_id AND c.project_id=o.project_id
 WHERE o.id=NEW.operation_id AND o.kind='volume' AND b.project_id=o.project_id AND p.project_id=o.project_id
 AND p.discipline=w.discipline AND p.status='APPROVED' AND p.approved_at<=o.recorded_at
 AND f.correction_of_id IS NULL AND f.recorded_at<=o.recorded_at AND substr(f.quantity_delta,1,1)!='-'
 AND (NEW.brigade_id IS NULL OR EXISTS(SELECT 1 FROM brigades g WHERE g.id=NEW.brigade_id AND g.contractor_id=NEW.contractor_id))
 AND (NEW.employee_id IS NULL OR NEW.previous_id IS NOT NULL OR EXISTS(SELECT 1 FROM employees e WHERE e.id=NEW.employee_id AND e.contractor_id=NEW.contractor_id)))
 THEN RAISE(ABORT,'Атрибуция не соответствует объекту, исполнителю или плану') END;
 SELECT CASE WHEN NEW.previous_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM labor_volume_attributions a JOIN labor_operations o ON o.id=a.operation_id
 JOIN labor_operations n ON n.id=NEW.operation_id WHERE a.id=NEW.previous_id AND a.fact_event_id=NEW.fact_event_id
 AND a.contractor_id=NEW.contractor_id AND a.brigade_id IS NEW.brigade_id AND a.employee_id IS NEW.employee_id
 AND a.revision_no+1=NEW.revision_no AND o.recorded_at<=n.recorded_at AND length(trim(n.reason))>0)
 THEN RAISE(ABORT,'Некорректная ревизия атрибуции') END;
END;
CREATE TRIGGER labor_operations_no_update BEFORE UPDATE ON labor_operations BEGIN SELECT RAISE(ABORT,'История неизменяема'); END;
CREATE TRIGGER labor_operations_no_delete BEFORE DELETE ON labor_operations BEGIN SELECT RAISE(ABORT,'История сохраняется'); END;
CREATE TRIGGER labor_hours_no_update BEFORE UPDATE ON labor_hour_allocations BEGIN SELECT RAISE(ABORT,'Создайте корректирующую ревизию'); END;
CREATE TRIGGER labor_hours_no_delete BEFORE DELETE ON labor_hour_allocations BEGIN SELECT RAISE(ABORT,'История сохраняется'); END;
CREATE TRIGGER labor_volume_no_update BEFORE UPDATE ON labor_volume_attributions BEGIN SELECT RAISE(ABORT,'Создайте корректирующую ревизию'); END;
CREATE TRIGGER labor_volume_no_delete BEFORE DELETE ON labor_volume_attributions BEGIN SELECT RAISE(ABORT,'История сохраняется'); END;
CREATE INDEX labor_fact_correction_lookup ON fact_events(correction_of_id);
