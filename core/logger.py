from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


class ProcessingLogger:
    """Small file logger used by background tasks.

    It deliberately avoids the stdlib logging global configuration so the
    plugin does not interfere with QGIS or other plugins.
    """

    def __init__(self, log_path: Optional[str] = None) -> None:
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str, level: str = "INFO") -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} [{level}] {message}"
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def extend(self, messages: Iterable[str], level: str = "INFO") -> None:
        for message in messages:
            self.write(message, level=level)

