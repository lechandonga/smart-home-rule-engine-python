"""测试公共装置：每个用例使用独立的临时存储文件。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import deps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

ADMIN = {"X-Api-Key": "admin-key"}
ALICE = {"X-Api-Key": "alice-key"}
BOB = {"X-Api-Key": "bob-key"}


@pytest.fixture()
def client(tmp_path):
    deps.init_services(str(tmp_path / "store.json"))
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def store_path(tmp_path):
    return tmp_path / "store.json"
