import argparse
from app.config import DEFAULT_DB_PATH
from app.database import initialize


def main():
    parser = argparse.ArgumentParser(description="Create/migrate and seed a local database")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    db = initialize(args.db)
    try:
        print("SQLite initialized:", args.db)
        print("Buildings:", db.execute("SELECT count(*) FROM buildings").fetchone()[0])
        print("Catalog entries:", db.execute("SELECT count(*) FROM work_catalog").fetchone()[0])
    finally:
        db.close()


if __name__ == "__main__":
    main()
