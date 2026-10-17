import secrets
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from urllib.parse import urlsplit

from flask import Flask, abort, current_app, g, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from app.config import DEFAULT_DB_PATH
from app.database import connect, initialize
from app.decimal_utils import multiply
from app.repositories.base import ValidationError
from app.time_utils import instant
from .forms import InputError


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATABASE=str(DEFAULT_DB_PATH), SECRET_KEY=secrets.token_bytes(32),
                      LOCAL_ORIGIN="http://127.0.0.1:5000", DEBUG=False,
                      SESSION_COOKIE_NAME="construction_progress", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=128 * 1024)
    if config:
        app.config.update(config)
    if str(app.config["DATABASE"]) == ":memory:":
        raise ValueError("The web application requires a file-backed database")
    db = initialize(app.config["DATABASE"])
    db.close()

    @app.teardown_appcontext
    def close_db(error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.before_request
    def local_request_guard():
        expected = app.config["LOCAL_ORIGIN"]
        if request.host != urlsplit(expected).netloc or request.remote_addr != "127.0.0.1":
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("Origin") != expected or request.headers.get("Sec-Fetch-Site") == "cross-site":
                abort(403)
            supplied = request.form.get("csrf_token", "")
            stored = session.get("csrf_token", "")
            if not supplied or not stored or not secrets.compare_digest(supplied.encode("utf-8"), stored.encode("utf-8")):
                abort(403)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # no-referrer turns native same-origin form POST Origin into "null" in
        # Chromium. same-origin retains it locally and reveals nothing off-site.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.template_filter("number")
    def number(value):
        if value is None:
            return "—"
        result = format(value, "f") if not isinstance(value, str) else value
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return result.replace(".", ",")

    @app.template_filter("percent")
    def percent(value):
        if value is None:
            return "Нет данных"
        with localcontext() as ctx:
            ctx.prec = 60
            ctx.rounding = ROUND_HALF_EVEN
            result = multiply(value, Decimal(100))
            return format(result, ".1f").replace(".", ",") + "%"

    @app.context_processor
    def shared_context():
        return dict(csrf_token=csrf_token, today=instant()[:10],
                    discipline_labels={"CONSTRUCTION": "СМР", "DESIGN": "Проектирование"},
                    status_labels={"VALID": "Корректно", "WARNING": "Предупреждение", "INVALID": "Ошибка",
                                   "NO_DATA": "Нет данных", "DRAFT": "Черновик", "APPROVED": "Утверждён"})

    @app.errorhandler(ValidationError)
    @app.errorhandler(ValueError)
    def validation_error(error):
        message = str(error) if isinstance(error, InputError) else translate_error(str(error))
        endpoint = {"buildings": "buildings.index", "catalog": "catalog.index", "volumes": "volumes.index",
                    "events": "events.confirmations" if request.path == "/confirmations" else "events.facts",
                    "progress": "progress.index"}.get(request.blueprint, "dashboard.index")
        return render_template("error.html", message=message, back_url=url_for(endpoint)), 400

    @app.errorhandler(Exception)
    def safe_error(error):
        if isinstance(error, HTTPException):
            messages = {403: "Запрос отклонён. Откройте приложение по его локальному адресу и обновите страницу.",
                        404: "Страница не найдена.", 405: "Этот способ запроса не поддерживается.",
                        413: "Запрос слишком большой."}
            code = error.code
            message = messages.get(code, "Не удалось обработать запрос.")
        else:
            app.logger.exception("Local application request failed")
            code, message = 500, "Не удалось выполнить операцию. Данные не сохранены. Проверьте доступность базы и повторите запрос."
        return render_template("error.html", message=message, back_url="/"), code

    from .routes import dashboard, buildings, catalog, volumes, events, progress
    for module in (dashboard, buildings, catalog, volumes, events, progress):
        app.register_blueprint(module.bp)

    @app.post("/shutdown")
    def shutdown():
        stop = app.extensions.get("stop_server")
        if stop:
            stop()
        return render_template("stopped.html")

    return app


def translate_error(message):
    translations = [
        ("confirmation exceeds actual", "Подтверждение превышает факт либо корректировка создаёт отрицательный накопительный объём. Запись не сохранена."),
        ("Approved plan", "Утверждённый план изменять нельзя. Создайте новый черновик на его основе."),
        ("positive quantities", "Для утверждения нужны положительные плановые объёмы и нормативы во всех строках."),
        ("empty plan", "Нельзя утвердить пустую версию. Сначала добавьте строки плана."),
        ("version order", "Утверждайте версии по порядку. Более новая версия уже утверждена."),
        ("Duplicate", "Такая запись уже существует. Повторная регистрация не выполнена."),
        ("UNIQUE", "Запись с таким кодом или сочетанием полей уже существует."),
        ("identity already exists", "Такая учётная запись уже существует."),
        ("FOREIGN KEY", "Связанная запись не найдена или уже используется в учёте."),
        ("Incompatible", "Единицы измерения несовместимы."),
        ("correction", "Проверьте исходное событие корректировки, его позицию и даты."),
        ("Plan version does not exist", "Версия плана не найдена."),
        ("Work item does not exist", "Учётная позиция не найдена."),
        ("discipline", "Направление позиции и версии плана должно совпадать."),
    ]
    for fragment, translated in translations:
        if fragment in message:
            return translated
    if any("А" <= char <= "я" for char in message):
        return message
    return "Запись не сохранена. Проверьте значения, единицы, даты и ссылки на исходные записи."
