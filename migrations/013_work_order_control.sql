-- Stage4.5: append-only order control; no primary journal or issued snapshot changes.
CREATE TABLE work_order_control_commands (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100), project_id INTEGER NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('transfer','cancel_transfer','writeoff','revoke_writeoff','match')),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64), result_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE work_order_transfers (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),
 source_order_id INTEGER NOT NULL REFERENCES work_orders(id),target_order_id INTEGER NOT NULL UNIQUE REFERENCES work_orders(id),
 command_id TEXT NOT NULL UNIQUE REFERENCES work_order_control_commands(id),reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 4000),
 comment TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,target_shift_date TEXT NOT NULL,
 source_executor_snapshot TEXT NOT NULL,target_executor_snapshot TEXT NOT NULL,CHECK(source_order_id!=target_order_id)
);
CREATE INDEX order_transfers_source ON work_order_transfers(source_order_id,created_at,id);
CREATE TABLE work_order_transfer_lines (
 id INTEGER PRIMARY KEY,transfer_id INTEGER NOT NULL REFERENCES work_order_transfers(id),
 source_line_id INTEGER NOT NULL REFERENCES work_order_lines(id),target_line_id INTEGER NOT NULL UNIQUE REFERENCES work_order_lines(id),
 quantity TEXT NOT NULL CHECK(typeof(quantity)='text' AND quantity!='0' AND instr(quantity,char(0))=0
        AND length(quantity) BETWEEN 1 AND 28 AND quantity NOT GLOB '*[^0-9.]*'
        AND length(quantity)-length(replace(quantity,'.',''))<=1
        AND length(CASE WHEN instr(quantity,'.')=0 THEN quantity ELSE substr(quantity,1,instr(quantity,'.')-1) END) BETWEEN 1 AND 18
        AND (quantity='0' OR substr(quantity,1,1) BETWEEN '1' AND '9' OR substr(quantity,1,2)='0.')
        AND (instr(quantity,'.')=0 OR (length(quantity)-instr(quantity,'.') BETWEEN 1 AND 9 AND substr(quantity,-1) BETWEEN '1' AND '9'))),unit TEXT NOT NULL,UNIQUE(transfer_id,source_line_id),CHECK(source_line_id!=target_line_id)
);
CREATE INDEX order_transfer_lines_source ON work_order_transfer_lines(source_line_id,transfer_id);
CREATE TABLE work_order_transfer_cancellations (
 id INTEGER PRIMARY KEY,transfer_id INTEGER NOT NULL UNIQUE REFERENCES work_order_transfers(id),
 command_id TEXT NOT NULL UNIQUE REFERENCES work_order_control_commands(id),reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 4000),recorded_at TEXT NOT NULL
);
CREATE TABLE work_order_resolutions (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),line_id INTEGER NOT NULL REFERENCES work_order_lines(id),
 kind TEXT NOT NULL CHECK(kind IN ('writeoff','revoke')),quantity TEXT NOT NULL CHECK(typeof(quantity)='text' AND quantity!='0' AND instr(quantity,char(0))=0
        AND length(quantity) BETWEEN 1 AND 28 AND quantity NOT GLOB '*[^0-9.]*'
        AND length(quantity)-length(replace(quantity,'.',''))<=1
        AND length(CASE WHEN instr(quantity,'.')=0 THEN quantity ELSE substr(quantity,1,instr(quantity,'.')-1) END) BETWEEN 1 AND 18
        AND (quantity='0' OR substr(quantity,1,1) BETWEEN '1' AND '9' OR substr(quantity,1,2)='0.')
        AND (instr(quantity,'.')=0 OR (length(quantity)-instr(quantity,'.') BETWEEN 1 AND 9 AND substr(quantity,-1) BETWEEN '1' AND '9'))),reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 4000),
 recorded_at TEXT NOT NULL,command_id TEXT NOT NULL UNIQUE REFERENCES work_order_control_commands(id),
 reverses_id INTEGER UNIQUE REFERENCES work_order_resolutions(id),CHECK((kind='writeoff' AND reverses_id IS NULL) OR (kind='revoke' AND reverses_id IS NOT NULL))
);
CREATE INDEX order_resolutions_line ON work_order_resolutions(line_id,recorded_at,id);
CREATE TABLE work_order_volume_matches (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),line_id INTEGER NOT NULL REFERENCES work_order_lines(id),
 fact_event_id INTEGER NOT NULL REFERENCES fact_events(id),confirmation_event_id INTEGER NOT NULL REFERENCES confirmation_events(id),
 quantity TEXT NOT NULL CHECK(typeof(quantity)='text' AND instr(quantity,char(0))=0
        AND length(quantity) BETWEEN 1 AND 28 AND quantity NOT GLOB '*[^0-9.]*'
        AND length(quantity)-length(replace(quantity,'.',''))<=1
        AND length(CASE WHEN instr(quantity,'.')=0 THEN quantity ELSE substr(quantity,1,instr(quantity,'.')-1) END) BETWEEN 1 AND 18
        AND (quantity='0' OR substr(quantity,1,1) BETWEEN '1' AND '9' OR substr(quantity,1,2)='0.')
        AND (instr(quantity,'.')=0 OR (length(quantity)-instr(quantity,'.') BETWEEN 1 AND 9 AND substr(quantity,-1) BETWEEN '1' AND '9'))),revision_no INTEGER NOT NULL CHECK(revision_no>0),previous_id INTEGER UNIQUE REFERENCES work_order_volume_matches(id),
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 4000),recorded_at TEXT NOT NULL,
 command_id TEXT NOT NULL UNIQUE REFERENCES work_order_control_commands(id),UNIQUE(line_id,fact_event_id,confirmation_event_id,revision_no)
);
CREATE INDEX order_matches_line ON work_order_volume_matches(line_id,recorded_at,revision_no);
CREATE TRIGGER order_transfer_scope BEFORE INSERT ON work_order_transfers
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM work_orders s JOIN work_orders t ON t.id=NEW.target_order_id
   JOIN work_order_control_commands c ON c.id=NEW.command_id WHERE s.id=NEW.source_order_id AND s.project_id=NEW.project_id
   AND t.project_id=NEW.project_id AND c.project_id=NEW.project_id AND c.kind='transfer' AND s.order_number IS NOT NULL
   AND s.status!='cancelled' AND t.status='draft' AND t.shift_date=NEW.target_shift_date AND t.shift_date>=s.shift_date)
   THEN RAISE(ABORT,'Несовместимый источник или дочерний черновик переноса') END;
 SELECT CASE WHEN EXISTS(WITH RECURSIVE descendants(id) AS (
   SELECT target_order_id FROM work_order_transfers WHERE source_order_id=NEW.target_order_id
   UNION SELECT t.target_order_id FROM work_order_transfers t JOIN descendants d ON t.source_order_id=d.id)
   SELECT 1 FROM descendants WHERE id=NEW.source_order_id)
   THEN RAISE(ABORT,'Циклическая цепочка переносов запрещена') END;
