"""Logging configuration.

Policy: log metadata (method, path, status, latency, request id) only.
Never log request/response bodies, prompts, completions or credentials.

Logs go to stdout and, when `log_dir` is set, to a rotating file there, so the traceback of a failed
request is still available after the terminal that started the server is gone.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_FILE_BYTES = 10 * 1024 * 1024
_FILE_BACKUPS = 5


def configure_logging(level: str, log_dir: str = "", filename: str = "infraspine.log") -> None:
    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                directory / filename, maxBytes=_FILE_BYTES, backupCount=_FILE_BACKUPS, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in root.handlers:
        old.close()
    root.handlers = handlers
    root.setLevel(level)

    # The HTTP clients and PDF parsers can log request/document contents at DEBUG; keep them quiet.
    # (openai>=3 ships its HTTP client under the "httpx2" logger name.)
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
