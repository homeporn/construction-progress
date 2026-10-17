from contextlib import contextmanager
import sqlite3


class ValidationError(ValueError):
    """Invalid write; no partial state has been committed."""


@contextmanager
def transaction(db):
    if db.in_transaction:
        raise RuntimeError("Repository operations own their transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise ValidationError(str(exc)) from exc
    except Exception:
        db.rollback()
        raise
