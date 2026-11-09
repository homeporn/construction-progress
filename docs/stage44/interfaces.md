# Контракты этапа4.4

Все repo(db,project_id=1), transaction BEGIN IMMEDIATE; датыvalid_to EXCLUSIVE.
Ни один метод не меняет существующийтабель/факт/подтверждение.

## Общие функции (родитель payroll_base.py)
PayrollBase(db,project_id), .people PersonnelRepository
.require_period(id), .require_version(id,editable=False) -> dict(versionfields +
 project_id,contractor_id,year,month,project_name,contractor_name).
.editable versionstatuses draft/calculated; revisionsdirty→draft.
.replay(key,kind,payload) -> (found:bool,result:any); .remember(key,kind,payload,result)
.key/newuuid автоеслиkeyNone; использовать returnresult дляreplay доисходныхпроверок.
.dirty(version_id,reason='') changesstatuscalculated→draft/revision+1 andevent.
.event(version_id,action,reason='',old_status=None,new_status=None,details={}).
.json(value) canonicaljson helper atmodule; .scope ensure projectactive.

## Rules repo (агент)
PayrollRulesRepository .save_terms(data,term_id=None,expected_revision=None,operation_key=None)->rootID
.list_terms(cutoff=None)->latest dict incl employee_id,full_name,root_id,revision_no
.terms_on(employee_id,contractor_id,date,cutoff=None)->row|None
.save_rate(data,rate_id=None,expected_revision=None,operation_key=None)->rootID
.list_rates(cutoff=None)->latest dict .rate_on(catalog_id,contractor_id,date,operation_id=None,cutoff=None)->row|None
.save_operation(catalog_id,code,name,operation_id=None,is_active=True)->ID .list_operations()
Data fields terms: employee_id,contractor_id,valid_from,valid_to,system(hourly/salary/piece/combined),
base_kind(hourly/salary/none),hourly_rate,salary,currency(RUB/USD/EUR),piece_enabled,
bonus_kind(none/fixed/percent/performance),bonus_basis(base/piece),bonus_value,
performance_threshold(default1),fixed_supplement,status(active/archived),comment.
Rates: catalog_id,contractor_id(optionalglobalproject),operation_id(optional),amount,
unit(snapshot),currency,valid_from,valid_to,status,comment.

payroll_money.py (агент): to_minor(value)->int (ROUND_HALF_UP cents), from_minor(int)->Decimal,
split_money(total_minor,weights dict[employee_id,Decimal])->dict ID:int stablelargestRemainder.

PayrollCalculator(db,project_id) (агент) .calculate(period:dict,version:dict,cutoff=None,
 piece_lines:list[dict]=[],adjustments:list[dict]=[])->dict lines/errors/warnings/sources/totals/workers
line fields employee_id,component(base_hourly/base_salary/piece/bonus/supplement/adjustment),
amount_minor,currency,source(dict),term_revision_id/rate_revision_id/work_item_id(optional),comment.
Sources deterministic JSONserializable all dependencies withIDs/rates/hours;
totals=dictcurrency:int; workers summarisedfromlines (parentnormalizesifneeded).
Calculator readonly, no writes/transactions ofitsown; parentwrapsnapshot/write lock.

## Piece repo (агент)
PayrollPieceRepository .save_claim(data,claim_id=None,expected_revision=None,operation_key=None)->claimID
Data: confirmation_event_id,contractor_id,brigade_id(optional),employee_id(optional),
operation_id(optional tech),quantity,proof_ref,reason(optionalrevisionmandatory).
.list_claims(cutoff=None)->latest +sourceavailable/assigned/reserved/remaining/code/name/unit/work_item_id/effective_date.
.preview(version_id,claim_id,quantity,method,recipients:list[dict(employee_id,weight(optional),amount(optional))],
 reason='',operation_key=None)->preview dict withid,total_minor,shares,currency,source etc.
.confirm(version_id,preview_id,operation_key=None)->batchID atomic,idempotent.
.cancel_batch(version_id,batch_id,reason,operation_key=None) .batches(version_id)->list
.validate(version_id,cutoff=None)->dict errors/warnings/sources/lines
lines employee_id/componentpiece/amount_minor/currency/source/rate_revision_id/work_item_id.
.copy_to_version(old_version_id,new_version_id) no own transaction (callerowns).
Readonlysource snapshots detect primary corrections/rate changes/assignmentrev/hours/KTU,
latesteffectiveversion reservations excludesobsolete versions andreservesmaxoldapproved/newdraft.

## Period repo (родитель)
PayrollRepository .create_period(contractor_id,year,month,norm_hours=None,comment='',operation_key=None)->periodID
.list_periods(); .get_period(id); .get_version(period_id,version_id=None);
.update_settings(version_id,norm_hours,comment='',expected_revision=None,operation_key=None)
.calculate(version_id,operation_key=None)->runID; .transition(version_id,action,reason='',operation_key=None)
actions submit/return/approve/close; .create_correction(period_id,reason,operation_key=None)->versionID
.add_adjustment(version_id,employee_id,amount,kind='adjustment',currency='RUB',effective_date=None,reason='',operation_key=None)->ID
.report(period_id,version_id=None)->dictperiod,version,rows,lines,totals,errors,warnings,delta,
source_changed(bool),versions,events,runs,adjustments,batches.
Rows employee_id/full_name/system/currency/base/piece/bonus/supplement/adjustment/total Decimals.
.delta dictcurrency:Decimal totaldifference vsbaseapproved (perworker fields optional).
.dashboard()->dictfunds currencyDecimal,periods,rows aggregations(bycontractor/method/work),warnings.

## UI (агент)
/payroll overview,/payroll/terms,/payroll/rates,/payroll/operations,
/payroll/periods,/payroll/periods/new,/payroll/periods/<id> (version_idquery),
/payroll/claims,/payroll/versions/<vid>/piece-preview+confirm,/<vid>/action,
/payroll/periods/<id>/export.xlsx. Inexistingnavonepayrollgroup, guard/csrf unchanged.
EightXLSXsheets existingwriter money numeric/date/style; noextraPII.
