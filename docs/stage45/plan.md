# Stage4.5 implementation plan and decisions

Goal: extend existing work orders, never create another order ledger or primary event.
Architecture: existing order/workflow repositories plus append-only control events;
one shared Decimal projection feeds card, commands, registry, control dashboard and XLSX.
Stack: existing Python/Flask/Jinja2/SQLite, no dependencies or network.
Spec: user stage4.5 request in this session; this file records implementation choices.

## Constraints and rulings

- Keep dirty4.4 files and their hashes; no reset, push, cloud, new roles or payroll recalculation.
- Keep migrations001–012 byte-identical. Add013 with Backup API and checked rollback.
- Preserve statuses draft→issued→in_progress→submitted→accepted→closed and cancellation.
- Issued quantity Q is the frozen work_order_lines.planned_quantity; never change it.
- F and C are separate linked journal net sums. Never infer a link from work item/location.
- Ruling: safe transfer basis is Q−max(F,C), clamped at0, less active outgoing and
  writeoffs. Q−F remains the gross execution remainder; Q−C is the gross confirmed
  remainder. Already performed unconfirmed work must not be reissued.
- New draft target reserves quantity immediately. `transferred` includes all active
  reservations; `reserved` is its draft subset, `sent` its issued subset.
  available=max(Q−max(F,C)−transferred−written_off,0).
- Explicit optional fact/confirmation root matching is separate append-only metadata.
  It never creates/changes primary events. Without complete reliable matching C against
  F, pending acceptance is None + «Требуется сопоставление», not F−C fiction.
- Whole transfer command always creates a NEW target draft, never merges into an issued
  target or an ancestor. SQL additionally checks same work item/unit/project and cycles.
- A target retains inherited line IDs/quantities on ordinary header/crew edits. Change
  inherited quantities by cancelling the safe draft transfer and creating a new command.
- Target uses the current approved plan for the identical active work item; source
  retains its old plan. Unit changes/inactive work block transfer with an explanation.
- Parent can close when C+sent+written_off covers Q; draft reservations are not a final
  disposition. Source/ancestor allocation conflicts block issue/close. Overrun without
  a transfer conflict needs explicit reviewed line comment; journals remain unchanged.
- Explicit writeoff applies only to the genuinely unexecuted/unconfirmed available
  balance, requires a reason, supports append-only reversal, never writes off fact.
- A fully disposed issued order with no linked fact can explicitly start in_progress
  and submit for review through existing status sequence. No fake fact is inserted.
- New confirmations in accepted status are permitted to finish real acceptance;
  they stale the prior review, so repeat acceptance before close. Closed orders still
  accept only corrections. No automatic assignment of facts to confirmations.
- Cancellation returns quantity only if target remains draft, has no dependent events
  or descendants. Target becomes cancelled; rows/snapshots stay. Direct cancellation
  cannot bypass active transfer protections.
- Knowledge timestamps are sampled after BEGIN IMMEDIATE. Histories use report_date
  and knowledge_cutoff independently; do not apply future control events to old slices.
- No invented overdue metric; existing shift date is not an agreed completion deadline.
- Reviewer/technical initiator remains local registration, never a fake authenticated user.

## Task1 — baseline and additive schema (root)

Files: docs/stage45/baseline.json; migrations/013_work_order_control.sql; app/database.py.
- [x] Read actual schema/workflow/routes/tests; full baseline1041passed7skipped141.94s.
- [x] Record existing source hashes, Git dirty state, working DB SHA/mtime/integrity read-only.
- [x] Add six control tables, append-only guards, FK/scopes, inherited-line/cycle guards.
- [x] Extend migration safety; populated012→013, repeated migrate, SQL/data failure rollback.

Schema contract (all timestamps canonical UTC text; quantities canonical Decimal TEXT):

`work_order_control_commands(id TEXT PK,project_id,kind,fingerprint,result_json,recorded_at)`.

