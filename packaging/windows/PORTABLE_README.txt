Construction Progress - Windows Portable x64
===========================================
Extract the COMPLETE folder into a personal writable directory, such as
Documents\Construction Progress. Run ConstructionProgress.exe.
Do not run from inside the ZIP or move only the EXE out of its folder.
Python and Internet are not required on the target computer.

The browser opens after the local server is ready. If necessary use the
http://127.0.0.1:PORT address printed in the console. Keep that console open.
Stop with the protected web button or Ctrl+C. Wait for the stopped message.
Closing a browser tab does not stop the server. Avoid Task Manager termination
or forcibly closing the console while a write/export is in progress.

Your local profile is beside the EXE:
  data\construction_progress.sqlite3 - working database
  backups\                         - checked Backup API copies
  logs\                            - technical event codes, no form values
  exports\                         - optional user export folder
Browser downloads use the browser's own download location.

For a separately stored profile, run from a shortcut/console:
  ConstructionProgress.exe --data-dir "C:\Users\YOUR_NAME\Documents\CP Profile"
Always reuse that same --data-dir when starting, backing up or updating.

Read-only diagnostics:
  ConstructionProgress.exe --diagnose
Checked backup AFTER stopping the running application:
  ConstructionProgress.exe --backup
Do not use an ordinary file copy of an open SQLite database as a backup.

UPDATE:
1. Stop the old version and run its --backup command.
2. Extract the new portable ZIP into a NEW folder.
3. Move the stopped profile's data/backups/logs/exports folders to the new folder,
   or keep using the same external --data-dir.
4. Start the new version. Before pending migrations, it checks a backup and
   preflights the migration chain on a separate copy. On failure it stops and
   reports the backup path. Do not replace your database with an empty/demo DB.

The server binds only to 127.0.0.1. Localhost is not a user authentication system.
Keep personnel data and backups in your personal protected Windows profile.
This release is unsigned; do not disable Windows security for an unknown file.
BUILD_INFO.json records the actual Windows build and EXE smoke-test environment.
Interactive acceptance in Edge/Chrome and offline use must also be checked.
