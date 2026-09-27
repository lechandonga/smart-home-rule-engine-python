"""审计日志。空实现骨架。"""
from __future__ import annotations

from ..models import AuditEntry
from ..storage import Storage


class AuditService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def record(self, caller: str, operation: str, target: str,
               decision: str, detail: str = "") -> AuditEntry:
        raise NotImplementedError

    def list_entries(self) -> list[AuditEntry]:
        raise NotImplementedError
