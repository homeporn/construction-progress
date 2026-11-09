-- Local managerial accruals; primary journals and migrations001-011 stay intact.
CREATE TABLE payroll_operations (
 id TEXT PRIMARY KEY NOT NULL,project_id INTEGER NOT NULL REFERENCES projects(id),kind TEXT NOT NULL,
 fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,recorded_at TEXT NOT NULL
);
CREATE TABLE payroll_term_roots (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),employee_id INTEGER NOT NULL REFERENCES employees(id),
 contractor_id INTEGER NOT NULL REFERENCES contractors(id),created_at TEXT NOT NULL,
 FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE TABLE payroll_terms (
 id INTEGER PRIMARY KEY,root_id INTEGER NOT NULL REFERENCES payroll_term_roots(id),revision_no INTEGER NOT NULL CHECK(revision_no>0),
 previous_id INTEGER UNIQUE REFERENCES payroll_terms(id),valid_from TEXT NOT NULL,valid_to TEXT,
 system TEXT NOT NULL CHECK(system IN ('hourly','salary','piece','combined')),
 base_kind TEXT NOT NULL CHECK(base_kind IN ('hourly','salary','none')),hourly_rate TEXT,salary TEXT,currency TEXT NOT NULL,
 piece_enabled INTEGER NOT NULL CHECK(piece_enabled IN (0,1)),bonus_kind TEXT NOT NULL CHECK(bonus_kind IN ('none','fixed','percent','performance')),
 bonus_basis TEXT NOT NULL CHECK(bonus_basis IN ('base','piece')),bonus_value TEXT NOT NULL DEFAULT '0',
 performance_threshold TEXT NOT NULL DEFAULT '1',fixed_supplement TEXT NOT NULL DEFAULT '0',
 status TEXT NOT NULL CHECK(status IN ('active','archived')),comment TEXT NOT NULL DEFAULT '',recorded_at TEXT NOT NULL,
 UNIQUE(root_id,revision_no),CHECK(valid_to IS NULL OR valid_to>valid_from),
 CHECK((revision_no=1 AND previous_id IS NULL) OR (revision_no>1 AND previous_id IS NOT NULL))
);
CREATE INDEX payroll_terms_dates ON payroll_terms(root_id,recorded_at,revision_no);
CREATE TABLE payroll_tech_operations (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),catalog_id INTEGER NOT NULL REFERENCES work_catalog(id),
 code TEXT NOT NULL,name TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(project_id,catalog_id,code)
);
CREATE TABLE payroll_rate_roots (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),contractor_id INTEGER REFERENCES contractors(id),
 catalog_id INTEGER NOT NULL REFERENCES work_catalog(id),operation_id INTEGER REFERENCES payroll_tech_operations(id),unit TEXT NOT NULL,created_at TEXT NOT NULL,
 FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE TABLE payroll_rates (
 id INTEGER PRIMARY KEY,root_id INTEGER NOT NULL REFERENCES payroll_rate_roots(id),revision_no INTEGER NOT NULL CHECK(revision_no>0),
 previous_id INTEGER UNIQUE REFERENCES payroll_rates(id),valid_from TEXT NOT NULL,valid_to TEXT,amount TEXT NOT NULL,currency TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','archived')),comment TEXT NOT NULL DEFAULT '',recorded_at TEXT NOT NULL,
 UNIQUE(root_id,revision_no),CHECK(valid_to IS NULL OR valid_to>valid_from),
 CHECK((revision_no=1 AND previous_id IS NULL) OR (revision_no>1 AND previous_id IS NOT NULL))
);
CREATE INDEX payroll_rates_dates ON payroll_rates(root_id,recorded_at,revision_no);
CREATE TABLE payroll_periods (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),contractor_id INTEGER NOT NULL REFERENCES contractors(id),
 year INTEGER NOT NULL CHECK(year BETWEEN 1900 AND 9999),month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),created_at TEXT NOT NULL,
 UNIQUE(project_id,contractor_id,year,month),FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE TABLE payroll_versions (
 id INTEGER PRIMARY KEY,period_id INTEGER NOT NULL REFERENCES payroll_periods(id),version_no INTEGER NOT NULL CHECK(version_no>0),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','calculated','submitted','approved','closed')),
 base_version_id INTEGER REFERENCES payroll_versions(id),correction_reason TEXT NOT NULL DEFAULT '',norm_hours TEXT,comment TEXT NOT NULL DEFAULT '',
 current_run_id INTEGER REFERENCES payroll_runs(id),revision_no INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,calculated_at TEXT,approved_at TEXT,closed_at TEXT,
 UNIQUE(period_id,version_no),CHECK(base_version_id IS NULL OR length(trim(correction_reason))>0)
);
CREATE UNIQUE INDEX payroll_one_open ON payroll_versions(period_id) WHERE status IN ('draft','calculated','submitted');
CREATE TABLE payroll_runs (
 id INTEGER PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES payroll_versions(id),run_no INTEGER NOT NULL,
 cutoff TEXT NOT NULL,source_digest TEXT NOT NULL,source_json TEXT NOT NULL,rows_json TEXT NOT NULL,totals_json TEXT NOT NULL,
 warnings_json TEXT NOT NULL,errors_json TEXT NOT NULL,created_at TEXT NOT NULL,sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),UNIQUE(version_id,run_no)
);
CREATE TABLE payroll_accruals (
 id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES payroll_runs(id),employee_id INTEGER NOT NULL REFERENCES employees(id),
 component TEXT NOT NULL CHECK(component IN ('base_hourly','base_salary','piece','bonus','supplement','adjustment')),
 amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND abs(amount_minor)<=9000000000000000),currency TEXT NOT NULL,
 term_revision_id INTEGER REFERENCES payroll_terms(id),rate_revision_id INTEGER REFERENCES payroll_rates(id),work_item_id INTEGER REFERENCES work_items(id),
 source_json TEXT NOT NULL,comment TEXT NOT NULL DEFAULT ''
);
CREATE INDEX payroll_accruals_worker ON payroll_accruals(run_id,employee_id,currency);
CREATE TABLE payroll_adjustments (
 id INTEGER PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES payroll_versions(id),employee_id INTEGER NOT NULL REFERENCES employees(id),
 effective_date TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('bonus','adjustment','supplement')),
 amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor!=0 AND abs(amount_minor)<=9000000000000000),
 currency TEXT NOT NULL,reason TEXT NOT NULL CHECK(length(trim(reason))>0),recorded_at TEXT NOT NULL,
 reverses_id INTEGER UNIQUE REFERENCES payroll_adjustments(id)
);
CREATE TABLE payroll_events (
 id INTEGER PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES payroll_versions(id),action TEXT NOT NULL,
 old_status TEXT,new_status TEXT,recorded_at TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE payroll_claims (
 id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),confirmation_event_id INTEGER NOT NULL REFERENCES confirmation_events(id),
 contractor_id INTEGER NOT NULL REFERENCES contractors(id),brigade_id INTEGER REFERENCES brigades(id),employee_id INTEGER REFERENCES employees(id),
 operation_id INTEGER REFERENCES payroll_tech_operations(id),created_at TEXT NOT NULL,
 FOREIGN KEY(contractor_id,project_id) REFERENCES contractor_projects(contractor_id,project_id)
);
CREATE UNIQUE INDEX payroll_claim_actor ON payroll_claims(confirmation_event_id,contractor_id,COALESCE(brigade_id,0),COALESCE(employee_id,0),COALESCE(operation_id,0));
CREATE TABLE payroll_claim_revisions (
 id INTEGER PRIMARY KEY,claim_id INTEGER NOT NULL REFERENCES payroll_claims(id),revision_no INTEGER NOT NULL CHECK(revision_no>0),
 previous_id INTEGER UNIQUE REFERENCES payroll_claim_revisions(id),quantity TEXT NOT NULL,proof_ref TEXT NOT NULL CHECK(length(trim(proof_ref))>0),
 reason TEXT NOT NULL DEFAULT '',recorded_at TEXT NOT NULL,UNIQUE(claim_id,revision_no),
 CHECK((revision_no=1 AND previous_id IS NULL) OR (revision_no>1 AND previous_id IS NOT NULL AND length(trim(reason))>0))
);
CREATE INDEX payroll_claim_history ON payroll_claim_revisions(claim_id,recorded_at,revision_no);
CREATE TABLE payroll_piece_previews (
 id TEXT PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES payroll_versions(id),claim_id INTEGER NOT NULL REFERENCES payroll_claims(id),
 claim_revision_id INTEGER NOT NULL REFERENCES payroll_claim_revisions(id),rate_revision_id INTEGER NOT NULL REFERENCES payroll_rates(id),
 quantity TEXT NOT NULL,total_minor INTEGER NOT NULL CHECK(typeof(total_minor)='integer'),currency TEXT NOT NULL,method TEXT NOT NULL,
 shares_json TEXT NOT NULL,basis_json TEXT NOT NULL,source_json TEXT NOT NULL DEFAULT '{}',snapshot_digest TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'preview' CHECK(status IN ('preview','committed','cancelled')),created_at TEXT NOT NULL,
 result_batch_id INTEGER REFERENCES payroll_piece_batches(id)
);
CREATE TABLE payroll_piece_batches (
 id INTEGER PRIMARY KEY,version_id INTEGER NOT NULL REFERENCES payroll_versions(id),claim_id INTEGER NOT NULL REFERENCES payroll_claims(id),
 claim_revision_id INTEGER NOT NULL REFERENCES payroll_claim_revisions(id),rate_revision_id INTEGER NOT NULL REFERENCES payroll_rates(id),
 quantity TEXT NOT NULL,rate_snapshot TEXT NOT NULL,amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer'),currency TEXT NOT NULL,
 method TEXT NOT NULL,source_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
 created_at TEXT NOT NULL,cancelled_at TEXT,cancellation_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX payroll_piece_reservations ON payroll_piece_batches(claim_id,version_id,status);
CREATE TABLE payroll_piece_shares (
 batch_id INTEGER NOT NULL REFERENCES payroll_piece_batches(id),employee_id INTEGER NOT NULL REFERENCES employees(id),
 amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>=0),weight TEXT NOT NULL,
 PRIMARY KEY(batch_id,employee_id)
);
CREATE TRIGGER payroll_operations_no_update BEFORE UPDATE ON payroll_operations BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_operations_no_delete BEFORE DELETE ON payroll_operations BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_term_roots_no_update BEFORE UPDATE ON payroll_term_roots BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_term_roots_no_delete BEFORE DELETE ON payroll_term_roots BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_terms_no_update BEFORE UPDATE ON payroll_terms BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_terms_no_delete BEFORE DELETE ON payroll_terms BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_rate_roots_no_update BEFORE UPDATE ON payroll_rate_roots BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_rate_roots_no_delete BEFORE DELETE ON payroll_rate_roots BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_rates_no_update BEFORE UPDATE ON payroll_rates BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_rates_no_delete BEFORE DELETE ON payroll_rates BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_periods_no_update BEFORE UPDATE ON payroll_periods BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_periods_no_delete BEFORE DELETE ON payroll_periods BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_runs_no_delete BEFORE DELETE ON payroll_runs BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_accruals_no_update BEFORE UPDATE ON payroll_accruals BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_accruals_no_delete BEFORE DELETE ON payroll_accruals BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_adjustments_no_update BEFORE UPDATE ON payroll_adjustments BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_adjustments_no_delete BEFORE DELETE ON payroll_adjustments BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_events_no_update BEFORE UPDATE ON payroll_events BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_events_no_delete BEFORE DELETE ON payroll_events BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_claims_no_update BEFORE UPDATE ON payroll_claims BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_claims_no_delete BEFORE DELETE ON payroll_claims BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_claim_revisions_no_update BEFORE UPDATE ON payroll_claim_revisions BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_claim_revisions_no_delete BEFORE DELETE ON payroll_claim_revisions BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_piece_shares_no_update BEFORE UPDATE ON payroll_piece_shares BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_piece_shares_no_delete BEFORE DELETE ON payroll_piece_shares BEGIN SELECT RAISE(ABORT,'История начислений неизменяема'); END;
CREATE TRIGGER payroll_versions_no_delete BEFORE DELETE ON payroll_versions BEGIN SELECT RAISE(ABORT,'История начислений сохраняется'); END;
CREATE TRIGGER payroll_piece_batches_no_delete BEFORE DELETE ON payroll_piece_batches BEGIN SELECT RAISE(ABORT,'История начислений сохраняется'); END;
CREATE TRIGGER payroll_piece_previews_no_delete BEFORE DELETE ON payroll_piece_previews BEGIN SELECT RAISE(ABORT,'История начислений сохраняется'); END;
CREATE TRIGGER payroll_tech_operations_no_delete BEFORE DELETE ON payroll_tech_operations BEGIN SELECT RAISE(ABORT,'История начислений сохраняется'); END;
CREATE TRIGGER payroll_versions_update BEFORE UPDATE ON payroll_versions BEGIN
 SELECT CASE WHEN NEW.id!=OLD.id OR NEW.period_id!=OLD.period_id OR NEW.version_no!=OLD.version_no
 OR NEW.base_version_id IS NOT OLD.base_version_id OR NEW.correction_reason!=OLD.correction_reason OR NEW.created_at!=OLD.created_at
 THEN RAISE(ABORT,'Контекст расчёта неизменяем') END;
 SELECT CASE WHEN OLD.status='closed' OR (OLD.status='approved' AND (NEW.status!='closed'
 OR NEW.norm_hours IS NOT OLD.norm_hours OR NEW.comment!=OLD.comment OR NEW.current_run_id IS NOT OLD.current_run_id))
 THEN RAISE(ABORT,'Утверждённые начисления неизменяемы') END;
 SELECT CASE WHEN NEW.status!=OLD.status AND NOT ((OLD.status='draft' AND NEW.status='calculated')
 OR (OLD.status='calculated' AND NEW.status IN ('draft','submitted')) OR (OLD.status='submitted' AND NEW.status IN ('draft','approved'))
 OR (OLD.status='approved' AND NEW.status='closed')) THEN RAISE(ABORT,'Недопустимый переход начислений') END;
 SELECT CASE WHEN OLD.status='submitted' AND (NEW.norm_hours IS NOT OLD.norm_hours OR NEW.comment!=OLD.comment OR NEW.current_run_id IS NOT OLD.current_run_id)
 THEN RAISE(ABORT,'Сначала верните расчёт на доработку') END;
 SELECT CASE WHEN NEW.current_run_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM payroll_runs r WHERE r.id=NEW.current_run_id AND r.version_id=NEW.id AND r.sealed=1)
 THEN RAISE(ABORT,'Расчёт другой версии') END;
