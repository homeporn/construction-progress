from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.repositories.work_repository import WorkRepository
from app.web import get_db, current_project_id
from app.web.forms import discipline, integer, quantity
from app.web.views import work_context

bp = Blueprint("volumes", __name__)


@bp.get("/volumes")
def index():
    return render_template("volumes.html", **work_context())


@bp.post("/volumes/items")
def create_item():
    data = request.form
    direction = discipline(data)
    WorkRepository(get_db(), current_project_id()).add_work_item(
        integer(data, "building_id", "Корпус"), integer(data, "floor_id", "Этаж"),
        integer(data, "catalog_id", "Код работы"), direction)
    flash("Учётная позиция добавлена. Введите план и норматив в черновике версии.", "success")
    return redirect(url_for("volumes.index", discipline=direction.value), code=303)


@bp.post("/volumes/plans")
def create_plan():
    data, repo = request.form, WorkRepository(get_db(), current_project_id())
    direction = discipline(data)
    version_id = repo.create_plan_version(direction, base_version_id=integer(data, "base_version_id", "Базовая версия", optional=True))
    flash("Черновик создан. Заполните строки и утвердите версию.", "success")
    return redirect(url_for("volumes.index", discipline=direction.value, version_id=version_id), code=303)


@bp.post("/volumes/lines")
def save_line():
    data, repo = request.form, WorkRepository(get_db(), current_project_id())
    version_id, item_id = integer(data, "version_id", "Версия"), integer(data, "work_item_id", "Позиция")
    version = repo.get_plan_version(version_id)
    planned, norm = quantity(data, "planned_quantity", "Плановый объём"), quantity(data, "labor_norm", "Норматив", optional=True)
    exists = any(line["work_item_id"] == item_id for line in repo.list_plan_lines(version_id))
    operation = repo.update_plan_line if exists else repo.add_plan_line
    operation(version_id, item_id, planned, norm)
    flash("Строка черновика сохранена.", "success")
    return redirect(url_for("volumes.index", discipline=version["discipline"], version_id=version_id), code=303)


@bp.post("/volumes/plans/<int:version_id>/approve")
def approve(version_id):
    repo = WorkRepository(get_db(), current_project_id())
    version = repo.get_plan_version(version_id)
    repo.approve_plan_version(version_id)
    flash("Версия утверждена. Для дальнейших изменений создайте новый черновик.", "success")
    return redirect(url_for("volumes.index", discipline=version["discipline"], version_id=version_id), code=303)


@bp.post("/volumes/lines/remove")
def remove_line():
    repo = WorkRepository(get_db(), current_project_id())
    version_id = integer(request.form, "version_id", "Версия")
    version = repo.get_plan_version(version_id)
    repo.remove_plan_line(version_id, integer(request.form, "work_item_id", "Позиция"))
    flash("Позиция исключена из черновика. История выполнения сохранена.", "success")
    return redirect(url_for("volumes.index", discipline=version["discipline"], version_id=version_id), code=303)
