# Stage 4.5 acceptance evidence

The disposable Flask acceptance passed. Chromium was blocked before page creation: crashpad `setsockopt` returned `Operation not permitted (1)`, and the process terminated with `SIGTRAP`. Exactly one launch used the default execution environment. No escalation, network permission request, alternate launch flags or screenshot substitution was attempted.

Browser interactions, layout, JavaScript, console and browser HTTP observations remain **unobserved**. Their JSON fields are `null`; no screenshots exist. Safari and native TCP/WSGI transport were not tested. The optional browser harness uses Chromium request interception to a real Flask `test_client` over subprocess stdio.

The independent Flask route acceptance uses a new temporary SQLite database, current UTC dates, real domain repositories and real rendered forms. CSRF, workspace and Origin guards remain enabled. Named issuer, foreman, contractor, brigade and crew selections are ordinary application data; no authentication/session identity is fabricated. The working database was never opened.

| Case | Observed Flask result |
| --- | --- |
| A | Assignment 120, linked fact/confirmation 100, transfer 20 creates a draft, source available 0, original chain 120. |
| B | Assignment 40 splits into 25 and 15; source available 0. |
| C | Chain 120 → 20 → 5 preserves original 120, confirmed 115 and open 5. The recipient's fact and confirmation 15 are explicit separate route posts. |
| D | Two controlled stale-form posts of 15 from 20 produce 303 then 400; one transfer 15 and availability 5. This is a sequential route conflict. |
| E | Cancelling the draft transfer 20 restores availability 20, cancels the recipient and retains transfer/cancellation history. |
| F | Ordinary fact 12 and confirmation 7 save through their journal routes without a work-order link. |

All eight transfer submissions, including the rejected conflict, leave complete primary journal, timesheet, labor and payroll table hashes unchanged. Cancellation also leaves those sources unchanged. This fixture has ten explicit primary journal events in total and no timesheet, labor or payroll rows; the unchanged empty tables do not establish behavior on populated financial data. Assignment, linked fact/confirmation and availability on all eleven rendered source/recipient cards equal repository values. Missing CSRF and foreign Origin return 403.

The real [synthetic XLSX export](../examples/stage45_work_order_control.xlsx) contains seven sheets with filters. Registry, control, residual/discrepancy lines, transfers, chain totals and source numeric values match the same repository slice. The validation examines 902 numeric cells, numeric dates and UTC timestamps, and literal `=SUM(1,2)` text; no formulas exist. The workbook is synthetic evidence, not an operational report.

Execution evidence is in [browser_results.json](browser_results.json). `python tests/browser_stage45.py` returned status `blocked` with backend `passed` (exit 2). The later `python tests/browser_stage45.py --refresh-backend` refreshed Flask/XLSX observations while retaining the original single blocked Chromium launch. Use this refresh option for further backend runs.

Separate repository concurrency verification: `.venv/bin/python -m pytest -q tests/test_work_order_transfers.py::test_two_real_connections_cannot_transfer_30_from_20` passed: **1 passed in 0.17s**. That test uses two SQLite connections and a barrier; it establishes one accepted and one rejected 15-from-20 transfer. It is repository evidence, not a browser observation. An earlier direct `.venv/bin/pytest` invocation failed to import `app`; running pytest as a Python module resolved that invocation issue.
