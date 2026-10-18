from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from app.repositories.project_repository import ProjectRepository
from app.repositories.base import ValidationError
from app.repositories.work_repository import WorkRepository
from app.web import current_project_id, get_db
from app.web.forms import InputError, integer, text

bp = Blueprint("projects", __name__)


@bp.get("/projects")
def index():
    return render_template("projects.html", projects=ProjectRepository(get_db()).list_projects())


@bp.route("/projects/new", methods=["GET", "POST"])
def create():
    if request.method == "GET":
        return render_template("project_form.html")
    data = request.form
    count = integer(data, "building_count", "Количество корпусов", signed=True)
    codes, names = data.getlist("building_code"), data.getlist("building_name")
    if count < 0 or count != len(codes) or count != len(names):
        raise InputError("Количество корпусов должно совпадать с количеством заполненных строк.")
    project_id = ProjectRepository(get_db()).create_project(
        text(data, "name", "Наименование"), text(data, "code", "Код объекта", limit=80),
        text(data, "description", "Описание", required=False, limit=4000),
        buildings=[dict(code=code, name=name) for code, name in zip(codes, names)])
    session["project_id"] = project_id
    flash("Объект и корпуса созданы. Этажи добавляются отдельно.", "success")
    return redirect(url_for("projects.detail", project_id=project_id), code=303)


@bp.post("/projects/select")
def select():
    project_id = integer(request.form, "project_id", "Объект")
    ProjectRepository(get_db()).get_project(project_id)
    session["project_id"] = project_id
    # Old filters and version IDs must not travel into the other workspace.
    return redirect(url_for("dashboard.index"), code=303)


@bp.route("/projects/<int:project_id>", methods=["GET", "POST"])
def detail(project_id):
    if project_id != current_project_id():
        abort(403)
    repo = ProjectRepository(get_db())
    if request.method == "POST":
        data = request.form
        action = text(data, "action", "Операция")
        if action == "edit":
            repo.update_project(project_id, text(data, "name", "Название"), text(data, "code", "Код", limit=80),
                                text(data, "description", "Описание", required=False, limit=4000))
        elif action in ("archive", "restore"):
            repo.set_archived(project_id, action == "archive")
        else:
            raise InputError("Неизвестная операция с объектом.")
        flash("Объект сохранён.", "success")
        return redirect(url_for("projects.detail", project_id=project_id), code=303)
    return render_template("project_detail.html", project=repo.get_project(project_id),
                           buildings=WorkRepository(get_db(), project_id).list_buildings())


@bp.post("/projects/<int:project_id>/buildings")
def buildings(project_id):
    if project_id != current_project_id():
        abort(403)
    repo, data = WorkRepository(get_db(), project_id), request.form
    action = text(data, "action", "Операция")
    if action in ("add", "edit"):
        values = (text(data, "code", "Код корпуса", limit=80), text(data, "name", "Название корпуса"),
                  integer(data, "sort_order", "Порядок", signed=True))
        if action == "add":
            repo.add_building(*values)
        else:
            repo.update_building(integer(data, "building_id", "Корпус"), *values)
    elif action in ("archive", "restore"):
        repo.archive_building(integer(data, "building_id", "Корпус"), action == "archive")
    else:
        raise InputError("Неизвестная операция с корпусом.")
    flash("Корпус сохранён.", "success")
    return redirect(url_for("projects.detail", project_id=project_id), code=303)


@bp.route("/projects/<int:project_id>/buildings/<int:building_id>/delete", methods=["GET", "POST"])
def delete_building(project_id, building_id):
    if project_id != current_project_id():
        abort(403)
    repo = WorkRepository(get_db(), project_id)
    info = repo.building_deletion_info(building_id)
    error = None
    if request.method == "POST":
        try:
            if request.form.get("confirm_delete") != "1":
                raise InputError("Подтвердите удаление корпуса.")
            repo.delete_building(building_id, delete_empty_floors=request.form.get("delete_empty_floors") == "1")
        except ValidationError as exc:
            error = str(exc)
            info = repo.building_deletion_info(building_id)
        else:
            flash("Корпус и его пустые этажи удалены. Журнал аудита сохранён.", "success")
            return redirect(url_for("projects.detail", project_id=project_id), code=303)
    return render_template("building_delete.html", project=ProjectRepository(get_db()).get_project(project_id),
                           error=error, **info), 400 if error else 200
