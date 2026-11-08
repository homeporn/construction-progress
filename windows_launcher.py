"""PyInstaller entry point; no cwd-relative paths or automatic network calls."""
from app.portable.launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
