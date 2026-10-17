"""Idempotent seed: no floors, quantities or invented labor norms."""
CATALOG = {
    "СТ": ("Стены", ["штукатурка", "шпаклёвка", "окраска", "облицовка плиткой",
                       "устройство перегородок", "грунтование"]),
    "ПЛ": ("Пол", ["стяжка", "гидроизоляция", "укладка плитки",
                     "укладка напольного покрытия", "наливной пол"]),
    "ПТ": ("Потолок", ["шпаклёвка", "окраска", "подвесной потолок", "грунтование"]),
}


def seed_catalog(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        for code, name in [("A", "Корпус А"), ("B", "Корпус B"), ("C", "Корпус C")]:
            if not db.execute("SELECT 1 FROM buildings WHERE code=?", (code,)).fetchone():
                db.execute("INSERT INTO buildings(code,name) VALUES (?,?)", (code, name))
        for prefix, (element, names) in CATALOG.items():
            for number, name in enumerate(names, 1):
                code = "ОТД.{}.{:02d}".format(prefix, number)
                if db.execute("SELECT 1 FROM work_catalog WHERE code=?", (code,)).fetchone():
                    continue
                db.execute("INSERT INTO work_catalog(code,work_type,element,name,unit) "
                           "VALUES (?,?,?,?,?)", (code, name, element, name, "м²"))
        db.commit()
    except Exception:
        db.rollback()
        raise
