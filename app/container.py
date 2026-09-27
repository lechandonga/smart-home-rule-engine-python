"""Wired application container (single source of service instances)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.audit import AuditLog
from app.auth import CallerRegistry
from app.devices import DeviceService
from app.rules import RuleEngine, RuleService
from app.store import Store


@dataclass
class Container:
    data_dir: Path

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "state.json")
        self.audit = AuditLog(self.data_dir / "audit.jsonl")
        self.registry = CallerRegistry(self.data_dir / "callers.json")
        self.device_service = DeviceService(self.store, self.registry, self.audit)
        self.rule_engine = RuleEngine(self.store, self.device_service)
        self.rule_service = RuleService(
            self.store, self.registry, self.audit, self.rule_engine
        )
        # Reports/commands fan out to the rule engine inside the store lock.
        self.device_service.on_state_change = self.rule_engine.evaluate_event
