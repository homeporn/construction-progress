# Stage4.5 UI and export report

Extended the existing `work_orders` blueprint and card. Old order/edit/print URLs
remain canonical; the registry uses shared control projections and server pages
of 50, including date/knowledge slices and all structural/derived filters.
Production navigation now includes «Контроль нарядов» in the existing menu.

The same card shows separate linked F/C, execution/confirmation remainders,
reserved/sent transfers, writeoffs, safe available quantity, matching uncertainty,
conflicts, complete connected-chain totals/links and append-only decision history.
Transfer explicitly selects source rows/amounts, new shift, contractor, crew and
brigades; it creates one reserved draft, preserving submitted values on errors.
Bound selects offer local search, and changing shift date refreshes available
workers through the existing scoped endpoint. Matching roots are server-bounded
50 per journal with exact-ID/comment search; updates require their revision,
including zero-quantity reversal. Existing-pair searches use the same bounded ID/reason filtering. Writeoff/revoke and safe transfer cancellation
use explicit reasons and idempotency keys. Inherited draft work selectors/quantities are locked while header, crew and
remarks remain editable; hidden IDs and readonly quantities still serialize.
No-fact disposed issued orders expose explicit submission. Accepted orders can add real confirmations and repeat review.

The seven-sheet offline XLSX reuses `xlsx_export.py`; no dependencies or engine
changes. Numeric Decimals, typed dates, exact cutoff/filter metadata, original
chain quantities and linked journal sources are retained. Scoped writeoff/revoke
and matching history use the same read snapshot and effective knowledge/date
cutoff as analytics. Executor snapshot text exceeding Excel's cell limit is
losslessly split into numbered parts on sheet seven, with original cell reference
and SHA256; user strings remain literal cells, including formula-like text.

Validation:

- Initial red web checks: missing card controls/match route and 51 rendered rows.
- `.venv/bin/python -m pytest tests/test_work_order_control_web.py tests/test_work_order_web.py -q --disable-warnings --maxfail=3`
  — 47 passed (final run; includes inherited-draft serialization, UTF-16-safe
  snapshot chunks, matching search and scoped source units).
- Tests cover partial/full/no-fact transfer, issuance-before-close, mandatory repeat
  review, three-node chain, cancellation, matching revisions/reversal, stale
  conflict, writeoff/revoke, preservation on invalid forms, actual 50+1 paging,
  archived/foreign scope, CSRF/Origin/Host/workspace guards, all-seven-sheet numeric
  parity, empty metadata, literal text, oversized snapshot reassembly/SHA and
  knowledge-sliced primary/control sources.
- `node --check` on both work-order JavaScript files and Python compilation
  passed. No full suite run by this agent; browser acceptance belongs to the
  independent browser/root acceptance work. Flask tests are not browser evidence.

Additional owned UI files: `_work_order_control_filters.html` and
`static/js/work_order_control.js`. No journal/timesheet/payroll data, authentication,
deployment, dependency, commit or push changes were made by this task.