`work_order_transfers(id INTEGER PK,project_id,source_order_id,target_order_id UNIQUE,
 command_id UNIQUE,reason,comment,created_at,target_shift_date,
 source_executor_snapshot,target_executor_snapshot)`.

`work_order_transfer_lines(id PK,transfer_id,source_line_id,target_line_id UNIQUE,
 quantity,unit)`, UNIQUE(transfer_id,source_line_id).

`work_order_transfer_cancellations(id PK,transfer_id UNIQUE,command_id UNIQUE,reason,recorded_at)`.

`work_order_resolutions(id PK,project_id,line_id,kind ['writeoff','revoke'],quantity,
 reason,recorded_at,command_id UNIQUE,reverses_id UNIQUE NULL)`.

`work_order_volume_matches(id PK,project_id,line_id,fact_event_id,confirmation_event_id,
 quantity [zero allowed to revoke],revision_no,previous_id UNIQUE NULL,reason,
 recorded_at,command_id UNIQUE)`, UNIQUE(line_id,fact_event_id,confirmation_event_id,revision_no).

No old data changes in migration. Changes to existing triggers only in013.

## Task2 — exact shared projection and registry (control_projection agent)

Own new app/services/work_order_control.py; app/repositories/work_order_control_repository.py;
tests/test_work_order_control_projection.py; tests/test_work_order_control_registry.py.
Do not edit existing work_order* repositories/routes, schema or payroll files.

Produces `WorkOrderControlProjection(db,project_id=1)`:

- `.metrics(order_id,report_date=None,knowledge_cutoff=None)->(list[dict],signature)`
  exact compatible old metric keys (line_id,work_item_id,assignment,actual,confirmed,
  remaining=max(Q−F,0),overrun,general_actual,general_confirmed,general_pending,
  has_actual,has_confirmation) plus fields below. No WorkOrderRepository.get_order
  calls (avoids recursion). Batch linked/general journal reads; no query per line.
- `.lines(order_ids=None,report_date=None,knowledge_cutoff=None,include_general=False)`
  returns scoped records with order_id,project_id,order_number,status,shift_date,
  contractor_id, contractor_name,brigades (snapshot labels),code,name,unit,
  building_id/floor_id; quantities/flags as below.
- `.report(order_id,report_date=None,knowledge_cutoff=None)->dict`
  keys lines,transfers,chain_nodes,chain_edges,chain_totals,conflicts,timeline,policy.
  Transfer rows include active bool, source/target IDs/numbers,line IDs,quantity/unit,
  reason/date/status, snapshots,cancellation details; include cancelled history.
  Connected chain includes branches, groups by (catalog_id,unit); original_quantity
  counts topmost non-inherited line quantities ONCE, confirmed counts independent
  linked confirmations ONCE, open_quantity sums current unconsumed leaves; drafts
  inherited from active transfer count as pending work, cancelled transfers do not.
  Chain totals fields catalog_id,code,unit,original_quantity,confirmed,open_quantity,
  written_off,excess; no mixing different units/codes.
- `.closure_errors(order_id,report_date=None,knowledge_cutoff=None)->list[str]`
  unresolved=C-based net including draft reserves as unresolved; block invalid
  transfer/writeoff/match capacities, ancestor conflicts, unsafe cancelled targets.
- `.assert_issue(order_id)` throws ValidationError for stale inherited capacity or
  inherited mismatch; no mutation. Traverse ancestors to block stale origins.

Every line extra fields:
`execution_remaining=max(Q−F,0)`, `confirmed_remaining=max(Q−C,0)`,
`transferred` total active outgoing, `reserved` draft subset, `sent` issued subset,
`written_off`, `available` safe unclipped residual floor0,
`open_quantity=max(Q−C−transferred−written_off,0)`,
`unresolved=max(Q−C−sent−written_off,0)`,
`closed_quantity=min(Q,C+sent+written_off)`, `confirmed_overrun=max(C−Q,0)`,
`allocation_excess=max(max(F,C)+transferred+written_off−Q,0)`,
`pending_acceptance:Decimal|None`, `matching_required:bool`, `matched_quantity`,
`conflicts:list[str]`, `state` Russian computedlabel, `state_key` stablekey,
`incoming_quantity` (active inherited), `line_id`, `order_id`.
Distinguish actual overrun (no allocations) from conflicting reservations. An actual
overrun alone is reviewed discrepancy; allocation_excess caused by activeoutgoing or
writeoff is fatal. Negative net/root/matching invalidities are fatal.