END;
CREATE TRIGGER order_transfer_line_scope BEFORE INSERT ON work_order_transfer_lines
WHEN NOT EXISTS(SELECT 1 FROM work_order_transfers t JOIN work_order_lines s ON s.id=NEW.source_line_id
 JOIN work_order_lines d ON d.id=NEW.target_line_id WHERE t.id=NEW.transfer_id AND s.work_order_id=t.source_order_id
 AND d.work_order_id=t.target_order_id AND s.work_item_id=d.work_item_id AND s.unit_snapshot=d.unit_snapshot
 AND NEW.unit=s.unit_snapshot AND d.planned_quantity=NEW.quantity)
BEGIN SELECT RAISE(ABORT,'Перенос требует совместимые позиции, единицы и количество'); END;
CREATE TRIGGER order_transfer_cancel_scope BEFORE INSERT ON work_order_transfer_cancellations
WHEN NOT EXISTS(SELECT 1 FROM work_order_transfers t JOIN work_orders d ON d.id=t.target_order_id
 JOIN work_order_control_commands c ON c.id=NEW.command_id WHERE t.id=NEW.transfer_id AND d.status='draft'
 AND c.kind='cancel_transfer' AND c.project_id=t.project_id AND NEW.recorded_at>=t.created_at)
 OR EXISTS(SELECT 1 FROM work_order_transfers t JOIN work_order_transfers child ON child.source_order_id=t.target_order_id WHERE t.id=NEW.transfer_id)
BEGIN SELECT RAISE(ABORT,'Отмена переноса недоступна: дочерний наряд выдан или имеет зависимости'); END;
CREATE TRIGGER order_resolution_scope BEFORE INSERT ON work_order_resolutions
WHEN NOT EXISTS(SELECT 1 FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id
 JOIN work_order_control_commands c ON c.id=NEW.command_id WHERE l.id=NEW.line_id AND o.project_id=NEW.project_id
 AND c.project_id=NEW.project_id AND o.order_number IS NOT NULL AND o.status!='cancelled'
 AND c.kind=CASE WHEN NEW.kind='writeoff' THEN 'writeoff' ELSE 'revoke_writeoff' END)
 OR (NEW.kind='revoke' AND NOT EXISTS(SELECT 1 FROM work_order_resolutions r WHERE r.id=NEW.reverses_id AND r.kind='writeoff'
 AND r.line_id=NEW.line_id AND r.quantity=NEW.quantity AND r.project_id=NEW.project_id AND NEW.recorded_at>=r.recorded_at))
