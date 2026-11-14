"""Scheduler à cadences multiples (pas de HFT) : intervalles + détection de clôture de barre."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..core.clock import is_new_bar, seconds_until_next_bar
from ..core.types import utcnow


class Scheduler:
    def __init__(self, intervals: dict[str, float], timeframes: list[str]):
        self.intervals = dict(intervals)
        self.timeframes = list(timeframes)
        self._last: dict[str, datetime] = {}
        self._bars: dict[str, datetime] = {}

    def due(self, name: str, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        last = self._last.get(name)
        if last is None or (now - last).total_seconds() >= self.intervals.get(name, 60):
            self._last[name] = now
            return True
        return False

    def new_bars(self, now: Optional[datetime] = None) -> list[str]:
        """Timeframes dont une nouvelle barre vient de s'ouvrir (donc la précédente est clôturée)."""
        now = now or utcnow()
        out = []
        for tf in self.timeframes:
            new, cur = is_new_bar(tf, self._bars.get(tf), now)
            if new:
                # au premier passage, on initialise sans déclencher
                if tf in self._bars:
                    out.append(tf)
                self._bars[tf] = cur
        return out

    def sleep_seconds(self, base: float, now: Optional[datetime] = None) -> float:
        now = now or utcnow()
        nxt = min([seconds_until_next_bar(tf, now) for tf in self.timeframes] + [base])
        return max(1.0, min(base, nxt + 0.5))
