# Расчётное ядро и Python API

Основной запуск приложения и пользовательский сценарий описаны в [README](../README.md).
Расчётные формулы и строгие исторические срезы первого этапа сохранены.

## Работа через Python API

Пример создаёт этаж и позицию в новой локальной базе. Все величины передаются
Decimal или строками. **Норматив `0.8` в примере — учебный ввод пользователя,
он не является строительным нормативом и не загружается в рабочий кодификатор.**

```python
from app.database import initialize
from app.models import Discipline
from app.repositories.work_repository import WorkRepository
from app.repositories.event_repository import EventRepository
from app.calculation_engine.historical import HistoricalCalculator
from app.calculation_engine.aggregation import group_by

db = initialize("data/example.sqlite3")
work = WorkRepository(db)
events = EventRepository(db)

building_id = db.execute("SELECT id FROM buildings WHERE code='A'").fetchone()[0]
catalog_id = db.execute("SELECT id FROM work_catalog WHERE code='ОТД.СТ.01'").fetchone()[0]
floor_id = work.add_floor(building_id, "1", 1)
item_id = work.add_work_item(building_id, floor_id, catalog_id, Discipline.CONSTRUCTION)
v1 = work.create_plan_version(Discipline.CONSTRUCTION, created_at="2026-01-01T08:00:00Z")
work.add_plan_line(v1, item_id, "1000", "0.8")
work.approve_plan_version(v1, approved_at="2026-01-01T09:00:00Z")

fact_id = events.add_fact(item_id, "600", "2026-01-10", event_key="journal-fact-0001",
                          recorded_at="2026-01-10T18:00:00Z", comment="Выполнено по журналу")
events.add_confirmation(item_id, "450", "2026-01-11", event_key="act-0001-line-1",
                        document_ref="Акт №1", recorded_at="2026-01-12T10:00:00Z")

calculator = HistoricalCalculator(db)
report = calculator.calculate_as_of("2026-01-31", "2026-01-31T23:59:59Z", v1)
assert str(report.total.actual_progress) == "0.6"
assert str(report.total.confirmed_progress) == "0.45"
by_floor = group_by(report.items, "floor_id")

v2 = work.create_plan_version(Discipline.CONSTRUCTION, base_version_id=v1,
                              created_at="2026-02-01T08:00:00Z")
work.update_plan_line(v2, item_id, "1200", "0.8")
work.approve_plan_version(v2, approved_at="2026-02-01T09:00:00Z")
plan_changes = work.compare_plan_versions(v1, v2)
execution_changes = calculator.compare_dates("2026-01-15", "2026-01-31",
                                             "2026-02-02T12:00:00Z", v1)
db.close()
```

Утверждённый план менять нельзя. `create_plan_version(base_version_id=...)`
копирует его в новый черновик, где разрешён `update_plan_line()`.
Без базовой версии создаётся пустой черновик. Номер версии назначается отдельно
по каждой дисциплине под блокировкой записи. Утверждение пустого плана, нулевого
объёма или строки без положительного норматива запрещено. Старые версии сохраняются.
Сравнение версий возвращает изменения объёмов, норм и трудоёмкостей, не прирост факта.

### События и корректировки

`add_fact()` и `add_confirmation()` не изменяют план и друг друга.
Дата факта — дата выполнения; дата подтверждения — отдельная календарная дата.
`recorded_at` — момент регистрации в UTC; без аргумента используется текущее время.
Явный `recorded_at` предусмотрен для доверенного импорта и воспроизводимых тестов.
В рабочем вводе его следует оставлять автоматически назначаемым.
Дата события не может быть позже даты регистрации. Временные метки требуют часовой пояс.

В каждом журнале `event_key` обязателен и уникален. Повторный импорт того же
источника должен передать тот же стабильный ключ и получит `ValidationError`.
Одинаковые объёмы в один день с **разными** ключами допустимы: это могут быть разные
записи журнала, а не дубли. Семантический поиск дублей по содержимому не выполняется.

События нельзя обновить, удалить или заменить через `REPLACE`.
Исправление — новая запись со ссылкой `correction_of_id` на событие того же журнала
и той же позиции. Отрицательная дельта без ссылки запрещена; дата исправления
и регистрации не могут предшествовать исходной записи. Корректировка может быть
внесена исходной датой для пересчёта истории с более поздней датой знаний.

```python
events.add_fact(item_id, "-20", "2026-01-10", event_key="journal-fix-0001",
                correction_of_id=fact_id, recorded_at="2026-02-01T12:00:00Z",
                comment="Исправление обмера")
```

Проверяются все затрагиваемые сочетания календарной даты и момента знаний.
Подтверждение не может превышать факт, накопительные объёмы не могут быть отрицательными.
Если уменьшение факта нарушает подтверждённый объём, сначала вносится корректировка
подтверждения с документом-основанием. Ошибка откатывает всю операцию вместе с аудитом.
Проверка повторно выполняется расчётным ядром, поэтому недостоверный импорт даст
`INVALID`, а не процент.