BEGIN SELECT RAISE(ABORT,'Решение об остатке не соответствует исходной строке'); END;
CREATE TRIGGER order_match_scope BEFORE INSERT ON work_order_volume_matches
WHEN NOT EXISTS(SELECT 1 FROM work_order_lines l JOIN work_orders o ON o.id=l.work_order_id
 JOIN fact_events f ON f.id=NEW.fact_event_id JOIN confirmation_events c ON c.id=NEW.confirmation_event_id
 JOIN work_order_control_commands op ON op.id=NEW.command_id
 WHERE l.id=NEW.line_id AND o.project_id=NEW.project_id AND o.order_number IS NOT NULL
 AND f.work_order_line_id=l.id AND c.work_order_line_id=l.id AND f.work_item_id=l.work_item_id AND c.work_item_id=l.work_item_id
 AND f.correction_of_id IS NULL AND c.correction_of_id IS NULL AND f.unit=c.unit AND f.unit=l.unit_snapshot
 AND NEW.recorded_at>=f.recorded_at AND NEW.recorded_at>=c.recorded_at AND op.project_id=NEW.project_id AND op.kind='match')
 OR (NEW.previous_id IS NULL AND NEW.revision_no!=1)
 OR (NEW.previous_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM work_order_volume_matches p WHERE p.id=NEW.previous_id
 AND p.line_id=NEW.line_id AND p.fact_event_id=NEW.fact_event_id AND p.confirmation_event_id=NEW.confirmation_event_id
 AND p.project_id=NEW.project_id AND NEW.revision_no=p.revision_no+1 AND NEW.recorded_at>=p.recorded_at))
BEGIN SELECT RAISE(ABORT,'Сопоставление требует связанные с одной строкой независимые источники'); END;
CREATE TRIGGER order_inherited_line_immutable BEFORE UPDATE ON work_order_lines
WHEN EXISTS(SELECT 1 FROM work_order_transfer_lines t WHERE t.target_line_id=OLD.id)
 AND (NEW.id!=OLD.id OR NEW.work_order_id!=OLD.work_order_id OR NEW.work_item_id!=OLD.work_item_id
 OR NEW.planned_quantity!=OLD.planned_quantity OR NEW.unit_snapshot!=OLD.unit_snapshot)
BEGIN SELECT RAISE(ABORT,'Объём и происхождение наследуемой строки неизменяемы; отмените перенос'); END;
CREATE TRIGGER order_active_transfer_cancel_guard BEFORE UPDATE OF status ON work_orders
WHEN NEW.status='cancelled' AND (EXISTS(SELECT 1 FROM work_order_transfers t WHERE t.source_order_id=OLD.id
 AND NOT EXISTS(SELECT 1 FROM work_order_transfer_cancellations c WHERE c.transfer_id=t.id))
 OR EXISTS(SELECT 1 FROM work_order_transfers t WHERE t.target_order_id=OLD.id
 AND NOT EXISTS(SELECT 1 FROM work_order_transfer_cancellations c WHERE c.transfer_id=t.id)))
BEGIN SELECT RAISE(ABORT,'Отмена наряда требует безопасной отмены связанных переносов'); END;
CREATE TRIGGER work_order_control_commands_no_update BEFORE UPDATE ON work_order_control_commands
WHEN NOT (OLD.result_json='null' AND NEW.result_json!='null' AND NEW.id=OLD.id
 AND NEW.project_id=OLD.project_id AND NEW.kind=OLD.kind AND NEW.fingerprint=OLD.fingerprint AND NEW.recorded_at=OLD.recorded_at)
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_control_commands_no_delete BEFORE DELETE ON work_order_control_commands
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfers_no_update BEFORE UPDATE ON work_order_transfers
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfers_no_delete BEFORE DELETE ON work_order_transfers
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfer_lines_no_update BEFORE UPDATE ON work_order_transfer_lines
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfer_lines_no_delete BEFORE DELETE ON work_order_transfer_lines
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfer_cancellations_no_update BEFORE UPDATE ON work_order_transfer_cancellations
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_transfer_cancellations_no_delete BEFORE DELETE ON work_order_transfer_cancellations
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_resolutions_no_update BEFORE UPDATE ON work_order_resolutions
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_resolutions_no_delete BEFORE DELETE ON work_order_resolutions
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_volume_matches_no_update BEFORE UPDATE ON work_order_volume_matches
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;
CREATE TRIGGER work_order_volume_matches_no_delete BEFORE DELETE ON work_order_volume_matches
BEGIN SELECT RAISE(ABORT,'История производственных решений неизменяема'); END;

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
 AND NOT EXISTS(SELECT 1 FROM work_order_transfers t WHERE t.source_order_id=NEW.id)
 AND NOT EXISTS(SELECT 1 FROM work_order_resolutions r JOIN work_order_lines l ON l.id=r.line_id WHERE l.work_order_id=NEW.id)
 AND NOT EXISTS(SELECT 1 FROM confirmation_events c JOIN work_order_lines l ON l.id=c.work_order_line_id WHERE l.work_order_id=NEW.id)
        THEN RAISE(ABORT,'Нет факта, связанного с нарядом') END;
END;

DROP TRIGGER confirmation_events_order_scope;
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
        AND ((NEW.correction_of_id IS NULL AND o.status IN ('issued','in_progress','submitted','accepted')) OR
            (NEW.correction_of_id IS NOT NULL AND o.status IN ('issued','in_progress','submitted','accepted','closed','cancelled'))))
        THEN RAISE(ABORT,'Наряд или его строка недоступны для регистрации события') END;
END;
