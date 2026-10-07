"""An append-only log of who asked what.

One JSON object per line. It records the admin's Telegram id, the event and the
tool parameters; never credentials and never the data a tool returned.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Audit:
    def __init__(self, path: Path):
        self._path = Path(path)

    def log(self, event: str, **fields: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields},
            ensure_ascii=False,
        )
        # 0600: the file names the athletes the admins asked about.
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
