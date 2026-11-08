"""Console launcher for the Windows onedir distribution and explicit test profiles."""
import argparse
import json
import logging
import signal
import sys
import webbrowser

from .instance import ProfileInstance
from .logging import attach_safe_handler, configure_logging
from .paths import PortableError, PortablePaths
from .resources import validate_resources
from .storage import VERSION, checked_backup, diagnostic_report, prepare_database


def open_browser(origin):
    try:
        if webbrowser.open(origin):
            return
    except (OSError, webbrowser.Error):
        pass
    print('Браузер не открылся. Введите локальный адрес вручную: ' + origin, flush=True)


def run(paths, *, port=0, browser=True, backup=False):
    paths.ensure_writable()
    logger, handler = configure_logging(paths.logs)
    try:
        with ProfileInstance(paths.profile) as instance:
            if not instance.acquire():
                if backup:
                    raise PortableError('Перед резервным копированием завершите запущенное приложение.')
                origin = instance.existing_origin()
                print('Приложение уже запущено: ' + origin, flush=True)
                if browser: open_browser(origin)
                return 0
            if backup:
                if not paths.database.is_file():
                    raise PortableError('База ещё не создана. Резервная копия не требуется.')
                print('Проверенная резервная копия: ' + str(checked_backup(paths.database, paths.backups)), flush=True)
                return 0
            validate_resources(paths.resources, require_manifest=getattr(sys, 'frozen', False))
            prepare_database(paths)
            from app.web import create_app
            from .server import PortableServer
            import secrets
            app = create_app({'DATABASE': str(paths.database), 'DEBUG': False})
            attach_safe_handler(app.logger, handler)
            for name in ('waitress', 'waitress.queue', 'waitress.channel'):
                attach_safe_handler(logging.getLogger(name), handler)
            token = secrets.token_hex(32)
            server = PortableServer(app, token=token, port=port)
            previous = {}
            try:
                for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
                    sig = getattr(signal, name, None)
                    if sig is not None:
                        previous[sig] = signal.getsignal(sig)
                        signal.signal(sig, lambda *_: server.request_stop())
                server.start()
                instance.publish(server.origin, token)
                print('Construction Progress {}: {}'.format(VERSION, server.origin), flush=True)
                print('Данные: ' + str(paths.database), flush=True)
                print('Завершение: кнопка «Завершить работу» в приложении или Ctrl+C в этом окне. Дождитесь сообщения об остановке.', flush=True)
                logger.info('server_ready')
                if browser: open_browser(server.origin)
                server.wait()
            finally:
                server.close()
                for sig, old in previous.items(): signal.signal(sig, old)
                logger.info('server_stopped')
                print('Сервер остановлен. Данные сохранены.', flush=True)
    except BaseException:
        logger.error('launcher_failed', exc_info=True)
        raise
    finally:
        handler.close()
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description='Construction Progress Windows Portable')
    parser.add_argument('--data-dir', help='Отдельный каталог профиля (data, backups, exports, logs)')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-pause', action='store_true', help='Не ждать Enter при ошибке EXE')
    parser.add_argument('--port', type=int, default=0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--diagnose', action='store_true', help='Read-only диагностика без миграций')
    mode.add_argument('--backup', action='store_true', help='Проверенная копия Backup API при остановленном приложении')
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535:
            raise PortableError('Порт должен быть от 0 до 65535.')
        paths = PortablePaths.discover(data_dir=args.data_dir)
        if args.diagnose:
            report = diagnostic_report(paths)
            try:
                validate_resources(paths.resources, require_manifest=getattr(sys, 'frozen', False))
                report['resources_check'] = 'ok'
            except PortableError:
                report['resources_check'] = 'failed'
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return int(report.get('integrity_check') == 'failed' or report['resources_check'] != 'ok')
        return run(paths, port=args.port, browser=not args.no_browser, backup=args.backup)
    except PortableError as exc:
        print('Ошибка: ' + str(exc), flush=True)
    except (OSError, ImportError) as exc:
        print('Не удалось запустить приложение ({}). Проверьте полный состав сборки, права записи и logs/application.log.'.format(type(exc).__name__), flush=True)
    except Exception as exc:
        print('Ошибка запуска ({}). Сервер остановлен; подробности категории ошибки — в logs/application.log.'.format(type(exc).__name__), flush=True)
    if getattr(sys, 'frozen', False) and not args.no_pause and sys.stdin is not None and sys.stdin.isatty():
        try: input('Нажмите Enter, чтобы закрыть окно…')
        except (EOFError, KeyboardInterrupt): pass
    return 2
