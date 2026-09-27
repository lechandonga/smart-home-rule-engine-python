"""Device HTTP API (thin transport layer; logic lives in DeviceService)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import Caller
from app.deps import get_caller, get_device_service
from app.devices import DeviceService
from app.models import (
    CommandRequest,
    DeviceStatus,
    RegisterDeviceRequest,
    StateReport,
)

router = APIRouter(prefix="/devices", tags=["devices"])


class StatusBody(BaseModel):
    status: DeviceStatus


class ReportBody(BaseModel):
    state: dict[str, Any]
    seq: int = Field(ge=0)
    token: str = Field(min_length=1)
    reported_at: str | None = None


@router.post("")
def register_device(
    body: RegisterDeviceRequest,
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    return svc.register_device(body, caller)


@router.get("")
def list_devices(
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    return svc.list_devices(caller)


@router.get("/{device_id}")
def get_device(
    device_id: str,
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    return svc.get_device(device_id, caller)


@router.post("/{device_id}/status")
def set_status(
    device_id: str,
    body: StatusBody,
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    return svc.set_status(device_id, body.status, caller)


@router.post("/{device_id}/reports")
def report_state(
    device_id: str,
    body: ReportBody,
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    report = StateReport(
        device_id=device_id,
        state=body.state,
        seq=body.seq,
        token=body.token,
        reported_at=body.reported_at or "",
    )
    return svc.report_state(report, caller)


@router.post("/{device_id}/commands")
def send_command(
    device_id: str,
    body: CommandRequest,
    svc: DeviceService = Depends(get_device_service),
    caller: Caller = Depends(get_caller),
):
    # The device id in the path is authoritative; keep the body flexible by
    # normalizing it to the path value when absent/divergent.
    payload = body.model_copy(update={"device_id": device_id})
    return svc.send_command(payload, caller)
