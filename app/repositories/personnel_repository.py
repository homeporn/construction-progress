import re

from app.time_utils import instant
from .base import ValidationError, transaction
from .project_repository import clean
from .work_repository import WorkRepository


class PersonnelRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.work = WorkRepository(db, project_id)

    def _row(self, table, record_id, *, active=False):
        # Table names are private constants chosen by repository methods.
        row = self.db.execute("SELECT * FROM {} WHERE id=? AND project_id=?".format(table),
                              (record_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Запись справочника не принадлежит текущему объекту")
        if active and not row["is_active"]:
            raise ValidationError("Запись справочника архивирована")
        return row

    def contractor(self, record_id, *, active=False):
        return self._row("contractors", record_id, active=active)

    def employee(self, record_id, *, active=False):
        return self._row("employees", record_id, active=active)

    def specialty(self, record_id):
        row = self.db.execute("SELECT * FROM specialties WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise ValidationError("Специальность не найдена")
        return row

    def list_contractors(self, *, active_only=False):
        return self.db.execute("SELECT * FROM contractors WHERE project_id=? AND (?=0 OR is_active=1) ORDER BY name,id",
                               (self.project_id, active_only)).fetchall()

    def list_employees(self, *, active_only=False):
        return self.db.execute("SELECT e.*,s.name AS specialty_name,c.name AS contractor_name FROM employees e "
                               "LEFT JOIN specialties s ON s.id=e.specialty_id LEFT JOIN contractors c ON c.id=e.contractor_id "
                               "WHERE e.project_id=? AND (?=0 OR e.is_active=1) ORDER BY e.full_name,e.id",
                               (self.project_id, active_only)).fetchall()

    def list_specialties(self):
        return self.db.execute("SELECT * FROM specialties ORDER BY name,id").fetchall()

    def save_contractor(self, data, contractor_id=None):
        values = [clean(data.get(field, ""), label, limit, field == "name") for field, label, limit in
                  (("name", "Название организации", 200), ("short_name", "Краткое название", 200),
                   ("inn", "ИНН", 12), ("contact_person", "Контактное лицо", 200), ("phone", "Телефон", 80))]
        if values[2] and not re.fullmatch(r"[0-9]{10}|[0-9]{12}", values[2]):
            raise ValidationError("ИНН должен содержать 10 или 12 цифр либо оставаться пустым")
        with transaction(self.db):
            self.work._writable()
            now = instant()
            if contractor_id is None:
                return self.db.execute("INSERT INTO contractors(project_id,name,short_name,inn,contact_person,phone,created_at,updated_at) "
                                       "VALUES (?,?,?,?,?,?,?,?)", [self.project_id] + values + [now, now]).lastrowid
            self.contractor(contractor_id)
            self.db.execute("UPDATE contractors SET name=?,short_name=?,inn=?,contact_person=?,phone=?,updated_at=? WHERE id=?",
                            values + [now, contractor_id])
            return contractor_id

    def save_employee(self, data, employee_id=None):
        name = clean(data.get("full_name", ""), "ФИО")
        position = clean(data.get("position", ""), "Должность", required=False)
        contractor, specialty = data.get("contractor_id"), data.get("specialty_id")
        with transaction(self.db):
            self.work._writable()
            if contractor is not None:
                self.contractor(contractor, active=True)
            if specialty is not None:
                self.specialty(specialty)
            if employee_id is None:
                return self.db.execute("INSERT INTO employees(project_id,contractor_id,full_name,specialty_id,position) VALUES (?,?,?,?,?)",
                                       (self.project_id, contractor, name, specialty, position)).lastrowid
            self.employee(employee_id)
            self.db.execute("UPDATE employees SET contractor_id=?,full_name=?,specialty_id=?,position=? WHERE id=?",
                            (contractor, name, specialty, position, employee_id))
            return employee_id

    def save_specialty(self, name, code, specialty_id=None):
        with transaction(self.db):
            self.work._writable()
            values = (clean(name, "Специальность"), clean(code, "Код специальности", 80))
            if specialty_id is None:
                return self.db.execute("INSERT INTO specialties(name,code) VALUES (?,?)", values).lastrowid
            self.specialty(specialty_id)
            self.db.execute("UPDATE specialties SET name=?,code=? WHERE id=?", values + (specialty_id,))
            return specialty_id

    def set_active(self, kind, record_id, active):
        if kind not in ("contractors", "employees"):
            raise ValidationError("Неизвестный справочник")
        with transaction(self.db):
            self.work._writable()
            self._row(kind, record_id)
            if kind == "contractors":
                self.db.execute("UPDATE contractors SET is_active=?,updated_at=? WHERE id=?", (int(active), instant(), record_id))
            else:
                self.db.execute("UPDATE employees SET is_active=? WHERE id=?", (int(active), record_id))
