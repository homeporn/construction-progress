from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "construction_progress.sqlite3"
MIN_SQLITE = (3, 24, 0)
TARGET_PYTHON = "3.13.12"
