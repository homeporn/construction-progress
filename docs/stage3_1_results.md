# Результаты этапа 3.1

Дата: 17 октября 2026. Исполнительная среда: Linux x86_64, CPython 3.13.5,
SQLite 3.46.1. Пользователь сообщил о проверке предыдущей версии на macOS;
новые изменения требуют повторной приёмки на целевых Mac. Зависимости, математика
Decimal и `start.command` сохранены; облачных сервисов не добавлено.

## Проверки

- До изменений: `174 passed in 3.61s`.
- После реализации: `228 passed in 7.22s` (54 новых сценария/параметризованных проверки).
- Сохранены исходные 174 сценария. Актуализированы старые ожидания количества
  миграций, разрешённое переименование корпуса и заполнение исторической схемы.
- Новые тесты покрывают атомарное создание объектов/корпусов, собственные V1,
  общие коды корпусов/работ между объектами, переименование, архивирование,
  раздельные факт/подтверждение/историю, выполнение только из утверждённого плана,
  исключённые работы, чужие URL/POST, устаревшую вкладку и консольную область объекта.
- Проверены перенос всех исходных колонок/ID, триггеры, FK, исторические 58%/45%,
  поздние события, идемпотентность, откат DDL при SQL-ошибке/изменении данных/FK,
  отказ при сбое резервного копирования и копирование незафиксированного в основном
  файле, но закоммиченного WAL через Backup API.
- Проверка запуска из каталога с пробелами и кириллицей прошла с принудительным
  запретом внешних сокетов: внешних обращений 0, 60%/45% сохраняются после
  перезапуска. Проверены SIGINT/SIGTERM/SIGHUP, освобождение порта, занятый порт,
  сохранение тестовой базы и неизменность рабочей базы.
- `git diff --check`, `node --check app/web/static/js/app.js`, `bash -n start.command`
  и проверка зафиксированных зависимостей без сети прошли.
- В Chromium выполнен сценарий школы: два корпуса, этаж, позиция, план 1000,
  норма как учебный пользовательский ввод, факт 600 и подтверждение 450.
  Получены 60% и 45%; переключение в исходный объект меняет показатели и возвращает
  их при обратном выборе. Проверены исторический экран, завершение сервера,
  отсутствие внешних запросов и ошибок JavaScript. Скриншоты: `docs/screenshots/`.

## Рабочая база

Перед работой выполнен Backup API в `data/backups/pre-stage3.1.sqlite3`.
Перед применением 003/004 автоматически создана вторая проверенная копия:
`data/backups/before-projects-20261017T145709-91c40322.sqlite3`.
Обе копии сохранены локально и не входят в Git.

В исходной копии рабочей базы были 3 корпуса, 15 кодов и 18 записей аудита;
этажей, позиций, планов и событий не было. После 004 все исходные ID и значения
сравнены с сохранённым снимком и совпали. Создан один объект «Строительный проект»
с ID 1, все прежние корпуса привязаны к нему. Начальные данные не задублированы.
`integrity_check=ok`, нарушений FK — 0. Показатели до/после — NO_DATA, поскольку
в исходной рабочей копии нет утверждённых планов. Сохранность заполненных планов,
событий, корректировок и результатов дополнительно доказана временными тестовыми базами.
Рабочая база не использовалась для тестов; её обновление выполнено отдельной операцией
после успешных тестов и браузерного сценария.
Повторные `./start.command --check` и `./start.command --cli` на обновлённой рабочей
базе прошли; SHA-256 файла не изменился, повторного заполнения данных не было.

## Изменённые и добавленные файлы

- `README.md`
- `app/calculation_engine/aggregation.py`
- `app/calculation_engine/historical.py`
- `app/calculation_engine/models.py`
- `app/catalog.py`
- `app/database.py`
- `app/repositories/event_repository.py`
- `app/repositories/project_repository.py`
- `app/repositories/work_repository.py`
- `app/services/progress_service.py`
- `app/web/__init__.py`
- `app/web/routes/buildings.py`
- `app/web/routes/dashboard.py`
- `app/web/routes/events.py`
- `app/web/routes/projects.py`
- `app/web/routes/volumes.py`
- `app/web/static/css/app.css`
- `app/web/static/js/app.js`
- `app/web/templates/_macros.html`
- `app/web/templates/base.html`
- `app/web/templates/events.html`
- `app/web/templates/project_detail.html`
- `app/web/templates/project_form.html`
- `app/web/templates/projects.html`
- `app/web/templates/volumes.html`
- `app/web/views.py`
- `docs/core.md`
- `docs/macos_acceptance_checklist.md`
- `docs/macos_first_run.md`
- `docs/schema.md`
- `docs/screenshots/project-create.png`
- `docs/screenshots/project-school.png`
- `docs/screenshots/projects.png`
- `docs/screenshots/workspace-progress.png`
- `docs/stage3_1_results.md`
- `docs/web.md`
- `docs/workspaces.md`
- `main.py`
- `migrations/004_projects.sql`
- `tests/test_database.py`
- `tests/test_launch.py`
- `tests/test_project_migration.py`
- `tests/test_project_web.py`
- `tests/test_projects.py`
- `tests/test_web_services.py`

## Обновление и ограничения

Пошаговые команды, резервное копирование и восстановление — в [workspaces.md](workspaces.md).
Выбор объекта сохраняется в браузерной сессии текущего процесса; после перезапуска
может потребоваться выбрать его снова. Архив не удаляет данные. Сохраняется строгая
ошибка MISSING_PLAN_LINE, если события не соответствуют составу выбранной версии.
Многопользовательские роли, `.app`, Excel, Windows и наряды этапа 3.2 не реализованы.
