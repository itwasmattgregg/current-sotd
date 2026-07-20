"""Ring-buffer + rotating file logging for the UI and disk."""

from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._lines.append(msg)
        except Exception:
            self.handleError(record)

    def get_lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


_ring: RingBufferHandler | None = None


def setup_logging(log_dir: str | Path, name: str = "sotd") -> logging.Logger:
    global _ring
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    _ring = RingBufferHandler(capacity=500)
    _ring.setFormatter(fmt)
    logger.addHandler(_ring)

    file_handler = RotatingFileHandler(
        log_path / "app.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    return logger


def get_log_lines() -> list[str]:
    if _ring is None:
        return []
    return _ring.get_lines()
