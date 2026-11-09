# Independent Stage4.5 review

Reviewed the recorded plan/rulings, initial review package, and current implementations
of migration013, transfer commands, shared projection, registry, workflow integration,
and the new route/export code. Review owns only this report and
`tests/test_work_order_control_review.py`; no application or database changes were made.

## Final verdict: approved within the reviewed scope

All three independently reproduced regressions below are fixed. The owned regression
suite now reports **3 passed in 0.52s**. Scoped rereview confirms that the fixes address
the causes: workflow writes fence timestamps against the active ancestor graph,
review signatures classify transfer capacity as draft/sent/cancelled, and transfer
target numbers use historical headers. No additional blocker was found in those fixes.

## Initial findings, now resolved

Three concrete regressions reproduced on disposable SQLite databases:

1. **P1 — A later ancestor conflict can be bypassed by backdating child closure.**
   Source Q120/F100/C100 transfers20. The issued child records F20/C20 and is accepted.
   A subsequent +1 source fact correction creates an ancestor allocation conflict.
   Closing the child with `performed_at` from before that correction succeeds. The
   workflow timestamp fence checks only the child's events/control history, and its
   closure projection uses the requested historical cutoff. Current ancestor safety
   must remain mandatory when writing closure. Regression:
   `test_backdated_child_close_cannot_hide_later_ancestor_allocation_conflict`.

2. **P2 — Normal child acceptance invalidates its own review signature.**
   A transferred child with F=C=Q cannot close immediately after its first acceptance,
   even without any changed journal/control quantities. Transfer signature data includes
   the target's full status, so submitted→accepted changes the child's incoming transfer
   signature. Normalize to the disposition relevant to quantities instead of unrelated
   workflow statuses. Regression:
   `test_transferred_child_can_close_after_one_unchanged_acceptance`.

3. **P2 — Historical transfer results acquire future order numbers.**
   Capture a cutoff while the transfer target is draft, then issue that target. The
   same historical report now shows its future assigned order number (and the same
   cutoff's source signature changes), because `_transfers` reads live header numbers.
   Transfer display/signature metadata must use the same as-of headers as quantities.
   Regression:
   `test_historical_reservation_does_not_gain_future_target_number_or_signature`.

## Evidence and bounded review scope

- Independent regression run initially: **3 failed**, each at the expected assertion.
- Existing targeted backend run: **56 passed in 9.98s** across transfers, projection,
  registry and migration tests. No full-suite run was performed by this reviewer.
- Existing tests and code inspection cover lock-protected reservations, exact Decimal
  arithmetic, independent linked journal scopes, correction-root matching capacities,
  protected inherited IDs/quantities, safe draft cancellation, and append-only control
  history. No new writes to primary events, timesheets, labor or payroll were found in
  control command implementations.
- Changes preserve the existing order ledger and use six additional control tables.
  No additional role/authentication system was introduced. No reset, push, deployment,
  cloud action, or production database write was performed in this review.
- UI and XLSX received a focused code inspection for scope and shared projection use;
  browser/full acceptance evidence remains the implementation owner's responsibility.

The architecture and tested core constraints follow the recorded rulings. Spec and
code-quality verdicts are both approved for this bounded review, with no outstanding
findings. This does not replace the owner's full-suite, preservation and browser
acceptance evidence.

## Final analytical follow-up

Scoped review of the two final analytical changes is approved. A closed order whose
late confirmation reversal leaves an unresolved balance now appears consistently in
the registry discrepancy filter, analytical discrepancy rows and conflict header state.
Execution and confirmation counts use their respective linked journal totals; fully
disposed orders retain a separate count, and transfer/writeoff-only orders do not count
as partially executed. The dashboard labels distinguish these measures explicitly.

The two corresponding regressions were run independently: **2 passed in 0.39s**
(`test_old_closed_order_with_revoked_confirmation_is_visible_in_discrepancies` and
`test_disposition_is_not_reported_as_performed_work`). No additional findings arose
within this follow-up scope. No application changes or full-suite run were performed
by the reviewer.
