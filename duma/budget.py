"""The most Duma may spend on the model in one day.

The day's total lives in a small file, so a restart does not hand out a fresh
allowance. A limit of 0 turns the cap off.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


class Budget:
    def __init__(self, path: Path, limit_usd: float, tz: str, now: Callable[[], datetime] | None = None):
        self._path = Path(path)
        self.limit_usd = limit_usd
        zone = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(zone))

    def _today(self) -> str:
        return self._now().date().isoformat()

    def spent(self) -> float:
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        if not isinstance(saved, dict) or saved.get("day") != self._today():
            return 0.0
        try:
            return float(saved.get("usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def add(self, usd: float) -> None:
        if usd <= 0:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"day": self._today(), "usd": round(self.spent() + usd, 6)})
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)

    def exhausted(self) -> bool:
        return self.limit_usd > 0 and self.spent() >= self.limit_usd