### Исторические срезы

Метод `HistoricalCalculator(db).calculate_as_of(report_date, knowledge_cutoff, plan_version_id)`
и одноимённая функция модуля с явным keyword-аргументом `db` возвращают `Snapshot`.
Обе границы включительные:

- Факт: `work_date <= report_date` и `recorded_at <= knowledge_cutoff`.
- Подтверждение: `confirmed_date <= report_date` и `recorded_at <= knowledge_cutoff`.
- План должен быть утверждён не позже конца отчётного дня UTC и контрольного момента знаний.

На этом этапе момент утверждения одновременно является началом действия версии:
отдельного `effective_from` и заднего утверждения плана нет. Утверждения идут по
номерам версий и времени. Явно указанная старая версия остаётся доступной после
утверждения новой. `get_active_plan_version(discipline, report_date=..., knowledge_cutoff=...)`
выбирает последнюю доступную утверждённую версию; без фильтров возвращает последнюю утверждённую.
Неизвестная, черновая или ещё недоступная версия даёт `INVALID / PLAN_UNAVAILABLE`.

Видимое событие по позиции той же дисциплины, отсутствующей в выбранном плане,
делает отчёт `INVALID / MISSING_PLAN_LINE`; такая работа не теряется в расчёте.
Поздняя регистрация не меняет ранний срез знаний, но учитывается при его пересчёте
с более поздним `knowledge_cutoff`.

`compare_dates()` использует одну версию и один контрольный момент знаний для обеих
дат. Он возвращает изменения физических объёмов **по позициям** и суммарной освоенной
трудоёмкости. Объёмы разных единиц измерения никогда не складываются в общий итог.
Пересмотр плана анализируется отдельно через `compare_plan_versions()`.

Консольный исторический отчёт:

```bash
./start.command --plan-version 1 --report-date 2026-01-31 --knowledge-cutoff 2026-02-05T18:00:00Z
```

### Расчёт и агрегация

Для каждой позиции:

```text
T_plan = Q_plan × N
Q_actual = сумма видимых дельт факта
Q_confirmed = сумма видимых дельт подтверждений
Q_earned = min(max(Q_actual, 0), Q_plan)
T_earned = Q_earned × N
P_actual = T_earned / T_plan
```

Подтверждённый прогресс считается аналогично из `Q_confirmed`.
Доли находятся в диапазоне 0..1; `0.6` означает 60%. Полный факт сохраняется,
даже если он выше плана. При 115 м² факта на плане 100 м²: готовность 100%,
перевыполнение 15 м², статус `WARNING`.

Для группы `P = SUM(T_earned) / SUM(T_plan)`, вес позиции `T_plan_i / SUM(T_plan)`.
Пример: 800 чел.-ч при выполнении 50% и 200 чел.-ч при выполнении 100% дают
`(400 + 200) / (800 + 200) = 60%`. Проценты этажей арифметически не усредняются.

`group_by()` поддерживает `building_id`, `floor_id`, `element`, `work_type`,
`discipline`. Ключ каждой группы включает дисциплину; ключ этажа также включает корпус.
`CONSTRUCTION` (СМР) и `DESIGN` (проектирование) имеют отдельные планы и показатели.
Попытка `aggregate()` объединить дисциплины возвращает `INVALID / MIXED_DISCIPLINES`.
Общего процента проекта нет.

| Статус | Смысл |
| --- | --- |
| `VALID` | Достаточно корректных данных; отсутствие событий означает нулевое выполнение |
| `WARNING` | Процент достоверен, есть перевыполнение; полный факт сохранён |
| `INVALID` | Нет достоверного расчёта; процент `None`, причины в `issues` |
| `NO_DATA` | Пустая группа; процент `None` |

Проверяются отсутствующий норматив/строка плана, нулевой или отрицательный план,
неположительный норматив, отрицательные накопления, превышение подтверждения над
фактом, несовместимость единиц, некорректные Decimal и повторное включение позиции.


## Дополнения этапа 2

`HistoricalCalculator.preview_draft_as_of()` выполняет явно помеченный предпросмотр
черновика тем же ядром. `calculate_as_of()` по-прежнему требует утверждённую версию.
Черновики не выбираются в качестве активных планов для сводки.

`Progress.remaining_labor` и `ItemProgress.remaining_quantity` возвращают остатки
на основе зачтённого выполнения с точным вычитанием Decimal. При INVALID возвращается None.

`WorkRepository` расширен операциями чтения, `delete_floor()`, `reorder_floor()`
и `update_catalog_entry()`. Все записи по-прежнему транзакционны.
`EventRepository.add_confirmation(..., comment="...")` сохраняет отдельный комментарий.
