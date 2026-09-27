"""Append-only JSONL audit trail.

Each authorization decision (allowed or denied) and each privileged action is
appended as one JSON line; the file is never rewritten, so the trail survives
crashes and cannot be quietly edited by later code paths.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.models import AuditEntry


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # File is created lazily on first append (see Store for rationale).

    def append(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.model_dump(), ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def list(self, limit: int = 100) -> list[AuditEntry]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        entries = [AuditEntry(**json.loads(line)) for line in lines if line.strip()]
        return entries[-limit:][::-1]
