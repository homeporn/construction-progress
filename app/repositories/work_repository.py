from decimal import Decimal

from app.decimal_utils import canonical, multiply, subtract
from app.models import Discipline
from app.time_utils import end_of_day, instant
from .base import ValidationError, transaction
from .building_dependencies import building_dependencies


class WorkRepository:
    def __init__(self, db, project_id=1):
        self.db = db
        self.project_id = project_id

    def _writable(self):
        row = self.db.execute("SELECT status FROM projects WHERE id=?", (self.project_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise ValidationError("Объект архивирован или не найден. Изменения запрещены")

    def _building(self, building_id, *, writable=False):
        row = self.db.execute("SELECT * FROM buildings WHERE id=? AND project_id=?",
                              (building_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Корпус не принадлежит текущему объекту")
        if writable:
            self._writable()
            if row["status"] != "active":
                raise ValidationError("Корпус архивирован. Сначала восстановите его")
        return row

    def add_building(self, code, name, sort_order=0):
        from .project_repository import clean
        with transaction(self.db):
            self._writable()
            return self.db.execute("INSERT INTO buildings(project_id,code,name,sort_order) VALUES (?,?,?,?)",
                                   (self.project_id, clean(code, "Код корпуса", 80),
                                    clean(name, "Название корпуса"), sort_order)).lastrowid

    def update_building(self, building_id, code, name, sort_order):
        from .project_repository import clean
        with transaction(self.db):
            self._writable()
            self._building(building_id)
            self.db.execute("UPDATE buildings SET code=?,name=?,sort_order=? WHERE id=?",
                            (clean(code, "Код корпуса", 80), clean(name, "Название корпуса"), sort_order, building_id))

    def archive_building(self, building_id, archived=True):
        with transaction(self.db):
            self._writable()
            self._building(building_id)
            self.db.execute("UPDATE buildings SET status=? WHERE id=?", ("archived" if archived else "active", building_id))

    def building_deletion_info(self, building_id):
        building = self._building(building_id)
        floors = self.list_floors(building_id)
        dependencies = building_dependencies(self.db, building_id)
        return dict(building=building, floors=floors, dependencies=dependencies, can_delete=not dependencies)

    def delete_building(self, building_id, *, delete_empty_floors=False):
        """Recheck all references under the write lock; never cascade records."""
        with transaction(self.db):
            self._writable()
            info = self.building_deletion_info(building_id)
            if not info["can_delete"]:
                raise ValidationError("Корпус содержит учётные или связанные данные. Используйте архивирование")
            if info["floors"] and not delete_empty_floors:
                raise ValidationError("Подтвердите удаление пустых этажей вместе с корпусом")
            # Explicit deletion only after checking that every floor is empty.
            self.db.execute("DELETE FROM floors WHERE building_id=?", (building_id,))
            self.db.execute("DELETE FROM buildings WHERE id=? AND project_id=?", (building_id, self.project_id))

    def _floor(self, floor_id):
        row = self.db.execute("SELECT f.* FROM floors f JOIN buildings b ON b.id=f.building_id "
                              "WHERE f.id=? AND b.project_id=?", (floor_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Этаж не принадлежит текущему объекту")
        return row

    def add_floor(self, building_id, floor_code, sort_order):
        with transaction(self.db):
            self._building(building_id, writable=True)
            return self.db.execute(
                "INSERT INTO floors(building_id,floor_code,sort_order) VALUES (?,?,?)",
                (building_id, floor_code, sort_order)).lastrowid

    def reorder_floor(self, floor_id, sort_order):
        with transaction(self.db):
            self._writable()
            self._floor(floor_id)
            if self.db.execute("UPDATE floors SET sort_order=? WHERE id=?", (sort_order, floor_id)).rowcount != 1:
                raise ValidationError("Этаж не найден")

    def delete_floor(self, floor_id):
        with transaction(self.db):
            self._writable()
            self._floor(floor_id)
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

    def list_buildings(self, *, include_archived=True):
        """Reports include the archive; new-entry forms request active rows."""
        return self.db.execute("SELECT * FROM buildings WHERE project_id=? AND (? OR status='active') ORDER BY sort_order,id",
                               (self.project_id, include_archived)).fetchall()

    def list_floors(self, building_id=None, *, include_archived=True):
        return self.db.execute(
            "SELECT f.*,b.name AS building_name,EXISTS(SELECT 1 FROM work_items w WHERE w.floor_id=f.id) AS used "
            "FROM floors f JOIN buildings b ON b.id=f.building_id "
            "WHERE b.project_id=? AND (? IS NULL OR f.building_id=?) AND (? OR b.status='active') "
            "ORDER BY b.sort_order,b.id,f.sort_order,f.id",
            (self.project_id, building_id, building_id, include_archived)).fetchall()

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
            "WHERE b.project_id=? AND (? IS NULL OR w.discipline=?) AND (? IS NULL OR w.building_id=?) "
            "AND (? IS NULL OR w.floor_id=?) AND (?='' OR c.element=?) AND (?='' OR c.work_type=?) "
            "ORDER BY b.id,f.sort_order,f.id,c.code,w.id",
            (self.project_id, discipline, discipline, building_id, building_id, floor_id, floor_id,
             element, element, work_type, work_type)).fetchall()

    def list_plan_versions(self, discipline):
        return self.db.execute("SELECT * FROM plan_versions WHERE project_id=? AND discipline=? ORDER BY version_no DESC",
                               (self.project_id, Discipline(discipline).value)).fetchall()

    def get_plan_version(self, version_id):
        return self._version(version_id)

    def list_plan_lines(self, version_id):
        self._version(version_id)
        return self.db.execute("SELECT * FROM plan_lines WHERE plan_version_id=? ORDER BY work_item_id",
                               (version_id,)).fetchall()

    def add_catalog_entry(self, code, work_type, element, name, unit):
        with transaction(self.db):
            return self.db.execute(
                "INSERT INTO work_catalog(code,work_type,element,name,unit) VALUES (?,?,?,?,?)",
                (code, work_type, element, name, unit)).lastrowid

    def add_work_item(self, building_id, floor_id, catalog_id, discipline=Discipline.CONSTRUCTION):
        with transaction(self.db):
            self._building(building_id, writable=True)
            self._floor(floor_id)
            return self.db.execute(
                "INSERT INTO work_items(building_id,floor_id,catalog_id,discipline) VALUES (?,?,?,?)",
                (building_id, floor_id, catalog_id, Discipline(discipline).value)).lastrowid

    def create_plan_version(self, discipline, *, base_version_id=None, created_at=None):
        discipline = Discipline(discipline).value
        created = instant(created_at)
        with transaction(self.db):
            self._writable()
            if base_version_id is not None:
                base = self._version(base_version_id)
                if base["discipline"] != discipline or base["status"] != "APPROVED":
                    raise ValidationError("Clone an approved plan of the same discipline")
                if created < base["approved_at"]:
                    raise ValidationError("A new version cannot predate its baseline")
            number = self.db.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE project_id=? AND discipline=?",
                (self.project_id, discipline)).fetchone()[0]
            version_id = self.db.execute(
                "INSERT INTO plan_versions(project_id,discipline,version_no,created_at) VALUES (?,?,?,?)",
                (self.project_id, discipline, number, created)).lastrowid
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
        params = [self.project_id, Discipline(discipline).value]
        sql = "SELECT * FROM plan_versions WHERE project_id=? AND discipline=? AND status='APPROVED'"
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
        row = self.db.execute("SELECT * FROM plan_versions WHERE id=? AND project_id=?", (version_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Plan version does not exist")
        return row

    def _draft(self, version_id):
        self._writable()
        row = self._version(version_id)
        if row["status"] != "DRAFT":
            raise ValidationError("Approved plan is immutable; create a new version")
        return row

    def _item(self, item_id):
        row = self.db.execute(
            "SELECT w.*,c.unit,b.project_id,b.status AS building_status FROM work_items w "
            "JOIN work_catalog c ON c.id=w.catalog_id JOIN buildings b ON b.id=w.building_id "
            "WHERE w.id=? AND b.project_id=?", (item_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Work item does not exist")
        return row

    def remove_plan_line(self, version_id, item_id):
        with transaction(self.db):
            self._draft(version_id)
            self._item(item_id)
            if self.db.execute("DELETE FROM plan_lines WHERE plan_version_id=? AND work_item_id=?",
                               (version_id, item_id)).rowcount != 1:
                raise ValidationError("Позиция отсутствует в версии")

    def available_work_items(self, discipline, **filters):
        """Current approved baseline only; drafts and removed lines never leak in."""
        version = self.get_active_plan_version(discipline, report_date=instant()[:10], knowledge_cutoff=instant())
        if version is None:
            return []
        ids = {row["work_item_id"] for row in self.list_plan_lines(version["id"])}
        return [row for row in self.list_work_items(discipline=Discipline(discipline).value, **filters)
                if row["id"] in ids and self._building(row["building_id"])["status"] == "active"]