END;
CREATE TRIGGER payroll_versions_insert BEFORE INSERT ON payroll_versions
WHEN NEW.base_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=NEW.base_version_id AND v.period_id=NEW.period_id AND v.status IN ('approved','closed') AND v.version_no<NEW.version_no)
BEGIN SELECT RAISE(ABORT,'Неверная исходная версия начислений'); END;
CREATE TRIGGER payroll_runs_editable BEFORE INSERT ON payroll_runs WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=NEW.version_id AND v.status IN ('draft','calculated')) BEGIN SELECT RAISE(ABORT,'Версия начислений недоступна для изменения'); END;
CREATE TRIGGER payroll_adjustments_editable BEFORE INSERT ON payroll_adjustments WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=NEW.version_id AND v.status IN ('draft','calculated')) BEGIN SELECT RAISE(ABORT,'Версия начислений недоступна для изменения'); END;
CREATE TRIGGER payroll_piece_previews_editable BEFORE INSERT ON payroll_piece_previews WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=NEW.version_id AND v.status IN ('draft','calculated')) BEGIN SELECT RAISE(ABORT,'Версия начислений недоступна для изменения'); END;
CREATE TRIGGER payroll_piece_batches_editable BEFORE INSERT ON payroll_piece_batches WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=NEW.version_id AND v.status IN ('draft','calculated')) BEGIN SELECT RAISE(ABORT,'Версия начислений недоступна для изменения'); END;
CREATE TRIGGER payroll_accruals_context BEFORE INSERT ON payroll_accruals
WHEN NOT EXISTS(SELECT 1 FROM payroll_runs r JOIN payroll_versions v ON v.id=r.version_id JOIN payroll_periods p ON p.id=v.period_id
 JOIN employee_projects e ON e.project_id=p.project_id AND e.employee_id=NEW.employee_id WHERE r.id=NEW.run_id AND v.status IN ('draft','calculated'))
