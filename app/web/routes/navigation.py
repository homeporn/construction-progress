"""Local presentation preferences and read-only application information."""

import platform
import sqlite3
from importlib.metadata import version
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, session

from app.web.forms import InputError
from app.web.navigation import GROUP_KEYS, navigation_context

bp = Blueprint("navigation", __name__)
bp.app_context_processor(navigation_context)


@bp.post("/navigation/state")
def state():
    opened = request.form.getlist("opened")
    collapsed = request.form.get("collapsed")
    if len(set(opened)) != len(opened) or len(opened) > len(GROUP_KEYS) or not set(opened) <= GROUP_KEYS or collapsed not in ("0", "1"):
        raise InputError("Некорректное состояние меню. Обновите страницу и повторите действие.")
    session["navigation"] = dict(opened=sorted(set(opened)), collapsed=collapsed == "1")
    return jsonify(saved=True)


@bp.get("/settings")
def settings():
    info = dict(python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                flask=version("Flask"), system=platform.system(), architecture=platform.machine(),
                database=str(Path(current_app.config["DATABASE"]).resolve()),
                address=current_app.config["LOCAL_ORIGIN"])
    return render_template("settings.html", environment=info)
