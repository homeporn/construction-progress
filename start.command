#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd -P "$(dirname "$0")" && pwd -P)"
cd "$PROJECT_DIR"

PREPARE=0
CHECK=0
HELP=0
ARG_COUNT=$#
# Rotate positional parameters once. This avoids empty-array/set -u behavior in
# Apple's Bash 3.2 while preserving spaces, Cyrillic and argument boundaries.
while [ "$ARG_COUNT" -gt 0 ]; do
    ARG=$1
    shift
    case "$ARG" in
        --prepare) PREPARE=1 ;;
        --check) CHECK=1 ;;
        -h|--help) HELP=1; set -- "$@" "$ARG" ;;
        *) set -- "$@" "$ARG" ;;
    esac
    ARG_COUNT=$((ARG_COUNT - 1))
done

fail() { printf '%s\n' "$*" >&2; exit 1; }
if [ "$CHECK" -eq 1 ] && [ "$PREPARE" -eq 1 ]; then
    fail "--check и --prepare нельзя совмещать: диагностика не устанавливает зависимости."
fi

PYTHON_BIN="${CP_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    elif command -v python3.13 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.13)"
    elif [ -x /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 ]; then
        PYTHON_BIN=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
    elif command -v python3 >/dev/null 2>&1; then
        # Useful for diagnostics only; the version check below rejects unsupported Python.
        PYTHON_BIN="$(command -v python3)"
    else
        fail "Python не найден. Установите CPython 3.13.12 с python.org или задайте полный путь CP_PYTHON. Инструкция: docs/macos_first_run.md"
    fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    fail "Указанный Python недоступен: $PYTHON_BIN. Проверьте CP_PYTHON и установку интерпретатора."
fi
if [ "$HELP" -eq 1 ]; then
    exec "$PYTHON_BIN" -B main.py --help
fi
if [ "$CHECK" -eq 1 ]; then
    if "$PYTHON_BIN" -B -m scripts.check_environment --diagnose "$@"; then
        exit 0
    else
        CHECK_STATUS=$?
        printf '%s\n' "Диагностика завершилась с ошибками. Восстановление: docs/macos_first_run.md" >&2
        exit "$CHECK_STATUS"
    fi
fi

if ! "$PYTHON_BIN" -m scripts.check_environment; then
    fail "Проверка Python не пройдена. Необходимо совместимое окружение; инструкция: docs/macos_first_run.md"
fi
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    if [ -e "$PROJECT_DIR/.venv" ] || [ -L "$PROJECT_DIR/.venv" ]; then
        fail "Окружение .venv повреждено. Сохраните его под другим именем и создайте заново по docs/macos_first_run.md; база данных не затрагивается."
    fi
    if [ "$PREPARE" -ne 1 ]; then
        fail "Окружение .venv отсутствует. Для первоначальной установки выполните ./start.command --prepare; диагностика: ./start.command --check"
    fi
    if ! "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"; then
        fail "Не удалось создать .venv. Проверьте права на каталог и установку Python с модулем venv."
    fi
fi
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if ! "$VENV_PYTHON" -m scripts.check_environment --venv; then
    fail "Окружение .venv несовместимо. Пересоздайте его по docs/macos_first_run.md; автоматическое удаление не выполняется."
fi

if [ "$PREPARE" -eq 1 ]; then
    # Explicit user action; this is the only network installation path.
    if ! "$VENV_PYTHON" -m pip --disable-pip-version-check install --only-binary=:all: -r requirements/macos_catalina.txt; then
        fail "Не удалось установить зависимости. Проверьте интернет, сертификаты Python и архитектуру. Автономная установка описана в docs/macos_first_run.md"
    fi
    "$VENV_PYTHON" -m scripts.check_environment --venv --web --tests
fi

if ! "$VENV_PYTHON" -m scripts.check_environment --venv --web; then
    fail "Веб-зависимости не готовы. Проверьте ./start.command --check или явно выполните --prepare для установки."
fi
exec "$VENV_PYTHON" main.py "$@"
