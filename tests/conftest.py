"""Shared pytest fixtures and test-logging helper.

Every test calls :func:`log_verdict` so the log output contains the case
inputs, the key intermediate state and the final decision basis required for
manual troubleshooting (pytest is configured with ``log_cli=true``).
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import Caller
from app.container import Container
from app.main import create_app

test_log = logging.getLogger("smart_home.test")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    shutil.copy(Path(__file__).parent / "callers.json", tmp_path / "callers.json")
    return tmp_path


@pytest.fixture
def container(data_dir: Path) -> Container:
    return Container(data_dir)


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app(data_dir))


@pytest.fixture
def admin(container: Container) -> Caller:
    return container.registry.get("admin")


@pytest.fixture
def alice(container: Container) -> Caller:
    return container.registry.get("alice")


@pytest.fixture
def bob(container: Container) -> Caller:
    return container.registry.get("bob")


def headers(caller_id: str) -> dict[str, str]:
    return {"X-Caller-Id": caller_id}


def log_verdict(caplog: pytest.LogCaptureFixture, case: str,
                inputs: object, states: object, verdict: object) -> None:
    with caplog.at_level(logging.INFO, logger="smart_home"):
        test_log.info("CASE=%s | 输入=%s", case, inputs)
        test_log.info("CASE=%s | 关键状态=%s", case, states)
        test_log.info("CASE=%s | 最终判定依据=%s", case, verdict)


def run_concurrently(target, n: int, *args, **kwargs) -> list:
    """Run ``target(worker_index, *args)`` in n threads released together."""
    barrier = threading.Barrier(n)
    results: list = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            results.append(target(i))
        except BaseException as exc:  # captured and re-raised by caller
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return results
