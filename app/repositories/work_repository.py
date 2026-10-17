from decimal import Decimal

from app.decimal_utils import canonical, multiply, subtract
from app.models import Discipline
from app.time_utils import end_of_day, instant
from .base import ValidationError, transaction


class WorkRepository:
    def __init__(self, db):
        self.db = db

    def add_floor(self, building_id, floor_code, sort_order):
        with transaction(self.db):
            return self.db.execute(
                "INSERT INTO floors(building_id,floor_code,sort_order) VALUES (?,?,?)",
                (building_id, floor_code, sort_order)).lastrowid

    def reorder_floor(self, floor_id, sort_order):
        with transaction(self.db):
            if self.db.execute("UPDATE floors SET sort_order=? WHERE id=?", (sort_order, floor_id)).rowcount != 1:
                raise ValidationError("Этаж не найден")

    def delete_floor(self, floor_id):
        with transaction(self.db):
            if self.db.execute("SELECT 1 FROM work_items WHERE floor_id=?", (floor_id,)).fetchone():
                raise ValidationError("Этаж используется в учёте; удаление запрещено")
            if self.db.execute("DELETE FROM floors WHERE id=?", (floor_id,)).rowcount != 1:
                raise ValidationError("Этаж не найден")

    def update_catalog_entry(self, catalog_id, code, work_type, element, name, unit):
        with transaction(self.db):
            if self.db.execute("SELECT 1 FROM work_items WHERE catalog_id=?", (catalog_id,)).fetchone():
                raise ValidationError("Код уже используется; создайте новый код для изменения классификации")
            cursor = self.db.execute(
                "UPDATE work_catalog SET code=?,work_type=?,element=?,name=?,unit=? WHERE id=?",
                (code, work_type, element, name, unit, catalog_id))
            if cursor.rowcount != 1:
                raise ValidationError("Код работы не найден")

    def list_buildings(self):
        return self.db.execute("SELECT * FROM buildings ORDER BY id").fetchall()

    def list_floors(self, building_id=None):
        return self.db.execute(
            "SELECT f.*,b.name AS building_name,EXISTS(SELECT 1 FROM work_items w WHERE w.floor_id=f.id) AS used "
            "FROM floors f JOIN buildings b ON b.id=f.building_id "
            "WHERE (? IS NULL OR f.building_id=?) ORDER BY b.id,f.sort_order,f.id",
            (building_id, building_id)).fetchall()

    def list_catalog(self, *, search="", element="", work_type=""):
        rows = self.db.execute(
            "SELECT c.*,EXISTS(SELECT 1 FROM work_items w WHERE w.catalog_id=c.id) AS used "
            "FROM work_catalog c ORDER BY c.code").fetchall()
        # Python casefold supports Cyrillic, unlike SQLite's built-in lower().
        return [row for row in rows if (not element or row["element"] == element)
                and (not work_type or row["work_type"] == work_type)
                and (not search or search.casefold() in " ".join(
                    str(row[x]) for x in ("code", "name", "element", "work_type", "unit")).casefold())]

    def list_work_items(self, *, discipline=None, building_id=None, floor_id=None, element="", work_type=""):
        return self.db.execute(
            "SELECT w.*,c.code,c.name,c.unit,c.element,c.work_type,b.name AS building_name,f.floor_code "
            "FROM work_items w JOIN work_catalog c ON c.id=w.catalog_id "
            "JOIN buildings b ON b.id=w.building_id JOIN floors f ON f.id=w.floor_id "
            "WHERE (? IS NULL OR w.discipline=?) AND (? IS NULL OR w.building_id=?) "
            "AND (? IS NULL OR w.floor_id=?) AND (?='' OR c.element=?) AND (?='' OR c.work_type=?) "
            "ORDER BY b.id,f.sort_order,f.id,c.code,w.id",
            (discipline, discipline, building_id, building_id, floor_id, floor_id,
             element, element, work_type, work_type)).fetchall()

    def list_plan_versions(self, discipline):
        return self.db.execute("SELECT * FROM plan_versions WHERE discipline=? ORDER BY version_no DESC",
                               (Discipline(discipline).value,)).fetchall()

    def get_plan_version(self, version_id):
        return self._version(version_id)

    def list_plan_lines(self, version_id):
        return self.db.execute("SELECT * FROM plan_lines WHERE plan_version_id=? ORDER BY work_item_id",
                               (version_id,)).fetchall()

    def add_catalog_entry(self, code, work_type, element, name, unit):
        with transaction(self.db):
            return self.db.execute(
                "INSERT INTO work_catalog(code,work_type,element,name,unit) VALUES (?,?,?,?,?)",
                (code, work_type, element, name, unit)).lastrowid

    def add_work_item(self, building_id, floor_id, catalog_id, discipline=Discipline.CONSTRUCTION):
        with transaction(self.db):
            return self.db.execute(
                "INSERT INTO work_items(building_id,floor_id,catalog_id,discipline) VALUES (?,?,?,?)",
                (building_id, floor_id, catalog_id, Discipline(discipline).value)).lastrowid

    def create_plan_version(self, discipline, *, base_version_id=None, created_at=None):
        discipline = Discipline(discipline).value
        created = instant(created_at)
        with transaction(self.db):
            if base_version_id is not None:
                base = self._version(base_version_id)
                if base["discipline"] != discipline or base["status"] != "APPROVED":
                    raise ValidationError("Clone an approved plan of the same discipline")
                if created < base["approved_at"]:
                    raise ValidationError("A new version cannot predate its baseline")
            number = self.db.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE discipline=?",
                (discipline,)).fetchone()[0]
            version_id = self.db.execute(
                "INSERT INTO plan_versions(discipline,version_no,created_at) VALUES (?,?,?)",
                (discipline, number, created)).lastrowid
            if base_version_id is not None:
                self.db.execute(
                    "INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) "
                    "SELECT ?,work_item_id,planned_quantity,labor_norm,unit FROM plan_lines WHERE plan_version_id=?",
                    (version_id, base_version_id))
            return version_id

    def add_plan_line(self, plan_version_id, work_item_id, planned_quantity, labor_norm=None, *, unit=None):
        quantity = canonical(planned_quantity)
        norm = canonical(labor_norm) if labor_norm is not None else None
        with transaction(self.db):
            self._draft(plan_version_id)
            item = self._item(work_item_id)
            return self.db.execute(
                "INSERT INTO plan_lines(plan_version_id,work_item_id,planned_quantity,labor_norm,unit) "
                "VALUES (?,?,?,?,?)",
                (plan_version_id, work_item_id, quantity, norm, item["unit"] if unit is None else unit)).lastrowid

    def update_plan_line(self, plan_version_id, work_item_id, planned_quantity, labor_norm=None, *, unit=None):
        """Only drafts are editable. Clone an approved version before changing it."""
        quantity = canonical(planned_quantity)
        norm = canonical(labor_norm) if labor_norm is not None else None
        with transaction(self.db):
            self._draft(plan_version_id)
            item = self._item(work_item_id)
            cursor = self.db.execute(
                "UPDATE plan_lines SET planned_quantity=?,labor_norm=?,unit=? "
                "WHERE plan_version_id=? AND work_item_id=?",
                (quantity, norm, item["unit"] if unit is None else unit, plan_version_id, work_item_id))
            if cursor.rowcount != 1:
                raise ValidationError("Position is absent from this version")

    def approve_plan_version(self, plan_version_id, *, approved_at=None):
        with transaction(self.db):
            self._draft(plan_version_id)
            self.db.execute("UPDATE plan_versions SET status='APPROVED',approved_at=? WHERE id=?",
                            (instant(approved_at), plan_version_id))

    def get_active_plan_version(self, discipline, *, report_date=None, knowledge_cutoff=None):
        """Approval is both the effective date and the availability timestamp."""
        params = [Discipline(discipline).value]
        sql = "SELECT * FROM plan_versions WHERE discipline=? AND status='APPROVED'"
        if report_date is not None:
            sql += " AND approved_at<=?"
            params.append(end_of_day(report_date))
        if knowledge_cutoff is not None:
            sql += " AND approved_at<=?"
            params.append(instant(knowledge_cutoff))
        sql += " ORDER BY approved_at DESC,version_no DESC LIMIT 1"
        return self.db.execute(sql, params).fetchone()

    def compare_plan_versions(self, before_id, after_id):
        before, after = self._version(before_id), self._version(after_id)
        if before["discipline"] != after["discipline"]:
            raise ValidationError("Cannot compare design and construction plans")
        lines = []
        for version_id in (before_id, after_id):
            lines.append({row["work_item_id"]: dict(row) for row in self.db.execute(
                "SELECT * FROM plan_lines WHERE plan_version_id=?", (version_id,))})
        changes = {}
        for item_id in sorted(set(lines[0]) | set(lines[1])):
            left, right = lines[0].get(item_id), lines[1].get(item_id)
            if left and right and left["unit"] != right["unit"]:
                raise ValidationError("Incompatible plan units")
            quantities = [Decimal(x["planned_quantity"]) if x else Decimal(0) for x in (left, right)]
            norms = [Decimal(x["labor_norm"]) if x and x["labor_norm"] is not None else None
                     for x in (left, right)]
            labors = [multiply(q, n) if n is not None else (Decimal(0) if x is None else None)
                      for q, n, x in zip(quantities, norms, (left, right))]
            changes[item_id] = {
                "change": "ADDED" if left is None else "REMOVED" if right is None else "EXISTING",
                "before": left, "after": right,
                "planned_quantity_delta": subtract(quantities[1], quantities[0]),
                "labor_norm_delta": subtract(norms[1], norms[0]) if all(n is not None for n in norms) else None,
                "planned_labor_delta": subtract(labors[1], labors[0]) if all(t is not None for t in labors) else None,
            }
        return changes

    def _version(self, version_id):
        row = self.db.execute("SELECT * FROM plan_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise ValidationError("Plan version does not exist")
        return row

    def _draft(self, version_id):
        row = self._version(version_id)
        if row["status"] != "DRAFT":
            raise ValidationError("Approved plan is immutable; create a new version")
        return row

    def _item(self, item_id):
        row = self.db.execute(
            "SELECT w.*,c.unit FROM work_items w JOIN work_catalog c ON c.id=w.catalog_id WHERE w.id=?",
            (item_id,)).fetchone()
        if row is None:
            raise ValidationError("Work item does not exist")
        return row
