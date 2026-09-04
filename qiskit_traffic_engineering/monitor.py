"""Lightweight TTL cache for backend calibration data."""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple, TypeVar

T = TypeVar("T")


class CalibrationCache:
    """Simple TTL cache keyed by (backend_name, kind)."""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = ttl_seconds
        self._store: Dict[Tuple[str, str], Tuple[float, object]] = {}

    def get_or_fetch(self, backend_name: str, kind: str, fetch_fn: Callable[[], T]) -> T:
        key = (backend_name, kind)
        cached = self._store.get(key)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < self.ttl_seconds:
            return cached[1]  # type: ignore[return-value]
        value = fetch_fn()
        self._store[key] = (now, value)
        return value

    def invalidate(self, backend_name: Optional[str] = None) -> None:
        if backend_name is None:
            self._store.clear()
            return
        for key in [k for k in self._store if k[0] == backend_name]:
            del self._store[key]
