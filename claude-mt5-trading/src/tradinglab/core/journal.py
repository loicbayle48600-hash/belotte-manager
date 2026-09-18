"""Journal / audit trail : JSONL par jour + logging Python. Jamais de credentials."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .types import utcnow

SECRET_PATTERNS = re.compile(r"(password|passwd|secret|api[_-]?key|token)", re.IGNORECASE)


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_PATTERNS.search(str(k)) else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return _scrub(obj.to_dict())
    if hasattr(obj, "value") and not isinstance(obj, (int, float, str)):
        return obj.value
    return obj


class Journal:
    """Écrit chaque événement (décision, ordre, gate, review…) dans logs/journal-YYYY-MM-DD.jsonl."""

    def __init__(self, logs_dir: Path, tz_local: str = "Europe/Paris", component: str = "orchestrator"):
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tz_local = ZoneInfo(tz_local) if tz_local else timezone.utc
        self.component = component
        self._lock = threading.Lock()
        self.log = logging.getLogger(f"tradinglab.{component}")
        if not self.log.handlers:
            fh = logging.FileHandler(self.logs_dir / f"{component}.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            self.log.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            self.log.addHandler(sh)
            self.log.setLevel(logging.INFO)

    def _file(self, now: datetime) -> Path:
        return self.logs_dir / f"journal-{now.strftime('%Y-%m-%d')}.jsonl"

    def event(self, kind: str, level: str = "INFO", **data: Any) -> dict:
        now = utcnow()
        rec = {
            "ts_utc": now.isoformat(),
            "ts_local": now.astimezone(self.tz_local).isoformat(),
            "component": self.component,
            "kind": kind,
            **_scrub(data),
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._file(now), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        getattr(self.log, level.lower(), self.log.info)(f"{kind} {json.dumps(_scrub(data), ensure_ascii=False, default=str)[:600]}")
        return rec

    def info(self, msg: str, **data: Any) -> None:
        self.event("info", message=msg, **data)

    def warn(self, msg: str, **data: Any) -> None:
        self.event("warning", level="WARNING", message=msg, **data)

    def error(self, msg: str, **data: Any) -> None:
        self.event("error", level="ERROR", message=msg, **data)

    def read_day(self, day: datetime | None = None, kinds: set[str] | None = None) -> list[dict]:
        day = day or utcnow()
        f = self._file(day)
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds is None or rec.get("kind") in kinds:
                out.append(rec)
        return out