BEGIN SELECT RAISE(ABORT,'Работник или версия другого объекта'); END;
CREATE TRIGGER payroll_claim_context BEFORE INSERT ON payroll_claims
WHEN NOT EXISTS(SELECT 1 FROM confirmation_events c JOIN work_items w ON w.id=c.work_item_id JOIN buildings b ON b.id=w.building_id
 WHERE c.id=NEW.confirmation_event_id AND c.correction_of_id IS NULL AND b.project_id=NEW.project_id
 AND (NEW.operation_id IS NULL OR EXISTS(SELECT 1 FROM payroll_tech_operations o WHERE o.id=NEW.operation_id AND o.catalog_id=w.catalog_id AND o.project_id=NEW.project_id))
 AND (NEW.brigade_id IS NULL OR EXISTS(SELECT 1 FROM brigades g WHERE g.id=NEW.brigade_id AND g.contractor_id=NEW.contractor_id)))
BEGIN SELECT RAISE(ABORT,'Подтверждение или операция другого контекста'); END;
CREATE TRIGGER payroll_piece_shares_context BEFORE INSERT ON payroll_piece_shares
WHEN NOT EXISTS(SELECT 1 FROM payroll_piece_batches b JOIN payroll_versions v ON v.id=b.version_id
 JOIN payroll_periods p ON p.id=v.period_id JOIN employee_projects e ON e.project_id=p.project_id AND e.employee_id=NEW.employee_id
 WHERE b.id=NEW.batch_id AND b.status='active' AND v.status IN ('draft','calculated'))
