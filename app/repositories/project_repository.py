"""Workspaces own buildings and plan baselines; the work catalog is global."""
from app.time_utils import instant
from .base import ValidationError, transaction


def clean(value, label, limit=200, required=True):
    if not isinstance(value, str):
        raise ValidationError(label + ": требуется текст")
    value = value.strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise ValidationError(label + ": проверьте значение")
    return value


class ProjectRepository:
    def __init__(self, db):
        self.db = db

    def list_projects(self, *, include_archived=True):
        return self.db.execute("SELECT * FROM projects WHERE (? OR status='active') ORDER BY id",
                               (include_archived,)).fetchall()

    def get_project(self, project_id):
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ValidationError("Объект не найден")
        return row

    def create_project(self, name, code, description="", *, buildings=()):
        name, code = clean(name, "Название"), clean(code, "Код объекта", 80)
        description = clean(description, "Описание", 4000, False)
        now = instant()
        with transaction(self.db):
            project_id = self.db.execute(
                "INSERT INTO projects(name,code,description,created_at,updated_at) VALUES (?,?,?,?,?)",
                (name, code, description, now, now)).lastrowid
            for order, building in enumerate(buildings, 1):
                self.db.execute("INSERT INTO buildings(project_id,code,name,sort_order) VALUES (?,?,?,?)",
                                (project_id, clean(building["code"], "Код корпуса", 80),
                                 clean(building["name"], "Название корпуса"), order))
            return project_id

    def update_project(self, project_id, name, code, description=""):
        with transaction(self.db):
            self.get_project(project_id)
            self.db.execute("UPDATE projects SET name=?,code=?,description=?,updated_at=? WHERE id=?",
                            (clean(name, "Название"), clean(code, "Код объекта", 80),
                             clean(description, "Описание", 4000, False), instant(), project_id))

    def set_archived(self, project_id, archived):
        with transaction(self.db):
            self.get_project(project_id)
            self.db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?",
                            ("archived" if archived else "active", instant(), project_id))
