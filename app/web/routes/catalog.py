from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.repositories.work_repository import WorkRepository
from app.web import get_db
from app.web.forms import integer, text, quantity

bp = Blueprint("catalog", __name__)


@bp.get("/catalog")
def index():
    repo = WorkRepository(get_db())
    all_rows = repo.list_catalog()
    search = text(request.args, "q", "Поиск", required=False)
    element = text(request.args, "element", "Элемент", required=False)
    work_type = text(request.args, "work_type", "Вид работ", required=False)
    return render_template("catalog.html", entries=repo.list_catalog(search=search, element=element, work_type=work_type),
                           elements=sorted({r["element"] for r in all_rows}),
                           work_types=sorted({r["work_type"] for r in all_rows}), q=search, element=element, work_type=work_type)


@bp.post("/catalog")
def save():
    data, repo = request.form, WorkRepository(get_db())
    values = [text(data, key, label, limit=limit) for key, label, limit in [
        ("code", "Код", 60), ("work_type", "Вид работ", 200), ("element", "Элемент", 200),
        ("name", "Наименование", 200), ("unit", "Единица измерения", 30)]]
    entry_id = integer(data, "catalog_id", "Код работы", optional=True)
    if entry_id:
        repo.update_catalog_entry(entry_id, *values)
    else:
        repo.add_catalog_entry(*values)
    flash("Кодификатор сохранён.", "success")
    return redirect(url_for("catalog.index"), code=303)


@bp.post('/catalog/norm')
def save_norm():
    WorkRepository(get_db()).set_catalog_norm(integer(request.form,'catalog_id','Код работы'),
        quantity(request.form,'reference_labor_norm','Справочный норматив',optional=True))
    flash('Справочный норматив сохранён. Утверждённые планы не изменены.','success')
    return redirect(url_for('catalog.index'),code=303)