BEGIN SELECT RAISE(ABORT,'Получатель или версия недоступны'); END;
CREATE TRIGGER payroll_piece_batches_update BEFORE UPDATE ON payroll_piece_batches
WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=OLD.version_id AND v.status IN ('draft','calculated'))
 OR OLD.status!='active' OR NEW.status!='cancelled' OR NEW.cancelled_at IS NULL OR length(trim(NEW.cancellation_reason))=0
 OR NEW.id!=OLD.id OR NEW.version_id!=OLD.version_id OR NEW.claim_id!=OLD.claim_id OR NEW.claim_revision_id!=OLD.claim_revision_id
 OR NEW.rate_revision_id!=OLD.rate_revision_id OR NEW.quantity!=OLD.quantity OR NEW.rate_snapshot!=OLD.rate_snapshot
 OR NEW.amount_minor!=OLD.amount_minor OR NEW.currency!=OLD.currency OR NEW.method!=OLD.method OR NEW.source_json!=OLD.source_json OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'Распределение зафиксировано; допускается только отмена черновика'); END;
CREATE TRIGGER payroll_piece_previews_update BEFORE UPDATE ON payroll_piece_previews
WHEN OLD.status!='preview' OR NEW.status NOT IN ('committed','cancelled')
 OR NOT EXISTS(SELECT 1 FROM payroll_versions v WHERE v.id=OLD.version_id AND v.status IN ('draft','calculated'))
 OR NEW.id!=OLD.id OR NEW.version_id!=OLD.version_id OR NEW.claim_id!=OLD.claim_id OR NEW.claim_revision_id!=OLD.claim_revision_id
 OR NEW.rate_revision_id!=OLD.rate_revision_id OR NEW.quantity!=OLD.quantity OR NEW.total_minor!=OLD.total_minor
 OR NEW.currency!=OLD.currency OR NEW.method!=OLD.method OR NEW.shares_json!=OLD.shares_json
 OR NEW.basis_json!=OLD.basis_json OR NEW.source_json!=OLD.source_json OR NEW.snapshot_digest!=OLD.snapshot_digest OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'Предварительный просмотр неизменяем'); END;
