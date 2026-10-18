-- Additive links: journals remain the sole source of executed/confirmed volumes.
ALTER TABLE fact_events ADD COLUMN work_order_line_id INTEGER REFERENCES work_order_lines(id);
ALTER TABLE fact_events ADD COLUMN overrun_reason TEXT NOT NULL DEFAULT '' CHECK(length(overrun_reason)<=2000);
ALTER TABLE confirmation_events ADD COLUMN work_order_line_id INTEGER REFERENCES work_order_lines(id);
ALTER TABLE work_orders ADD COLUMN workflow_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX fact_order_line ON fact_events(work_order_line_id,work_date,recorded_at);
CREATE INDEX confirmation_order_line ON confirmation_events(work_order_line_id,confirmed_date,recorded_at);
-- 006 approvals intentionally permit only one issuance. Keep those rows and
-- extend that immutable audit mechanism instead of rebuilding the table.
CREATE TABLE work_order_workflow_actions (
    id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    status_history_id INTEGER REFERENCES work_order_status_history(id),
    action TEXT NOT NULL CHECK(action IN ('submitted','accepted','closed')),
    employee_id INTEGER NOT NULL REFERENCES employees(id), employee_name_snapshot TEXT NOT NULL,
    performed_at TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL, event_signature TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE CHECK(length(event_key) BETWEEN 1 AND 100)
);
CREATE INDEX work_order_actions_time ON work_order_workflow_actions(work_order_id,performed_at,id);
CREATE TABLE work_order_line_reviews (
    id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL REFERENCES work_order_workflow_actions(id),
    work_order_line_id INTEGER NOT NULL REFERENCES work_order_lines(id),
    checked INTEGER NOT NULL CHECK(checked IN (0,1)), comment TEXT NOT NULL DEFAULT '',
    UNIQUE(action_id,work_order_line_id)
);
CREATE TRIGGER order_workflow_action_scope BEFORE INSERT ON work_order_workflow_actions
WHEN NOT EXISTS(SELECT 1 FROM work_orders o JOIN employees e ON e.id=NEW.employee_id
    WHERE o.id=NEW.work_order_id AND e.project_id=o.project_id AND o.order_number IS NOT NULL AND o.status=NEW.action)
    OR (NEW.status_history_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM work_order_status_history h
        WHERE h.id=NEW.status_history_id AND h.work_order_id=NEW.work_order_id AND h.new_status=NEW.action))
BEGIN SELECT RAISE(ABORT,'Согласование не соответствует наряду или ответственному лицу'); END;
CREATE TRIGGER order_line_review_scope BEFORE INSERT ON work_order_line_reviews
WHEN NOT EXISTS(SELECT 1 FROM work_order_workflow_actions a JOIN work_order_lines l ON l.work_order_id=a.work_order_id
    WHERE a.id=NEW.action_id AND a.action='accepted' AND l.id=NEW.work_order_line_id)
BEGIN SELECT RAISE(ABORT,'Проверка строки не соответствует наряду'); END;

DROP TRIGGER work_orders_update_guard;
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
DROP TRIGGER work_order_status_changed;
CREATE TRIGGER work_order_status_changed AFTER UPDATE OF status ON work_orders WHEN NEW.status!=OLD.status
BEGIN INSERT INTO work_order_status_history(work_order_id,old_status,new_status,changed_at,reason)
    VALUES(NEW.id,OLD.status,NEW.status,NEW.updated_at,
        CASE WHEN NEW.status='cancelled' THEN NEW.cancellation_reason WHEN NEW.status='issued' THEN 'Выдача наряда' ELSE NEW.workflow_reason END); END;
CREATE TRIGGER fact_order_starts_work AFTER INSERT ON fact_events
WHEN NEW.work_order_line_id IS NOT NULL AND NEW.correction_of_id IS NULL AND substr(NEW.quantity_delta,1,1)!='-'
BEGIN UPDATE work_orders SET status='in_progress',updated_at=NEW.recorded_at,workflow_reason='Первое выполнение, связанное с нарядом'
    WHERE id=(SELECT work_order_id FROM work_order_lines WHERE id=NEW.work_order_line_id) AND status='issued'; END;

CREATE TRIGGER fact_events_order_scope BEFORE INSERT ON fact_events
BEGIN
    SELECT CASE WHEN NEW.source_type='work_order' AND NEW.work_order_line_id IS NULL
        THEN RAISE(ABORT,'Источник наряд требует ссылку на строку наряда') END;
    SELECT CASE WHEN NEW.correction_of_id IS NOT NULL AND NEW.work_order_line_id IS NOT
        (SELECT work_order_line_id FROM fact_events WHERE id=NEW.correction_of_id)
        THEN RAISE(ABORT,'Корректировка сохраняет связь исходного события с нарядом') END;
    SELECT CASE WHEN NEW.work_order_line_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id
        JOIN work_items w ON w.id=NEW.work_item_id JOIN buildings b ON b.id=w.building_id
        WHERE l.id=NEW.work_order_line_id AND l.work_item_id=NEW.work_item_id AND o.project_id=b.project_id
        AND o.order_number IS NOT NULL AND NEW.recorded_at>=o.issued_at AND l.unit_snapshot=NEW.unit
        AND ((NEW.correction_of_id IS NULL AND o.status IN ('issued','in_progress')) OR
            (NEW.correction_of_id IS NOT NULL AND o.status IN ('issued','in_progress','submitted','accepted','closed','cancelled'))))
        THEN RAISE(ABORT,'Наряд или его строка недоступны для регистрации события') END;
