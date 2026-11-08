"""Diagnostic logs contain event codes/classes, never form values or tracebacks."""
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import re


class SafeFormatter(logging.Formatter):
    def format(self, record):
        code = record.msg if (record.name == 'construction_progress.portable' and
                              isinstance(record.msg, str) and re.fullmatch('[a-z_]{1,80}', record.msg)) else 'runtime_message'
        error = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else ''
        return '{} {} {} {}'.format(datetime.now(timezone.utc).isoformat(), record.levelname, code, error).rstrip()


def configure_logging(folder):
    handler = RotatingFileHandler(folder / 'application.log', maxBytes=1024*1024,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(SafeFormatter())
    logger = logging.getLogger('construction_progress.portable')
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    return logger, handler


def attach_safe_handler(logger, handler):
    logger.handlers = [handler]
    logger.propagate = False
