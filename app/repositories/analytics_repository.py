"""Versioned analytical views. Never writes plan lines or volume events."""
from uuid import uuid4

from app.models import Discipline
from app.time_utils import instant
from .base import ValidationError, transaction
from .project_repository import clean
from .work_repository import WorkRepository


class AnalyticsRepository:
    def __init__(self, db, project_id=1):
        self.db, self.project_id = db, project_id
        self.work = WorkRepository(db, project_id)

    def identity(self, group_id):
        row = self.db.execute('SELECT * FROM analytical_groups WHERE id=? AND project_id=?',
                              (group_id, self.project_id)).fetchone()
        if row is None:
            raise ValidationError('Аналитическая группа не принадлежит текущему объекту')
        return row

    def get(self, group_id, cutoff=None, *, version_no=None):
        self.identity(group_id)
        row = self.db.execute('SELECT v.*,c.code,c.name AS work_name FROM analytical_group_versions v '
            'JOIN work_catalog c ON c.id=v.catalog_id WHERE v.group_id=? AND v.sealed=1 AND v.changed_at<=? '
            'AND (? IS NULL OR v.version_no=?) ORDER BY v.version_no DESC LIMIT 1',
            (group_id, instant(cutoff), version_no, version_no)).fetchone()
        if row is None:
            raise ValidationError('Редакция группы недоступна на выбранный момент знания')
        result = dict(row)
        result['members'] = [dict(r) for r in self.db.execute('SELECT work_item_id,operation FROM analytical_group_members WHERE version_id=? ORDER BY work_item_id', (row['id'],))]
        return result

    def list(self, cutoff=None, *, include_archived=False):
        cutoff = instant(cutoff)
        result = []
        for row in self.db.execute('SELECT g.id FROM analytical_groups g WHERE g.project_id=? AND EXISTS('
                'SELECT 1 FROM analytical_group_versions v WHERE v.group_id=g.id AND v.sealed=1 AND v.changed_at<=?) ORDER BY g.id', (self.project_id, cutoff)):
            version = self.get(row['id'], cutoff)
            if include_archived or not version['archived']:
                result.append(version)
        return result

    def history(self, group_id, cutoff=None):
        self.identity(group_id)
        return self.db.execute('SELECT version_no,changed_at,name,archived FROM analytical_group_versions '
            'WHERE group_id=? AND sealed=1 AND changed_at<=? ORDER BY version_no DESC', (group_id, instant(cutoff))).fetchall()

    def save(self, name, catalog_id, *, unit=None, discipline='CONSTRUCTION', base_building_id=None,
             includes=(), excludes=(), archived=False, group_id=None, expected_version=None,
             changed_at=None, event_key=None):
        name, direction = clean(name, 'Название группы'), Discipline(discipline).value
        includes, excludes = list(includes), list(excludes)
        if len(includes)!=len(set(includes)) or len(excludes)!=len(set(excludes)) or set(includes)&set(excludes):
            raise ValidationError('Позиция группы не может повторяться во включениях или исключениях')
        event_key = clean(event_key or uuid4().hex, 'Ключ редакции', 100)
        with transaction(self.db):
            self.work._writable()
            now = instant(changed_at)
            if group_id is not None:
                self.identity(group_id)
            previous = self.db.execute('SELECT v.group_id,g.project_id FROM analytical_group_versions v JOIN analytical_groups g ON g.id=v.group_id WHERE v.event_key=?', (event_key,)).fetchone()
            if previous:
                if previous['project_id']!=self.project_id or (group_id is not None and previous['group_id']!=group_id):
                    raise ValidationError('Ключ редакции уже используется')
                return previous['group_id']
            catalog = self.db.execute('SELECT * FROM work_catalog WHERE id=?', (catalog_id,)).fetchone()
            if catalog is None or (unit is not None and unit != catalog['unit']):
                raise ValidationError('Код и единица группы несовместимы')
            if base_building_id is not None:
                self.work._building(base_building_id)
            if group_id is None:
                group_id = self.db.execute('INSERT INTO analytical_groups(project_id,created_at,create_key) VALUES (?,?,?)', (self.project_id, now, event_key)).lastrowid
                number = 1
            else:
                latest = self.db.execute('SELECT * FROM analytical_group_versions WHERE group_id=? ORDER BY version_no DESC LIMIT 1', (group_id,)).fetchone()
                if expected_version is None or latest['version_no']!=expected_version:
                    raise ValidationError('Группа изменена в другой вкладке. Обновите страницу')
                if now < latest['changed_at']:
                    raise ValidationError('Редакция не может предшествовать истории группы')
                number = latest['version_no']+1
            version_id = self.db.execute('INSERT INTO analytical_group_versions(group_id,version_no,name,catalog_id,unit,discipline,base_building_id,archived,changed_at,event_key) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (group_id,number,name,catalog_id,catalog['unit'],direction,base_building_id,int(archived),now,event_key)).lastrowid
            self.db.executemany('INSERT INTO analytical_group_members(version_id,work_item_id,operation) VALUES (?,?,?)',
                [(version_id, item, action) for action, items in [('include', includes), ('exclude', excludes)] for item in items])
            self.db.execute('UPDATE analytical_group_versions SET sealed=1 WHERE id=?', (version_id,))
            return group_id

    def revise(self, group_id, *, reset=False, archived=None, expected_version, event_key=None):
        previous = self.get(group_id)
        return self.save(previous['name'],previous['catalog_id'],unit=previous['unit'],discipline=previous['discipline'],
            base_building_id=previous['base_building_id'],includes=[] if reset else [m['work_item_id'] for m in previous['members'] if m['operation']=='include'],
            excludes=[] if reset else [m['work_item_id'] for m in previous['members'] if m['operation']=='exclude'],
            archived=previous['archived'] if archived is None else archived,group_id=group_id,
            expected_version=expected_version,event_key=event_key)
