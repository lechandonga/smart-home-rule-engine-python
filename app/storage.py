"""JSON 文件持久化：加载、迁移、保存。"""
from __future__ import annotations

import threading
from pathlib import Path

from .models import StorageData


class Storage:
    """进程内数据 + JSON 文件落盘。所有写操作在锁内完成并立即落盘。"""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self.data = StorageData()

    def load(self) -> None:
        raise NotImplementedError

    def save(self) -> None:
        raise NotImplementedError

    @property
    def lock(self) -> threading.RLock:
        return self._lock
