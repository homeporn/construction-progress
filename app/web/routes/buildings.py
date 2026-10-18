from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from app.repositories.work_repository import WorkRepository
from app.web import get_db, current_project_id
from app.web.forms import InputError, integer, text

bp = Blueprint("buildings", __name__)


@bp.get("/buildings")
def index():
    repo = WorkRepository(get_db(), current_project_id())
    return render_template("buildings.html", buildings=repo.list_buildings(), floors=repo.list_floors(),
                           entry_buildings=repo.list_buildings(include_archived=False))


@bp.get("/api/buildings")
def listing():
    return jsonify([dict(row) for row in WorkRepository(get_db(), current_project_id()).list_buildings()])


@bp.post("/buildings/floors")
def change_floor():
    repo, data = WorkRepository(get_db(), current_project_id()), request.form
    action = text(data, "action", "Операция")
    if action == "add":
        repo.add_floor(integer(data, "building_id", "Корпус"), text(data, "floor_code", "Обозначение этажа", limit=40),
                       integer(data, "sort_order", "Порядок", signed=True))
    elif action == "reorder":
        repo.reorder_floor(integer(data, "floor_id", "Этаж"), integer(data, "sort_order", "Порядок", signed=True))
    elif action == "delete":
        repo.delete_floor(integer(data, "floor_id", "Этаж"))
    else:
        raise InputError("Неизвестная операция с этажом.")
    flash("Изменения этажей сохранены.", "success")
    return redirect(url_for("buildings.index"), code=303)
