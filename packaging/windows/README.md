# Windows onedir build

Run on Windows 10/11 x64 with CPython 3.13 x64 (recommended **3.13.12**):

```powershell
& .\packaging\windows\build.ps1
# Or with an explicit Python installation:
& .\packaging\windows\build.ps1 -Python 'C:\Python313\python.exe'
```

The script creates an isolated build venv, installs pinned binary dependencies,
runs the full test collection (macOS Bash tests are explicitly skipped), builds
PyInstaller **onedir**, verifies resource hashes, runs the actual EXE in a private
Unicode profile, and then creates `dist/ConstructionProgress-Windows-x64.zip`.
Existing release archives are never overwritten; previous application folders
and their databases are never build targets. The temporary native portable
folder is printed at the end. No GitHub Actions or push is involved.

For an offline build, prepare wheels on a matching Windows/Python machine and use
`-Wheelhouse 'D:\wheels'`. The destination application's ordinary use is offline;
the initial online build needs access to PyPI unless a wheelhouse is provided.

All Jinja templates, static resources, migrations 001–010, Excel templates and
Flask/Waitress metadata are included explicitly. Runtime SQLite comes with Python.
Excel uses the existing standard-library OOXML implementation; no new Excel
library is introduced. `_internal` contains resources, never the working DB.

`build.ps1` and the Windows EXE have **not been executed on Windows in the current
Linux environment**. The source Build Kit is not a finished portable release.
See [instructions](../../docs/windows_portable.md) and
[verification results](../../docs/windows_build_results.md).