CREATE TRIGGER payroll_piece_batches_context BEFORE INSERT ON payroll_piece_batches
WHEN NOT EXISTS(SELECT 1 FROM payroll_versions v JOIN payroll_periods p ON p.id=v.period_id
 JOIN payroll_claims c ON c.id=NEW.claim_id JOIN payroll_claim_revisions r ON r.id=NEW.claim_revision_id
 JOIN payroll_rates rate ON rate.id=NEW.rate_revision_id JOIN payroll_rate_roots rr ON rr.id=rate.root_id
 WHERE v.id=NEW.version_id AND c.project_id=p.project_id AND c.contractor_id=p.contractor_id
 AND r.claim_id=c.id AND rr.project_id=p.project_id AND (rr.contractor_id IS NULL OR rr.contractor_id=p.contractor_id))
BEGIN SELECT RAISE(ABORT,'Источники сдельного начисления другого контекста'); END;
CREATE TRIGGER payroll_accruals_published BEFORE INSERT ON payroll_accruals
WHEN EXISTS(SELECT 1 FROM payroll_runs r WHERE r.id=NEW.run_id AND r.sealed=1)
BEGIN SELECT RAISE(ABORT,'Опубликованный расчёт неизменяем; создайте новый расчёт'); END;
CREATE TRIGGER payroll_runs_seal BEFORE UPDATE ON payroll_runs
WHEN OLD.sealed!=0 OR NEW.sealed!=1 OR NEW.id!=OLD.id OR NEW.version_id!=OLD.version_id OR NEW.run_no!=OLD.run_no
 OR NEW.cutoff!=OLD.cutoff OR NEW.source_digest!=OLD.source_digest OR NEW.source_json!=OLD.source_json OR NEW.rows_json!=OLD.rows_json
 OR NEW.totals_json!=OLD.totals_json OR NEW.warnings_json!=OLD.warnings_json OR NEW.errors_json!=OLD.errors_json OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'Зафиксированный расчёт неизменяем'); END;
