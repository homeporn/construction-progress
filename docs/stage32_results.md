# Construction Progress — результаты этапа 3.2

## Реализованное поведение

Добавлены справочники подрядчиков, персонала и специальностей, сменные наряды
с несколькими работами и составом бригады, реестр, фильтры, редактирование
черновика, выдача, отмена с основанием и печать. Нумерация независима по объектам,
выданные номера и снимки неизменяемы. Используется существующий Flask-интерфейс
и единственная локальная SQLite-база, без новых зависимостей.

Проверено управление корпусами: удаление свободного корпуса, отдельное согласие
на пустые этажи, защита всех зависимостей, архивирование и восстановление.
Архивный корпус недоступен в новых нарядах, оставаясь в документах и отчётах.

Формулы нормативно-весового прогресса и файлы `app/calculation_engine` не менялись.
`EventRepository` и прежние журналы факта/подтверждения не изменены. Проектирование
и СМР остаются раздельными. Сдача, приёмка и регистрация объёмов через наряд
относятся к этапу 3.3 и не реализованы.

## SQLite и обновление

Новая аддитивная [миграция 006](../migrations/006_work_orders.sql) добавляет десять
таблиц документов/персонала, FK, индексы, снимки, счётчик, ограничения, триггеры
аудита и защиты истории. Миграции 001–005 не переписаны.
Новых колонок в расчётных таблицах нет. Полный [обзор новой схемы](work_orders.md#хранение-связи-и-аудит).

Обновление существующей базы предусматривает проверенную копию SQLite Backup API,
транзакцию, сравнение исходных данных до/после, `integrity_check` и проверку FK.
Тестовая миграция с событиями 600/450 сохранила все прежние ID, строки, даты,
аудит и результаты 60%/45%. Повторное применение не создаёт дубли.
Искусственная ошибка в конце 006 откатывает новые таблицы/триггеры и сохраняет
проверенную резервную копию.

Рабочая база в репозитории во время разработки **не мигрировалась и не изменялась**:

```text
SHA-256: 10f7b9964447ec33eb9d4865ef38b52384f606174217c1304d420510a55dc036
Размер: 172032 байта
Время изменения и количества записей: совпали до/после
integrity_check: ok
foreign_key_check: без нарушений
```

Она содержит четыре применённые миграции, один объект, три корпуса и 15 кодов.
Обычный запуск применит ожидающие 005 и 006 с резервной копией; `--check` только
сообщает о них. Сценарии разработки выполнялись исключительно на временных БД.

## Автоматические проверки

Среда: Linux x86_64, CPython 3.13.5, SQLite 3.46.1. Установленные зависимости
проверены локальным `scripts.check_environment --web --tests`, без установки пакетов.

```bash
.venv/bin/python -m pytest -q
```

До полного этапа: **294 passed** — все 260 исходных сценариев плюс 34 проверки
управления корпусами. После этапа: **373 passed**. Добавлено ещё 79 сценариев
нарядов, веб-интерфейса и миграции. Все исходные тесты сохранены.

Покрыты организация/персонал, смешанное заполнение бригады, предупреждения,
разные корпуса, черновики/выдача/отмена, независимые и конкурентные номера,
повторные отправки, объёмы Decimal TEXT, смена плана до выдачи, неизменяемые
снимки, архивные сущности, изоляция через URL/POST, CSRF/Origin/рабочее пространство,
печать, перезапуск и безопасная миграция. Проверены также сохранение существующего
ручного факта/подтверждения при выдаче и отсутствие повторного создания удалённого
пустого корпуса после инициализации приложения.

`git diff --check` не выявил ошибок форматирования. В миграциях 001–005,
расчётном ядре и репозитории событий изменений нет.

## Chromium и печать

Временный объект «Школа»: учебный/спортивный корпуса, этажи 3/1, подрядчик
«ООО Строй», прораб, бригадир, четыре штукатура и три плиточника. Утверждён план
1000/500 м² с учебными введёнными нормативами. В наряде заданы 120/60 м²,
две специальности и семь работников. После выдачи:

- Присвоен `НЗ-0001`, документ найден в реестре и через фильтры.
- Строки связаны с исходными позициями; черновик до выдачи повторно отредактирован.
- Печатный документ содержит реквизиты, работы, состав бригады и места подписей.
- Факт и подтверждение наряда пустые; оба журнала содержат ноль новых событий.
- Показатели сводки совпали до/после выдачи.
- Печатный образец получен в PDF на **одной странице A4, альбомная ориентация**;
  проверены CSS `@media print`, скрытие панели и вызов печати кнопкой.
- Проверены удаление пустого корпуса, блокировка используемого, архивирование,
  отсутствие его в новой форме, сохранение в печати и восстановление.
- Переключение объектов изолирует реестр. Проверена ширина экрана 1366×768.
- Ошибки JavaScript: **0**. Ошибки HTTP: **0**. Внешние запросы браузера: **0**.
  Сервер работал с блокировкой внешних соединений, попыток соединения не было.
- Сервер завершился через интерфейс с кодом 0; повторное открытие SQLite показало
  `integrity_check=ok`, отсутствие нарушений FK, сохранённый номер и пустые журналы.

Скриншоты: [реестр](screenshots/order-registry.png), [форма](screenshots/order-form.png),
[печатная версия](screenshots/order-print.png),
[удаление пустых этажей](screenshots/building-delete-confirm.png),
[запрет удаления](screenshots/building-delete-blocked.png),
[архивирование](screenshots/building-archived.png).

## Обновление и ограничения

Остановите сервер, сохраните собственную базу, обновите исходники без замены `data`
и `.venv`, затем выполните:

```bash
cd "/путь/к/construction-progress"
./start.command --check
./start.command
```

Подробная [инструкция обновления, резервного копирования и восстановления](work_orders.md#обновление-на-macos).
Новое окружение или установка пакетов из сети не требуются.

Предыдущая версия испытывалась пользователем в Monterey/Safari. Новые изменения
проверены в Linux/Chromium; повторная приёмка на реальных Catalina/современных Mac
и проверка печати в Safari остаются обязательными. Приложение сохраняет прежнюю
кодовую базу/зависимости, минимальный SQLite 3.24 и запуск `start.command`.

Другие ограничения: локальная однопользовательская работа, списки без пагинации,
отсутствие резервирования сменных объёмов между нарядами, отсутствие юридической
электронной подписи. Большой печатный документ может занимать несколько страниц.
Упаковка `.app`, Windows-версия, Excel и этап 3.3 не выполнялись.

## Изменённые и добавленные файлы

Перечень ниже включает ранее начатое дополнение управления корпусами.

Изменены:

- [README.md](../README.md)
- [app/database.py](../app/database.py)
- [app/repositories/work_repository.py](../app/repositories/work_repository.py)
- [app/web/__init__.py](../app/web/__init__.py)
- [app/web/routes/buildings.py](../app/web/routes/buildings.py)
- [app/web/routes/projects.py](../app/web/routes/projects.py)
- [app/web/static/css/app.css](../app/web/static/css/app.css)
- [app/web/templates/_event_form.html](../app/web/templates/_event_form.html)
- [app/web/templates/base.html](../app/web/templates/base.html)
- [app/web/templates/buildings.html](../app/web/templates/buildings.html)
- [app/web/templates/project_detail.html](../app/web/templates/project_detail.html)
- [app/web/templates/volumes.html](../app/web/templates/volumes.html)
- [app/web/views.py](../app/web/views.py)
- [docs/schema.md](../docs/schema.md)

Добавлены:

- [app/repositories/building_dependencies.py](../app/repositories/building_dependencies.py)
- [app/repositories/personnel_repository.py](../app/repositories/personnel_repository.py)
- [app/repositories/work_order_repository.py](../app/repositories/work_order_repository.py)
- [app/web/routes/personnel.py](../app/web/routes/personnel.py)
- [app/web/routes/work_orders.py](../app/web/routes/work_orders.py)
- [app/web/static/css/work_order_print.css](../app/web/static/css/work_order_print.css)
- [app/web/static/js/print_order.js](../app/web/static/js/print_order.js)
- [app/web/static/js/work_orders.js](../app/web/static/js/work_orders.js)
- [app/web/templates/_work_order_document.html](../app/web/templates/_work_order_document.html)
- [app/web/templates/building_delete.html](../app/web/templates/building_delete.html)
- [app/web/templates/personnel.html](../app/web/templates/personnel.html)
- [app/web/templates/personnel_form.html](../app/web/templates/personnel_form.html)
- [app/web/templates/work_order_detail.html](../app/web/templates/work_order_detail.html)
- [app/web/templates/work_order_form.html](../app/web/templates/work_order_form.html)
- [app/web/templates/work_order_print.html](../app/web/templates/work_order_print.html)
- [app/web/templates/work_orders.html](../app/web/templates/work_orders.html)
- [app/work_order_models.py](../app/work_order_models.py)
- [docs/building_management.md](../docs/building_management.md)
- [docs/screenshots/building-archived.png](../docs/screenshots/building-archived.png)
- [docs/screenshots/building-delete-blocked.png](../docs/screenshots/building-delete-blocked.png)
- [docs/screenshots/building-delete-confirm.png](../docs/screenshots/building-delete-confirm.png)
- [docs/screenshots/order-form.png](../docs/screenshots/order-form.png)
- [docs/screenshots/order-print.png](../docs/screenshots/order-print.png)
- [docs/screenshots/order-registry.png](../docs/screenshots/order-registry.png)
- [docs/stage32_results.md](../docs/stage32_results.md)
- [docs/work_orders.md](../docs/work_orders.md)
- [migrations/006_work_orders.sql](../migrations/006_work_orders.sql)
- [tests/test_building_management.py](../tests/test_building_management.py)
- [tests/test_work_order_migration.py](../tests/test_work_order_migration.py)
- [tests/test_work_order_web.py](../tests/test_work_order_web.py)
- [tests/test_work_orders.py](../tests/test_work_orders.py)
