from flask import Blueprint, render_template, request
from app.services.progress_service import ProgressService
from app.web import get_db, current_project_id
from app.web.forms import report_dates

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    report_date, cutoff = report_dates(request.args)
    return render_template("dashboard.html", report_date=report_date, cutoff=cutoff,
                           **ProgressService(get_db(), current_project_id()).dashboard(report_date, cutoff))
