import json
import re
from decimal import Decimal
from uuid import uuid4

from app.decimal_utils import canonical
from app.time_utils import day, instant
from app.calculation_engine.historical import read_snapshot
from app.work_order_models import STATUS_LABELS
from .base import ValidationError, transaction
from .personnel_repository import PersonnelRepository
from .project_repository import clean
from .work_repository import WorkRepository


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class WorkOrderRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.work = WorkRepository(db, project_id)
        self.people = PersonnelRepository(db, project_id)

    def _order(self, order_id):
        row = self.db.execute("SELECT * FROM work_orders WHERE id=? AND project_id=?", (order_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError("Наряд не принадлежит текущему объекту")
        return row

    def available_items(self):
        return self.work.available_work_items("CONSTRUCTION")

    def _header(self, data):
        date = day(data.get("shift_date", ""))
        start, end = data.get("shift_start", ""), data.get("shift_end", "")
        if not all(isinstance(t, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in (start, end)) or start == end:
            raise ValidationError("Укажите различное время начала и окончания смены в формате ЧЧ:ММ")
        contractor = self.people.contractor(data.get("contractor_id"), active=True)
        issuer, foreman = data.get("issued_by_employee_id"), data.get("foreman_employee_id")
        for employee_id in (issuer, foreman):
            if employee_id is not None:
                employee = self.people.employee(employee_id, active=True, as_of=date)
                if employee_id == foreman and employee["contractor_id"] not in (None, contractor["id"]):
                    raise ValidationError("Бригадир относится к другой подрядной организации")
        return (date, start, end, contractor["id"], issuer, foreman, clean(data.get("notes", ""), "Примечание", 4000, False))

    def _line_values(self, lines):
        now = instant()
        version = self.work.get_active_plan_version("CONSTRUCTION", report_date=now[:10], knowledge_cutoff=now)
        available = {item["id"]: item for item in self.available_items()}
        seen, result = set(), []
        for line in lines:
            item_id = line.get("work_item_id")
            if item_id in seen:
                raise ValidationError("Учётная позиция не может повторяться в наряде")
            seen.add(item_id)
            if item_id not in available or version is None:
                raise ValidationError("Выберите активную работу из действующего утверждённого плана текущего объекта")
            if line.get("plan_version_id") not in (None, version["id"]):
                raise ValidationError("Версия плана изменилась. Обновите строки наряда")
            quantity = canonical(line.get("planned_quantity"))
            if Decimal(quantity) <= 0:
                raise ValidationError("Плановый объём на смену должен быть положительным")
            item = available[item_id]
            location = {k: item[k] for k in ("building_id", "building_name", "floor_id", "floor_code", "element", "work_type", "code")}
            result.append((item_id, version["id"], quantity, item["unit"], item["name"], encoded(location),
                           len(result) + 1, clean(line.get("remarks", ""), "Примечание строки", 2000, False)))
        return result

    def _crew_values(self, crews, contractor_id, shift_date=None):
        seen_specialties, seen_people, result = set(), set(), []
        for crew in crews:
            specialty = self.people.specialty(crew.get("specialty_id"))
            if specialty["id"] in seen_specialties:
                raise ValidationError("Объедините одну специальность в одну строку бригады")
            seen_specialties.add(specialty["id"])
            count = crew.get("planned_count")
            if count is not None and (type(count) is not int or not 1 <= count <= 999999):
                raise ValidationError("Численность должна быть положительным целым числом до 999999")
            members, names = [], []
            for employee_id in crew.get("employee_ids", []):
                if employee_id in seen_people:
                    raise ValidationError("Работник не может повторяться в составе бригады")
                seen_people.add(employee_id)
                employee = self.people.employee(employee_id, active=True, as_of=shift_date)
                if employee["specialty_id"] != specialty["id"] or employee["contractor_id"] not in (None, contractor_id):
                    raise ValidationError("Работник не соответствует специальности или подрядчику")
                members.append(employee_id)
                names.append(employee["full_name"])
            if count is None and not members:
                raise ValidationError("Укажите численность или работников в строке бригады")
            result.append((specialty["id"], count, clean(crew.get("comment", ""), "Комментарий бригады", 2000, False),
                           specialty["name"], encoded(names), members))
        return result

    def _with_brigades(self, crews, brigade_ids, contractor_id, shift_date):
        from .brigade_repository import BrigadeRepository
        brigades=BrigadeRepository(self.db,self.project_id)
        result=[dict(c,employee_ids=list(c.get('employee_ids',[]))) for c in crews]
        by_specialty={c['specialty_id']:c for c in result}
        selected={e for c in result for e in c['employee_ids']}
        if len(brigade_ids)!=len(set(brigade_ids)):
            raise ValidationError('Бригада не может повторяться в выборе')
        for brigade_id in brigade_ids:
            b=brigades.get(brigade_id,active=True)
            if b['contractor_id']!=contractor_id:raise ValidationError('Бригада относится к другому подрядчику')
            members=[m for m in brigades.members(brigade_id,shift_date) if m['is_active']]
            if not members:raise ValidationError('На дату смены в бригаде нет доступных работников текущего объекта')
            for m in members:
                if m['specialty_id'] is None:raise ValidationError('Сначала заполните специальности работников бригады')
                c=by_specialty.get(m['specialty_id'])
                if c is None:
                    c=dict(specialty_id=m['specialty_id'],planned_count=None,employee_ids=[],comment='')
                    result.append(c);by_specialty[m['specialty_id']]=c
                if m['employee_id'] not in selected:
                    c['employee_ids'].append(m['employee_id']);selected.add(m['employee_id'])
        return result

    def _member_snapshot(self, employee_id, shift_date):
        from .brigade_repository import BrigadeRepository
        member=BrigadeRepository(self.db,self.project_id).current(employee_id,shift_date)
        return (member['brigade_id'],member['number']) if member else (None,'')

    def save_draft(self, header, lines=(), crew=(), *, order_id=None, expected_revision=None, draft_key=None, brigade_ids=()):
        with transaction(self.db):
            self.work._writable()
            if order_id is not None:
                order = self._order(order_id)
                if order["status"] != "draft":
                    raise ValidationError("Редактировать можно только черновик наряда")
                if expected_revision is not None and expected_revision != order["revision"]:
                    raise ValidationError("Черновик изменён в другой вкладке. Откройте его заново")
            elif draft_key:
                existing = self.db.execute("SELECT id,project_id FROM work_orders WHERE draft_key=?", (draft_key,)).fetchone()
                if existing:
                    if existing["project_id"] != self.project_id:
                        raise ValidationError("Форма относится к другому объекту")
                    return existing["id"]
            values = self._header(header)
            line_values = self._line_values(lines)
            if brigade_ids:
                crew=self._with_brigades(crew,list(brigade_ids),values[3],values[0])
            crew_values = self._crew_values(crew, values[3], values[0])
            now = instant()
            if order_id is None:
                order_id = self.db.execute("INSERT INTO work_orders(project_id,shift_date,shift_start,shift_end,contractor_id,"
                    "issued_by_employee_id,foreman_employee_id,notes,created_at,updated_at,draft_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (self.project_id,) + values + (now, now, clean(draft_key or uuid4().hex, "Ключ черновика", 100))).lastrowid
            else:
                self.db.execute("UPDATE work_orders SET shift_date=?,shift_start=?,shift_end=?,contractor_id=?,issued_by_employee_id=?,"
                                "foreman_employee_id=?,notes=?,updated_at=?,revision=revision+1 WHERE id=?", values + (now, order_id))
                self.db.execute("DELETE FROM work_order_crew_members WHERE crew_id IN (SELECT id FROM work_order_crew WHERE work_order_id=?)", (order_id,))
                self.db.execute("DELETE FROM work_order_crew WHERE work_order_id=?", (order_id,))
                self.db.execute("DELETE FROM work_order_lines WHERE work_order_id=?", (order_id,))
            self.db.executemany("INSERT INTO work_order_lines(work_order_id,work_item_id,plan_version_id,planned_quantity,unit_snapshot,"
                                "work_name_snapshot,location_snapshot,sort_order,remarks) VALUES (?,?,?,?,?,?,?,?,?)",
                                [(order_id,) + values for values in line_values])
            for values in crew_values:
                crew_id = self.db.execute("INSERT INTO work_order_crew(work_order_id,specialty_id,planned_count,comment,"
                    "specialty_name_snapshot,employees_snapshot) VALUES (?,?,?,?,?,?)", (order_id,) + values[:5]).lastrowid
                if self.people.extended:
                    self.db.executemany("INSERT INTO work_order_crew_members(crew_id,employee_id,brigade_id,brigade_number_snapshot) VALUES (?,?,?,?)",
                        [(crew_id,e)+self._member_snapshot(e,header['shift_date']) for e in values[5]])
                else:
                    self.db.executemany("INSERT INTO work_order_crew_members(crew_id,employee_id) VALUES (?,?)", [(crew_id, e) for e in values[5]])
            return order_id

    def issue(self, order_id):
        with transaction(self.db):
            self.work._writable()
            order = self._order(order_id)
            if order["status"] in ("issued", "in_progress", "submitted", "accepted", "closed"):
                return order["order_number"]
            if order["status"] != "draft":
                raise ValidationError("Можно выдать только черновик")
            self._header(dict(order))
            if order["issued_by_employee_id"] is None or order["foreman_employee_id"] is None:
                raise ValidationError("Для выдачи укажите мастера / прораба и бригадира")
            lines = self.db.execute("SELECT * FROM work_order_lines WHERE work_order_id=? ORDER BY sort_order,id", (order_id,)).fetchall()
            if not lines:
                raise ValidationError("Добавьте хотя бы одну работу в наряд")
            for line, values in zip(lines, self._line_values([dict(r) for r in lines])):
                self.db.execute("UPDATE work_order_lines SET unit_snapshot=?,work_name_snapshot=?,location_snapshot=? WHERE id=?",
                                values[3:6] + (line["id"],))
            crews = self._read_crew(order_id)
            if not crews:
                raise ValidationError("Добавьте состав бригады: численность или поимённый список")
            for crew, values in zip(crews, self._crew_values(crews, order["contractor_id"], order['shift_date'])):
                self.db.execute("UPDATE work_order_crew SET specialty_name_snapshot=?,employees_snapshot=? WHERE id=?",
                                (values[3], values[4], crew["id"]))
                if self.people.extended:
                    for employee_id in values[5]:
                        self.db.execute('UPDATE work_order_crew_members SET brigade_id=?,brigade_number_snapshot=? WHERE crew_id=? AND employee_id=?',
                            self._member_snapshot(employee_id,order['shift_date'])+(crew['id'],employee_id))
            self.db.execute("INSERT INTO work_order_sequences(project_id,last_number) VALUES (?,1) "
                            "ON CONFLICT(project_id) DO UPDATE SET last_number=last_number+1", (self.project_id,))
            number = self.db.execute("SELECT last_number FROM work_order_sequences WHERE project_id=?", (self.project_id,)).fetchone()[0]
            number = "НЗ-{:04d}".format(number)
            now, header = instant(), self._header_snapshot(order)
            self.db.execute("UPDATE work_orders SET status='issued',order_number=?,issued_at=?,updated_at=?,header_snapshot=? WHERE id=?",
                            (number, now, now, encoded(header), order_id))
            self.db.execute("INSERT INTO work_order_approvals(work_order_id,action,employee_id,employee_name_snapshot,performed_at,comment) "
                            "VALUES (?,'issued',?,?,?,?)", (order_id, order["issued_by_employee_id"], header["issued_by"], now,
                            "Внутренняя регистрация выдачи; не является электронной подписью"))
            return number

    def cancel(self, order_id, reason):
        reason = clean(reason, "Основание отмены", 2000)
        with transaction(self.db):
            self.work._writable()
            order = self._order(order_id)
            if order["status"] not in ("draft", "issued", "in_progress"):
                raise ValidationError("Отмена этого статуса недоступна")
            self.db.execute("UPDATE work_orders SET status='cancelled',cancellation_reason=?,updated_at=? WHERE id=?",
                            (reason, instant(), order_id))

    def _header_snapshot(self, order):
        project = self.db.execute("SELECT name,code FROM projects WHERE id=?", (self.project_id,)).fetchone()
        contractor = self.people.contractor(order["contractor_id"])
        return dict(project_name=project["name"], project_code=project["code"], contractor_name=contractor["name"],
                    contractor_inn=contractor["inn"], issued_by=self.people.employee(order["issued_by_employee_id"])["full_name"] if order["issued_by_employee_id"] else "",
                    foreman=self.people.employee(order["foreman_employee_id"])["full_name"] if order["foreman_employee_id"] else "")

    def _read_crew(self, order_id):
        result = []
        for row in self.db.execute("SELECT * FROM work_order_crew WHERE work_order_id=? ORDER BY id", (order_id,)):
            crew = dict(row)
            crew["employee_ids"] = [r[0] for r in self.db.execute("SELECT employee_id FROM work_order_crew_members WHERE crew_id=? ORDER BY id", (row["id"],))]
            crew["names"] = json.loads(row["employees_snapshot"])
            crew['brigades'] = list(dict.fromkeys(r[0] for r in self.db.execute(
                'SELECT brigade_number_snapshot FROM work_order_crew_members WHERE crew_id=? AND brigade_number_snapshot!=\'\' ORDER BY id',
                (row['id'],)))) if self.people.extended else []
            result.append(crew)
        return result

    def get_order(self, order_id, *, report_date=None, knowledge_cutoff=None):
        with read_snapshot(self.db):
            return self._get_order(order_id, report_date=report_date, knowledge_cutoff=knowledge_cutoff)

    def _get_order(self, order_id, *, report_date=None, knowledge_cutoff=None):
        order = dict(self._order(order_id))
        order["header"] = json.loads(order["header_snapshot"]) if order["header_snapshot"] else self._header_snapshot(order)
        order["lines"] = [dict(row) for row in self.db.execute("SELECT * FROM work_order_lines WHERE work_order_id=? ORDER BY sort_order,id", (order_id,))]
        for line in order["lines"]:
            line["location"] = json.loads(line["location_snapshot"])
        order["crew"] = self._read_crew(order_id)
        order["warnings"] = ["{}: численность {} и поимённый список ({}) отличаются".format(c["specialty_name_snapshot"], c["planned_count"], len(c["names"]))
                             for c in order["crew"] if c["planned_count"] is not None and c["planned_count"] != len(c["names"])]
        order["history"] = self.db.execute("SELECT * FROM work_order_status_history WHERE work_order_id=? ORDER BY id", (order_id,)).fetchall()
        order["approvals"] = self.db.execute("SELECT * FROM work_order_approvals WHERE work_order_id=? ORDER BY id", (order_id,)).fetchall()
        from .work_order_workflow_repository import WorkOrderWorkflowRepository
        return WorkOrderWorkflowRepository(self.db, self.project_id).enrich(order, report_date, knowledge_cutoff)

    def list_orders(self, *, search="", date_from=None, date_to=None, contractor_id=None, building_id=None, status=""):
        with read_snapshot(self.db):
            return self._list_orders(search=search, date_from=date_from, date_to=date_to,
                                     contractor_id=contractor_id, building_id=building_id, status=status)

    def _list_orders(self, *, search, date_from, date_to, contractor_id, building_id, status):
        if status and status not in STATUS_LABELS:
            raise ValidationError("Неизвестный статус наряда")
        if contractor_id is not None:
            self.people.contractor(contractor_id)
        if building_id is not None:
            self.work._building(building_id)
        for value in (date_from, date_to):
            if value:
                day(value)
        if date_from and date_to and date_from > date_to:
            raise ValidationError("Начало периода должно быть не позже окончания")
        rows = self.db.execute("SELECT o.*,c.name AS contractor_name,(SELECT COUNT(*) FROM work_order_lines l WHERE l.work_order_id=o.id) AS line_count "
            "FROM work_orders o JOIN contractors c ON c.id=o.contractor_id WHERE o.project_id=? "
            "AND (?='' OR o.status=?) AND (? IS NULL OR o.contractor_id=?) AND (? IS NULL OR o.shift_date>=?) "
            "AND (? IS NULL OR o.shift_date<=?) AND (? IS NULL OR EXISTS(SELECT 1 FROM work_order_lines l JOIN work_items w ON w.id=l.work_item_id "
            "WHERE l.work_order_id=o.id AND w.building_id=?)) ORDER BY o.shift_date DESC,o.id DESC",
            (self.project_id, status, status, contractor_id, contractor_id, date_from, date_from, date_to, date_to, building_id, building_id)).fetchall()
        result = []
        for row in rows:
            if search.casefold() not in (row["order_number"] or "").casefold():
                continue
            item = dict(row)
            if row["header_snapshot"]:
                item["contractor_name"] = json.loads(row["header_snapshot"])["contractor_name"]
            locations = [json.loads(r[0])["building_name"] for r in self.db.execute("SELECT location_snapshot FROM work_order_lines WHERE work_order_id=? ORDER BY sort_order,id", (row["id"],))]
            item["buildings"] = ", ".join(dict.fromkeys(locations))
            result.append(item)
        return result
