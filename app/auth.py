"""Caller registry and authorization boundary.

Callers are provisioned in a local JSON file (the "control plane" of this
local-only service). The authorization model is deliberately simple and
explicit:

* ``role == "admin"`` may access every device and manage every rule;
* a regular caller may only access devices listed in ``allowed_devices`` and
  only manage rules whose ``created_by`` equals its own caller id.

Rule *viewing* follows the same ownership boundary; a caller can only inspect a
rule if it created it (admins aside).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.models import Caller, Device, Rule


class CallerRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._callers: dict[str, Caller] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"调用方数据文件不存在: {self.path}（请参考 data/callers.example.json）"
            )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("callers 文件必须是数组")
        self._callers = {c["caller_id"]: Caller(**c) for c in raw}

    def get(self, caller_id: str) -> Caller | None:
        return self._callers.get(caller_id)


def can_access_device(caller: Caller, device: Device | str) -> bool:
    device_id = device.device_id if isinstance(device, Device) else device
    if caller.role == "admin":
        return True
    return device_id in caller.allowed_devices


def can_manage_rule(caller: Caller, rule: Rule) -> bool:
    if caller.role == "admin":
        return True
    return rule.created_by == caller.caller_id
