"""Deletion guards for building/floor references, including future modules.

Only the structural floors -> buildings link is removable. Every other inbound
reference blocks deletion, even when its FK declares ON DELETE CASCADE. SQL
identifiers come from SQLite metadata and are quoted; values remain parameters.
"""


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def building_dependencies(db, building_id):
    dependencies = []
    labels = {"work_items": "Учётные позиции", "work_orders": "Наряды-задания"}
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    primary_keys = {}
    for parent in ("buildings", "floors"):
        columns = db.execute("PRAGMA table_info({})".format(_quote(parent))).fetchall()
        primary_keys[parent] = [r["name"] for r in sorted(columns, key=lambda r: r["pk"]) if r["pk"]]
    for table_row in tables:
        table = table_row["name"]
        foreign_keys = db.execute("PRAGMA foreign_key_list({})".format(_quote(table))).fetchall()
        groups = {}
        for fk in foreign_keys:
            groups.setdefault(fk["id"], []).append(fk)
        predicates, params = [], []
        for parts in groups.values():
            parts.sort(key=lambda r: r["seq"])
            parent = parts[0]["table"].casefold()
            if parent not in primary_keys:
                continue
            pairs = [(fk["from"], fk["to"] or primary_keys[parent][fk["seq"]]) for fk in parts]
            if table.casefold() == "floors" and parent == "buildings" and pairs == [("building_id", "id")]:
                continue
            join = " AND ".join("child.{}=parent.{}".format(_quote(left), _quote(right)) for left, right in pairs)
            scope = "id" if parent == "buildings" else "building_id"
            predicates.append("EXISTS(SELECT 1 FROM {} parent WHERE parent.{}=? AND {})".format(
                _quote(parent), _quote(scope), join))
            params.append(building_id)
        # Fail closed for conventional links in extensions that omitted a FK.
        covered = {fk["from"].casefold() for fk in foreign_keys}
        for column in db.execute("PRAGMA table_info({})".format(_quote(table))).fetchall():
            name = column["name"].casefold()
            if name in covered or (table.casefold() == "floors" and name == "building_id"):
                continue
            if name == "building_id":
                predicates.append("child.{}=?".format(_quote(column["name"])))
                params.append(building_id)
            elif name == "floor_id":
                predicates.append("child.{} IN (SELECT id FROM floors WHERE building_id=?)".format(_quote(column["name"])))
                params.append(building_id)
        if predicates:
            count = db.execute("SELECT COUNT(*) FROM {} child WHERE {}".format(
                _quote(table), " OR ".join(predicates)), params).fetchone()[0]
            if count:
                dependencies.append(dict(table=table, label=labels.get(table, "Связанные записи: " + table), count=count))
    return dependencies
