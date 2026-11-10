# ПЛАНКТОН rebranding implementation plan

Goal: change presentation and future build names while preserving every data path,
configuration contract, primary record, migration and calculation.
Architecture: independent app/branding.py supplies names to UI/launch/build layers;
existing SQLite, services, repositories and offline deployment remain unchanged.
Spec: user's detailed rebranding request in this conversation.
Execution: inline implementation with a final independent review; no push or release deploy.

## Change map, recorded before implementation

| Category | Observed locations | Action |
| --- | --- | --- |
| User-facing brand | base.html, stopped.html, favicon, main/server/diagnostic messages | Official Russian name, short wordmark, existing colours |
| Browser titles | base template, standalone stopped/print templates | Home full name; section followed by ` | ПЛАНКТОН`; no duplication |
| Runtime identity/contracts | construction_progress cookies, CP_PYTHON, CP_WINDOWS_RESOURCE_MANIFEST, X-CP headers, local-storage keys, logger | Preserve exactly |
| User data | data/construction_progress.sqlite3, portable profile/data/backups/exports/logs | Preserve all names and discovery logic; PRODUCT_ID does not choose paths |
| Existing XLSX/report bodies | no former brand found in XML cells; reports use domain headings | Preserve worksheet data/structure; change only branded download filename |
| Excel template resource names | Construction_Progress_4_1/4_2_*.xlsx | Preserve agreed asset lookup and filenames; new downloads may use PlanktonTime names |
| Approved/archived documents | frozen DB snapshots, existing XLSX/PDF/images/evidence | Leave byte-identical; print body retains domain headings |
| Future Windows output | spec EXE/COLLECT, build.ps1, bundle/smoke, PE metadata/readmes/version | PlanktonTime.exe, PlanktonTime folder and ZIP; onedir unchanged |
| Windows entry contracts | ConstructionProgress.spec, windows_launcher.py, BUILD_WINDOWS.cmd | Keep source entry paths; spec produces the new name |
| Active instructions | README, Mac first-run/checklist, Windows portable/build instructions | New brand with explicit migration/update instructions; old profile paths preserved |
| Historical documentation | stage3.x–4.x results, W1.0 results, saved screenshots/benchmarks | Preserve evidence; explain remaining references in inventory |
| Cloud | no Docker/Railway/compose deployment configuration found | No deployment, new routes, security or secret changes |

## Global constraints

- Official: ПЛАНКТОН. Учёт рабочего времени; family ПЛАНКТОН, Latin PLANKTON.
- PRODUCT_ID=plankton_time, PRODUCT_SLUG=PlanktonTime only for technical metadata/builds.
- Existing construction_progress.sqlite3, cookies, headers, environment and profile paths stay.
- No migration/table/column/repository/calculation changes; Decimal and business rules stay.
- No external images/fonts/frameworks; no redesign/navigation restructuring/PlanCoreAI changes.
- No cloud publication, GitHub push, native Windows/macOS build claim.

## Review focus

1. New EXE in the existing installation must open the SAME SQLite, not plankton_time.sqlite3.
2. Base-title overrides and standalone pages must preserve correct escaping and no duplicate brand.
3. Windows artifact names must agree across spec/build/validator/ZIP/smoke/PE metadata/docs.
4. An old protective cookie/header/lock/env key must not silently change under PRODUCT_ID.
5. Existing approved exports/snapshots must not acquire altered headers or numerical cells.

## Task1 — baseline/audit

- [x] Read actual code and user documents; record SHA-256/Git/SQLite read-only status.
- [x] Full baseline:1128 passed,7 skipped,158.36s; skips are existing sandbox socket cases.
- [x] Save145 initial textual brand references, no old brand inside tracked XLSX XML cells.

## Task2 — centralized UI and launch naming

Files: new app/branding.py; existing base/stopped/print/settings templates,
navigation.css/favicon.svg; main.py, scripts/check_environment.py, app/web/server.py,
app/portable/launcher.py/storage.py; minimal template context in app/web/__init__.py.
Interfaces: pure constants PRODUCT_FAMILY_NAME, PRODUCT_FAMILY_LATIN, PRODUCT_NAME,
PRODUCT_SUBTITLE, PRODUCT_ID, PRODUCT_SLUG; no core library dependency on these names.

- [x] Test real routes/titles and malformed Origin/Host/CSRF; see unchanged old cookie.
- [x] Test new and legacy executable paths resolve the same populated database.
- [x] Test help/diagnostic from Unicode/foreign cwd without creating a database.
- [x] Add names, wordmark and neutral П SVG; preserve route/menu/security semantics.
- [x] Adjust only branded download names; no report writer or archived document changes.

## Task3 — future Windows Build Kit

Files: packaging/windows spec/build/PE/readmes/VERSION, tools/windows bundle/smoke.
Consumers use PRODUCT_SLUG for future executable/folder/ZIP. Old spec entry and
CP_WINDOWS_RESOURCE_MANIFEST stay; do not rename installed folders/databases.

- [x] Add failing artifact-name/kit-layout test; preserve resource template lookup.
- [x] Apply coordinated output/metadata names, current app identity in diagnostics/BUILD_INFO.
- [x] Build source-only ZIP using existing exclusive creation and explicit resources.
- [x] Native Windows remains unbuilt/untested; no Linux artifact labelled Windows EXE.

## Task4 — documentation and exhaustive retained-reference inventory

- [x] Update README and active Mac/Windows/user instructions, preserve all historical reports.
- [x] Write docs/rebranding_plankton.md with compatibility and update procedure.
- [x] Generate retained_references.json/md: every remaining old-name line/path, category,
  reason; exclude recursive audit evidence and identify those exclusions explicitly.

## Task5 — final validation

- [x] Run full regression and one Chromium attempt if available; honest blocked status.
- [x] Hash-check old migrations/core/business repositories/archive exports/live SQLite.
- [x] Fresh independent review of UI/build/path compatibility; fix concrete issues only.
- [x] Finish report with exact tests/artifacts/limitations and changed-file manifest; no push.

Pre-flight: Task2 names feed Task3 build tools. Existing resource and data constants are
consumed without renaming in both. Archived document branding was absent, so adding a
global workbook creator/header would be an unnecessary historical-format change:
keep report bodies/writer byte-identical and rebrand the existing download label only.

## Completion evidence

Final full regression:1163 passed,7 skipped,0 failed;160.75s.
SHA-256 verification:159 protected files unchanged; working SQLite hash/mtime
unchanged; integrity/FKs valid; no migration or replacement DB created.
Independent review found a new diagnostic-test ANSI decoding issue, reproduced
and fixed with explicit UTF-8. Chromium was attempted once and blocked before
page load by crashpad setsockopt EPERM/SIGTRAP; visual acceptance and screenshots
remain unperformed, with manual Mac/Safari instructions in the report.
Windows output is a source-only Build Kit; native EXE/build/smoke remain unperformed.
No commit, GitHub push or deployment. Full details:../rebranding_plankton.md.
