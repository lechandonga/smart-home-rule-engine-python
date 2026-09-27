"""审计日志：所有关键决策（含越权拒绝）追加留痕并落盘。"""
from __future__ import annotations

import logging
from typing import Literal

from ..models import AuditEntry
from ..storage import Storage

logger = logging.getLogger("smart_home.audit")


class AuditService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def record(self, caller: str, operation: str, target: str,
               decision: Literal["allow", "deny"], detail: str = "") -> AuditEntry:
        with self.storage.lock:
            self.storage.data.audit_seq += 1
            entry = AuditEntry(
                seq=self.storage.data.audit_seq,
                caller=caller, operation=operation, target=target,
                decision=decision, detail=detail,
            )
            self.storage.data.audit.append(entry)
            self.storage.save()
        logger.info("audit #%d %s %s %s -> %s (%s)",
                    entry.seq, caller, operation, target, decision, detail)
        return entry

    def list_entries(self) -> list[AuditEntry]:
        return list(self.storage.data.audit)