Matching latest revision asofcutoff; root capacities net correction chains only
linked to same order line. No unlinked journal matching. If C unallocated and F>0,
pending=None; when all C safely matched, pending=max(F−matched,0). F0→pending0;
C0→pendingF. Corrections invalidating matching create conflicts, no autotruncation.

Registry API `WorkOrderControlRepository(db,project_id=1)`:
- `.registry(filters=None,page=1,per_page=50,report_date=None,knowledge_cutoff=None)`
  -> dict(rows,total,page,per_page,pages,filters). filters search,date_from,date_to,
  contractor_id,building_id,brigade_id,status,has_remaining,has_transfer,unclosed,
  discrepancies. Optional boolean flags bool. Header rows have existing key id,
  number/date/contractor/buildings/line_count, plus grouped `volumes` by code/unit,
  open_line_count,transfer_line_count,has_children,execution_state,brigades.
  SQL COUNT/LIMIT/OFFSET for structural filters. Derivedfilters may need exact
  bounded server-side batches of candidate line projections before final pagination;
  never return full histories or thousands of HTML rows. Avoid N+1 per worker.
- `.analytics(filters=None,report_date=None,knowledge_cutoff=None)` -> dict counts,
  by_work,by_contractor,by_brigade,dynamics,discrepancies,rows,filters,report_date,cutoff,
  transfers,chain_rows,source_rows. Counts issued,full,partial,open,closed,
  remaining_lines,transferred_lines; overdue None with an explanatory policy.
  Use chain-aware initial/open work quantities; no duplicate original+children total.
  Group natural totals by code/unit; snapshots define historical brigades.

Tests red→green:120/100→20, F110/C100→10 safe, unmappedpendingNone,
mappedpartial/exact capacities, zero/full/excess, latecorrections/cutoff,
draftreserved→issued sent; cancellationhistory; branch chain120→20→5 =115confirmed5open,
different units; registry51rows→50+1, allfilterclasses, actualSQLbounded headers.

## Task3 — commands and existing lifecycle (root)

Own app/repositories/work_order_transfer_repository.py; existing
work_order_repository.py + work_order_workflow_repository.py; new tests
test_work_order_transfers.py,test_work_order_control_migration.py,
test_work_order_control_preservation.py; update obsolete old closure tests explicitly.

Produces `WorkOrderTransferRepository(db,project_id=1)`:
- `.transfer(source_order_id,lines,header,crew=(),brigade_ids=(),*,reason,
 comment='',operation_key)->dict(transfer_id,order_id,line_ids)`.
 lines list dict(source_line_id,quantity). target ALWAYS newdraft.
- `.cancel_transfer(transfer_id,reason,operation_key)->bool`.
- `.writeoff(line_id,quantity,reason,operation_key)->id`.
- `.revoke_writeoff(resolution_id,reason,operation_key)->id`.
- `.match(line_id,fact_event_id,confirmation_event_id,quantity,reason,
 expected_revision=0,operation_key)->id`.
 All command operations BEGIN IMMEDIATE, canonical input fingerprint and idkey;
 replay samepayload returns original, mismatched payload controlled conflict.
 No primary journal/timesheet/labor/payroll writes. Capacity reread under lock.

Existing `.save_draft` wraps new private `._save_draft` used inside callertransaction;
 preserve inherited IDs. Existing issue/cancel invoke control safety, no newordercopy.
 Workflow.metrics delegates to shared projection only if013 present (legacy fixtures
 may initialize schema007–012); closure replaces comment-only exception with exact
 resolutions. Confirmations allowed accepted with mandatory fresh review beforeclose.