END;
CREATE TRIGGER confirmation_events_order_scope BEFORE INSERT ON confirmation_events
BEGIN
    SELECT CASE WHEN NEW.source_type='work_order' AND NEW.work_order_line_id IS NULL
        THEN RAISE(ABORT,'Источник наряд требует ссылку на строку наряда') END;
    SELECT CASE WHEN NEW.correction_of_id IS NOT NULL AND NEW.work_order_line_id IS NOT
        (SELECT work_order_line_id FROM confirmation_events WHERE id=NEW.correction_of_id)
        THEN RAISE(ABORT,'Корректировка сохраняет связь исходного события с нарядом') END;
    SELECT CASE WHEN NEW.work_order_line_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id
        JOIN work_items w ON w.id=NEW.work_item_id JOIN buildings b ON b.id=w.building_id
        WHERE l.id=NEW.work_order_line_id AND l.work_item_id=NEW.work_item_id AND o.project_id=b.project_id
        AND o.order_number IS NOT NULL AND NEW.recorded_at>=o.issued_at AND l.unit_snapshot=NEW.unit
        AND ((NEW.correction_of_id IS NULL AND o.status IN ('issued','in_progress','submitted')) OR
            (NEW.correction_of_id IS NOT NULL AND o.status IN ('issued','in_progress','submitted','accepted','closed','cancelled'))))
        THEN RAISE(ABORT,'Наряд или его строка недоступны для регистрации события') END;
END;
CREATE TRIGGER work_order_workflow_actions_no_update BEFORE UPDATE ON work_order_workflow_actions
BEGIN SELECT RAISE(ABORT,'История согласований неизменяема'); END;
CREATE TRIGGER work_order_workflow_actions_no_delete BEFORE DELETE ON work_order_workflow_actions
BEGIN SELECT RAISE(ABORT,'История согласований неизменяема'); END;
CREATE TRIGGER work_order_line_reviews_no_update BEFORE UPDATE ON work_order_line_reviews
BEGIN SELECT RAISE(ABORT,'История согласований неизменяема'); END;
CREATE TRIGGER work_order_line_reviews_no_delete BEFORE DELETE ON work_order_line_reviews
BEGIN SELECT RAISE(ABORT,'История согласований неизменяема'); END;
DROP TRIGGER audit_fact_events_insert;
CREATE TRIGGER audit_fact_events_insert AFTER INSERT ON fact_events BEGIN
INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('fact_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.work_date=' || quote(NEW.work_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit) || '; ' || 'NEW.source_type=' || quote(NEW.source_type) || '; ' || 'NEW.work_order_line_id=' || quote(NEW.work_order_line_id) || '; ' || 'NEW.overrun_reason=' || quote(NEW.overrun_reason)); END;
DROP TRIGGER audit_confirmation_events_insert;
CREATE TRIGGER audit_confirmation_events_insert AFTER INSERT ON confirmation_events BEGIN
INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('confirmation_events',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_item_id=' || quote(NEW.work_item_id) || '; ' || 'NEW.quantity_delta=' || quote(NEW.quantity_delta) || '; ' || 'NEW.confirmed_date=' || quote(NEW.confirmed_date) || '; ' || 'NEW.recorded_at=' || quote(NEW.recorded_at) || '; ' || 'NEW.document_ref=' || quote(NEW.document_ref) || '; ' || 'NEW.correction_of_id=' || quote(NEW.correction_of_id) || '; ' || 'NEW.event_key=' || quote(NEW.event_key) || '; ' || 'NEW.unit=' || quote(NEW.unit) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.source_type=' || quote(NEW.source_type) || '; ' || 'NEW.work_order_line_id=' || quote(NEW.work_order_line_id)); END;
CREATE TRIGGER audit_work_order_workflow_actions_insert AFTER INSERT ON work_order_workflow_actions BEGIN
INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('work_order_workflow_actions',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.work_order_id=' || quote(NEW.work_order_id) || '; ' || 'NEW.status_history_id=' || quote(NEW.status_history_id) || '; ' || 'NEW.action=' || quote(NEW.action) || '; ' || 'NEW.employee_id=' || quote(NEW.employee_id) || '; ' || 'NEW.employee_name_snapshot=' || quote(NEW.employee_name_snapshot) || '; ' || 'NEW.performed_at=' || quote(NEW.performed_at) || '; ' || 'NEW.comment=' || quote(NEW.comment) || '; ' || 'NEW.snapshot=' || quote(NEW.snapshot) || '; ' || 'NEW.event_signature=' || quote(NEW.event_signature) || '; ' || 'NEW.event_key=' || quote(NEW.event_key)); END;
CREATE TRIGGER audit_work_order_line_reviews_insert AFTER INSERT ON work_order_line_reviews BEGIN
INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('work_order_line_reviews',NEW.id,'INSERT',strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z','NEW.id=' || quote(NEW.id) || '; ' || 'NEW.action_id=' || quote(NEW.action_id) || '; ' || 'NEW.work_order_line_id=' || quote(NEW.work_order_line_id) || '; ' || 'NEW.checked=' || quote(NEW.checked) || '; ' || 'NEW.comment=' || quote(NEW.comment)); END;
CREATE TRIGGER audit_order_workflow_reason AFTER UPDATE OF workflow_reason ON work_orders WHEN OLD.workflow_reason!=NEW.workflow_reason BEGIN INSERT INTO audit_log(entity_type,entity_id,operation,recorded_at,details) VALUES ('work_orders',NEW.id,'UPDATE',NEW.updated_at,'OLD.workflow_reason=' || quote(OLD.workflow_reason) || '; NEW.workflow_reason=' || quote(NEW.workflow_reason)); END;
