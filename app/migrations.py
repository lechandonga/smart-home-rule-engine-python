"""存储数据版本迁移。无法安全迁移的版本明确拒绝。"""
from __future__ import annotations

from .errors import MigrationError
from .models import STORAGE_SCHEMA_VERSION


def migrate(raw: dict) -> dict:
    """把旧版本原始 dict 迁移到当前 STORAGE_SCHEMA_VERSION。

    - 低于最低可迁移版本或高于当前版本：抛 MigrationError。
    """
    raise NotImplementedError