Tests: whole/partial/multiline transfer, branch/3nodes, sameID replay/differentconflict,
negative/zero/foreign/sourcecancelled/units/currentplan conflict, editing inherited
draft, safe and unsafe cancel, actual threads15+15 from20 onlyone, writesrollback,
late source correction beforeissue/afterclose, nofact independentF/C, writeoff/revoke,
matchingcap/history, statuses and readonly issued snapshot. 012→013 populatedpayroll
backup and fingerprints preserve all primary/personnel/time/labor/payroll rows.

## Task4 — integrate existing UI and XLSX (UI agent after contracts)

Own existing app/web/routes/work_orders.py, work_orders.html, work_order_detail.html,
_work_order_progress.html,_work_order_actions.html, app/web/navigation.py;
new work_order_transfer.html,_work_order_transfers.html,work_order_control.html,
work_order_matches.html,app/services/work_order_control_xlsx.py and webtests.

Routes within existing `work_orders` blueprint:
- existing GET/work-orders uses registry, serverpagination50 preservingoldURLs.
- existing detail enriched with `.report` in templatecontext `control` (no secondcard).
- GET/POST/work-orders/<order_id>/transfer ->transferAPI; parses `source_line_id`
  repeated and `quantity_<id>`, header same existing parse header/crew fields.
  existing parse_form assumesworkitems; transfer should parseheader+crewwith
  empty normalworklines arrays, passes empty lines to parse_form forheader+crew.
- POST/work-orders/transfers/<transfer_id>/cancel.
- POST/work-orders/<order_id>/lines/<line_id>/writeoff and .../match;
  POST/work-orders/resolutions/<id>/revoke.
- GET/work-orders/control, GET/work-orders/control/export.xlsx withsamefilters+dates.
 Export seven sheets registry/control/open/transfers/chains/discrepancies/sources;
 existingxlsxwriter,numericDecimal,typeddate/filtermetadata,literalusertext.
 Bounded HTML, preserve oldprint, CSRFworkspaceHostOrigin andarchivereadonly.
 Newnav «Контроль нарядов» within Production, one existingmenu system.
 Tests realcsrf forms full/partial/chainclose/cancel/filter/page/export andscope.

## Task5 — independent review, acceptance, documentation (root + fresh reviewer)

- [x] Run newtests thenfullsuite, record skips honestly; compare SHA/hash & payroll totals.
- [x] Attempt Chromium once, record EPERM ifno page; never claim Flask isbrowser.
  Disposable backendacceptance exact scenariosA–F, full7sheetnumericparity.
- [x] docs/stage45_work_orders.md and docs/stage45_results.md, Macmanualsteps,
  changedfilemanifest, evidenceJSON/logs and optionalreal-synthetic XLSXsample.
- [x] No push/commit/deploy or nextstage. Return reviewable workingtree.

## Progress ledger

2026-11-09: baseline1041passed7skipped; dirty4.4 preserved; sourceSQLite read-onlyok.
Ruling: continue in user-designated shared checkout without resetting or snapshot
replacement; no parallel agent may edit parent-owned files. The user's detailed
implementation instruction supplies scope; routine choices above do not require
another approval conversation.

2026-11-09 progress:013+commands implemented; transfer/legacyflow/migration targeted runs green. Root timestampfence regression reproduced andfixed. Task2 projection/registry implementedandreview semanticfixes inprogress; Task4 UI/XLSX inprogress. Parent originalSQLite unchanged. Old closure tests explicitly updated to real confirmation+resolution, never autoaccepted barecomment. No fullsuiteafter45yet.

Final2026-11-09:1128passed7skipped0failed171.58s;87newchecks. ActualChromiumattemptblockedbeforepage; FlaskA–Fseparatepassed,7sheetreal-syntheticartifactretained. Independentreview3regressionsfixed+2analyticalfixesapproved. Allold001–012/primary/time/labor/payrollhashespreserved; liveSQLiteSHA/mtimeunchanged. Stage45docs/manifest/evidencecomplete; no push/deploy/nextstage.
