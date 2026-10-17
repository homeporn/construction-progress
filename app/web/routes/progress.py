from flask import Blueprint, render_template
from app.web.views import work_context

bp = Blueprint("progress", __name__)


@bp.get("/progress")
def index():
    return render_template("progress.html", **work_context(historical=True))
