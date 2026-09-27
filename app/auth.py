"""调用方身份与授权范围。基于 API key 的本地模拟认证。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Caller(BaseModel):
    """一个调用方及其被授权的资源范围。"""
    name: str
    # 可操作的 device_id 集合；"*" 表示全部
    devices: set[str] = Field(default_factory=set)
    # 可操作的 rule_id 集合；"*" 表示全部
    rules: set[str] = Field(default_factory=set)
    admin: bool = False

    def can_device(self, device_id: str) -> bool:
        return self.admin or "*" in self.devices or device_id in self.devices

    def can_rule(self, rule_id: str) -> bool:
        return self.admin or "*" in self.rules or rule_id in self.rules


# 本地模拟的 API key -> Caller 映射（不接入真实 IdP）
API_KEYS: dict[str, Caller] = {
    "admin-key": Caller(name="admin", admin=True),
    "alice-key": Caller(name="alice", devices={"lamp-1", "sensor-1"},
                        rules={"rule-1", "rule-2"}),
    "bob-key": Caller(name="bob", devices={"lamp-2"}, rules=set()),
}


def authenticate(api_key: str | None) -> Caller:
    """按 API key 识别调用方；未知/缺失 key 一律拒绝。"""
    from .errors import UnauthorizedError

    if not api_key:
        raise UnauthorizedError("缺少 API key（请求头 X-Api-Key）")
    caller = API_KEYS.get(api_key)
    if caller is None:
        raise UnauthorizedError("未知 API key", detail={"api_key": api_key[:4] + "***"})
    return caller
