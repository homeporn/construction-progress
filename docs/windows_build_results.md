# W1.0 — результаты подготовки Windows-сборки

**Статус: Build Kit; настоящая Windows Portable-сборка ещё не выполнена.**
Доступна Linux x64. Windows 10/11 runner, PowerShell и Wine не обнаружены.
GitHub Actions не создавались/не запускались. Push и публикация не выполнялись.

## Исходное состояние

- Git HEAD: `4a407131c5ff1a7a542d25decb533755a0263704`.
- Используется текущая рабочая копия, включая незакоммиченные 4.2/4.2.1.
  Ничего не сбрасывалось и не восстанавливалось из старых снимков.
- SHA-256 184 исходных файлов и состояние Git: [baseline.json](windows/baseline.json).
- Последняя миграция — `010_timesheets.sql`. Новых миграций нет.
- Исходный полный прогон: **829 passed, 0 failed, 95,58 с**.
- База репозитория имеет применённые 001–004; она не является базой пользователя с Mac.
  `integrity_check=ok`, `foreign_key_check=[]`.
- Исходный SHA-256 базы:
  `10f7b9964447ec33eb9d4865ef38b52384f606174217c1304d420510a55dc036`.

## Реализованные файлы W1.0

- `app/portable/{paths,storage,instance,logging,resources,server,launcher}.py`, `__init__.py`.
- `windows_launcher.py`, `requirements/windows.txt`.
- `packaging/windows/ConstructionProgress.spec`, `build.ps1`,
  `requirements-build.txt`, `version_info.txt`, `VERSION.txt`, инструкции `.txt/.md`, `BUILD_WINDOWS.cmd`.
- `tools/windows/{bundle,smoke,pytest_plugin}.py`, `__init__.py`.
- `tests/test_windows_{storage,instance,server,bundle,launcher}.py`.
- `docs/windows_portable.md`, этот отчёт, `docs/windows/baseline.json`, план и результаты проверки.

Существующие расчёты, бизнес-репозитории, старые миграции и macOS launcher не изменены.
Сохранены прежние отчёты и ограничения частичного этапа 4.2.1.

## Зависимости и поставка

Waitress **3.0.2** — новая зависимость только Windows runtime.
PyInstaller **6.16.0**, hooks-contrib **2025.9** и их зависимости закреплены для
сборки. Общие Flask/Jinja2 зависимости используются из `requirements/web.txt`.
Рекомендуемый runtime для воспроизводимой Windows-сборки: CPython **3.13.12 x64**.
Фактическая Windows-версия Python/PyInstaller/Waitress будет записана в
`BUILD_INFO.json` только после реального запуска build.ps1.

Excel продолжает использовать существующий OOXML-код стандартной библиотеки.
Новой библиотеки XLSX нет; macOS requirements не изменялись.

`ConstructionProgress-Windows-BuildKit-W1.0.zip` содержит актуальные исходники,
ресурсы, тесты и инструкции. В нём **нет EXE, Python runtime и рабочей базы**.
Контрольные суммы каждого исходного файла включены в `SOURCE_SHA256.json`.
Windows-скрипт собирает настоящий onedir и проверяет PE x64 до создания файла
`ConstructionProgress-Windows-x64.zip`. Linux-файл не переименовывается в EXE.

## Проверки

| Проверка | Результат |
| --- | --- |
| Полный прогон до W1.0 | 829 passed, 0 failed, 95,58 с |
| Повторный полный прогон | **857 passed, 0 failed, 7 skipped, 98,97 с** |
| Новые проверки W1.0 отдельно | 28 passed, 7 skipped, 1,28 с |
| Компиляция новых Python-модулей | Успешно |
| Исходные 184 файла | SHA-256 совпадают с исходными |
| Миграции 001–010 | Не изменены |
| Рабочая SQLite | SHA-256 и mtime совпадают, integrity=ok, FK ошибок нет |

Проверены временные профили: Unicode/пробелы, независимость от cwd, первый запуск,
отсутствие демо-событий, повторная инициализация, сохранность ID при 009→010,
Backup API, искусственная ошибка миграции, повреждённая/новая схема, read-only
диагностика, межпроцессная блокировка и stale-файл, безопасное логирование,
наличие/хеширование ресурсов, отказ упаковывать ELF как Windows PE.

Итоговые числа и протокол полного повторного прогона сохраняются в `docs/windows/`.
Сетевой sandbox Linux запрещает даже bind к 127.0.0.1; семь интеграционных
Waitress-тестов в нём явно пропускаются. Это ограничение **не считается успешным
Windows smoke test**. Скрипт Windows-сборки должен выполнить эти проверки в нативной
среде; также запускает реальный EXE в отдельном временном Unicode-профиле.

Синтетическая контрольная модель сохраняет **60% / 45%**, часы табеля 7,5,
факт зарегистрирован без наряда. Тестовые профили не относятся к рабочей базе.

## Невыполненные проверки

- Настоящего Windows EXE и готового portable ZIP нет; их размеры не измерены.
- PyInstaller/build.ps1 не выполнялись на Windows, скрипт PowerShell не проверен нативным интерпретатором.
- Не проверены Windows 10/11 без Python, Edge/Chrome, открытие браузера и Windows файл-диалоги.
- Не проверены нативные права/Unicode/MAX_PATH, Windows вариант msvcrt и завершение процесса.
- Не проводились новые нагрузочные испытания и замеры RAM/времени на Windows.
- EXE не подписан; реакция SmartScreen/антивируса неизвестна.
- Для полной готовности нужны Windows-сборка, нативный smoke test и ручной сценарий из инструкции.

Запуск сборки и обновление описаны в [windows_portable.md](windows_portable.md).
