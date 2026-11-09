# Task2 — exact projection and registry

Implemented and frozen by control_projection agent, 2026-11-09.

Owned implementation: `app/services/work_order_control.py`,
`app/repositories/work_order_control_repository.py`; focused tests:
`tests/test_work_order_control_projection.py`,
`tests/test_work_order_control_registry.py`.
No existing repositories, routes, primary journals, payroll code, or migrations edited.

## API

`WorkOrderControlProjection(db,project_id=1)` exposes `metrics`, `lines`, `report`,
`closure_errors`, and `assert_issue` with the plan signatures. `metrics` returns
all compatible legacy metric keys plus the control fields. The supplementary
`fact_roots` and `confirmation_roots` maps are root-event-id → exact Decimal net
capacity, consumed by the command repository. `lines` returns one row per line;
`report.chain_nodes` likewise contains one row per line, scoped to the connected
order component. `report.chain_edges` and `transfers` contain one row per transfer
line, including cancelled history. Snapshot fields retain their stored JSON text.
`report.conflicts` contains order_id,line_id,code,conflict records. Timeline covers
control events; ordinary lifecycle history remains in the existing order card.

All queries are read-only, scoped by project, and use shared read snapshots.
Journal and control arithmetic uses Decimal exact_sum/subtract throughout. An
actual overrun without an outgoing allocation/writeoff is a reviewed discrepancy,
not a fatal allocation conflict. Draft reservations do not satisfy closure.
Ancestor allocation/matching conflicts block issue/closure. Root matching never
reassigns journal events or trims obsolete matches after corrections.

Without control events, metrics signatures reproduce the exact legacy tuple
payload, JSON whitespace, and hash, preserving existing reviewed snapshots.
Control identities, their timestamps, cancellation state, and target status enter
the signature when control events exist. Cutoffs filter journal knowledge and
business dates independently; control events/statuses are bounded by the earlier
of cutoff and end-of-report-day. Future issued numbers/header snapshots are not
shown on historical draft registry rows.

`WorkOrderControlRepository.registry(...)` returns rows,total,page,per_page,pages,
filters. Structural filters use SQL COUNT/LIMIT/OFFSET; derived boolean filters
reduce candidate batches of200 before pagination. Only the resulting header page
is enriched. Parameters are bounded to400 or fewer per IN clause, including
project-wide projections, for SQLite3.24's999-variable limit. Per-page limit is200
(default50); requested out-of-range pages remain empty.

`analytics(...)` returns all documented keys plus `registry`, `policy`, `overdue`,
and `overdue_policy`. `rows` are projected line rows; `registry` are header rows;
`source_rows` are linked primary journal rows with kind and Decimal quantity;
`chain_rows` group connected-component totals by catalog/code/unit. Grouped rows
have original_quantity and assignment counting non-inherited assignments once.
A component graph is built once, avoiding per-order project reprojection.
Multiple brigade snapshots share a combined label, preserving natural totals
without inventing a split of volume. That policy is explicit in `policy.brigades`.

## Verification

Red: initial modules absent; focused imports failed as expected. Additional red
regressions observed for legacy signature mismatch, stale match after C→0, and
multi-brigade duplicate attribution before fixes.

Latest command:

```
.venv/bin/python -m pytest -q tests/test_work_order_control_projection.py tests/test_work_order_control_registry.py tests/test_work_order_transfers.py
38 passed in 7.21s
```

Task2's own19 tests cover safe120/110/100 balances, unrelated events, exact Decimal
arithmetic under precision3, separate matching/no matching, match revisions and
root corrections, draft→issued reservation accounting, cancellation history,
writeoff/reversal, stale ancestors, knowledge cutoffs, branches and three nodes
120→20→5 with115confirmed/5open, separate code/unit groups, legacy signatures,
51row SQL paging, structural/derived filters, project scope, immutable brigade
labels and combined attribution. A1001order analytics test explicitly lowers
SQLite variable limit to999, verifies120120 initial quantity and1001 chain rows,
and requires fewer than300 SELECT/WITH queries.

Full-suite execution belongs to the root agent and was not run here. Files are
frozen; root may review and amend them.

## Root-review correction pass

The frozen files were reopened for root-requested semantic corrections, then
frozen again. New targeted regressions reproduced all five reported gaps before
implementation changes:

- `unclosed` now filters historical status in SQL: true excludes closed/cancelled;
  false includes those two. Fully confirmed accepted orders and empty drafts are
  correctly unclosed. This remains a structural filter with SQL paging.
- `has_remaining` and counts.remaining_lines use current `unresolved` disposition,
  not gross Q−F. A fully transferred parent contributes no remaining line. Draft
  reservations remain unresolved until issued. `policy.remaining_lines` states
  the definition.
- counts.open includes issued/in_progress/submitted/accepted orders, including
  partially completed work; draft/closed/cancelled are excluded. counts.draft is
  available separately; `policy.open` states the definition.
- Daily dynamics now reads actual/confirmed deltas from primary business dates
  in source_rows, including corrections. It uses `shift_date` as the series-date
  key for UI compatibility. Each row has catalog_id,code,unit,assignment,
  original_quantity,actual,confirmed. Only non-inherited root assignment enters
  the series on its shift date. Transferred child assignment is not duplicated.
  No synthetic daily remaining or transfer quantities are supplied.
- Line state_key adds `closed`, `review_required`, `transferred`,
  `partial_transferred`, and `cancelled`; `reserved` takes precedence over missing
  matching for a draft reservation. Closed unresolved lines require review.
  Fatal conflicts retain precedence. Matching and overrun quantities/flags remain
  independently visible; registry header execution_state keys stay unchanged.
- closed_quantity is clamped to [0,Q], including externally introduced invalid
  negative net confirmation rows, which still produce fatal conflicts.

The UI agent received the dynamics/state contract. No primary rows are mutated.

Latest Task2-only verification:

```
.venv/bin/python -m pytest -q tests/test_work_order_control_projection.py tests/test_work_order_control_registry.py
24 passed in 5.83s
```

Python compilation passes. A concurrent combined command (Task2 plus root transfer
tests) produced51passed/1failed: root-owned
`test_workflow_cannot_backdate_close_before_later_control_reversal` did not raise
ValidationError. This was promptly reported to root, who owns lifecycle fixes;
no root-owned file was changed here. Full-suite verification still belongs to root.

## Independent-review signature/history correction

Reproduced the reviewer's two targeted failures before fixing:
`test_transferred_child_can_close_after_one_unchanged_acceptance` and
`test_historical_reservation_does_not_gain_future_target_number_or_signature`.
Transfer signature dependencies now normalize target status into its allocation
capacity class: draft, sent, or cancelled. Issuing a draft still changes the
parent's signature; issued→in_progress→submitted→accepted→closed does not.
The transfer report retains the real historical status for display. Historical
transfer target numbers now come from as-of order metadata, preventing a later
issued number from changing an earlier draft report or signature.

Own regression additionally checks parent draft→sent signature invalidation,
unchanged signatures through a child's fact/submission/review/close lifecycle,
and a transferred child closing after its first unchanged review.

Latest targeted verification (own two files plus the reviewer's two regressions):
27 passed in7.45s. No workflow file edited; root owns the independent ancestor
backdating fix. Files frozen again after this pass.
