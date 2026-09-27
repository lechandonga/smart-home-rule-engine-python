"""JSON 文件持久化：加载（含迁移）、保存。"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .migrations import migrate
from .models import StorageData

logger = logging.getLogger("smart_home.storage")


class Storage:
    """进程内数据 + JSON 文件落盘。

    - 所有变更在 ``lock`` 内完成，随后调用 ``save()`` 原子落盘；
    - 落盘采用"写临时文件 + os.replace"，避免崩溃留下半截文件。
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self.data = StorageData()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            logger.info("storage: no file at %s, start empty", self._path)
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        migrated = migrate(raw)  # 版本不兼容时抛 MigrationError
        self.data = StorageData.model_validate(migrated)
        logger.info(
            "storage: loaded schema_version=%s devices=%d rules=%d events=%d",
            self.data.schema_version, len(self.data.devices),
            len(self.data.rules), len(self.data.processed_events),
        )

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(self.data.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
