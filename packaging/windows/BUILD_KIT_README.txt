ПЛАНКТОН. Учёт рабочего времени — W1.0 Windows Build Kit
==================================================
THIS IS A BUILD KIT, NOT A READY PORTABLE DISTRIBUTION.
There is no PlanktonTime.exe inside this archive.
It preserves the current sources, including stages 4.3–4.5 and the rebranding.
Partial 4.2.1 limitations remain documented. No user SQLite database or personal
data is included. This kit has not been built or smoke-tested on Windows.

On a Windows 10/11 x64 BUILD MACHINE:
1. Install CPython 3.13 x64 (recommended 3.13.12).
2. Extract this complete archive into a writable folder.
3. Double-click BUILD_WINDOWS.cmd. The first build downloads pinned dependencies.
   It runs regression tests, PyInstaller onedir and an actual EXE smoke test.
4. The result, only if every build step succeeds, is:
   dist\PlanktonTime-Windows-x64.zip

The END USER of that generated portable ZIP needs neither Python nor Internet.
Extract it, run PlanktonTime.exe and keep the console window open.
Stop through the protected web button or Ctrl+C; wait for the stopped message.

No GitHub upload or CI is used. See docs/windows_portable.md,
docs/windows_build_results.md and packaging/windows/README.md for limitations.
The build recipe itself has not yet been executed on Windows.
For an existing installation, retain the old data profile and database name
data\construction_progress.sqlite3. Follow docs/rebranding_plankton.md before
replacing program files; opening a new empty folder creates a separate profile.
