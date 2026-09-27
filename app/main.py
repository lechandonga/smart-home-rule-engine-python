"""FastAPI application factory and process-wide container."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from app.container import Container
from app.errors import register_exception_handlers
from app.routers import audit as audit_router
from app.routers import devices as devices_router
from app.routers import rules as rules_router


def create_app(data_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Smart Home Rule Engine (local simulation)",
        version="0.1.0",
        description="本地模拟的智能家居自动化服务：设备管理、状态/命令幂等、规则引擎、审计留痕。",
    )
    register_exception_handlers(app)

    data_dir = Path(data_dir or os.environ.get("SMART_HOME_DATA", "data"))
    app.state.container = Container(data_dir)

    app.include_router(devices_router.router)
    app.include_router(rules_router.router)
    app.include_router(audit_router.router)

    @app.get("/")
    def root():
        return {
            "service": "smart-home-rule-engine",
            "status": "ok",
            "data_dir": str(data_dir),
            "docs": "/docs",
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app(Path(os.environ.get("SMART_HOME_DATA", "data")))
