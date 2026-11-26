"""采集量指标迁移核验接口。

同一组路由挂两个前缀：
- /api/metrics-migration/*                （迁移核验台主入口）
- /api/collect_metrics/migration/*       （归属到采集量指标模块的别名）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.metrics_migration import (
    MigrationConflict,
    migration_service,
)

router = APIRouter(tags=["采集量迁移核验"])


class SiteBody(BaseModel):
    site: str | None = "K"


class LegacyBody(BaseModel):
    site: str | None = "K"
    total: int = 37


class BackfillBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    site: str | None = "K"
    amount: int | str | None = Field(default=2)
    value: int | str | None = None
    delta: int | str | None = None
    window_start: str | None = None
    window: str | None = None
    report_key: str | None = None
    idempotency_key: str | None = None
    audit_id: str | None = None
    expected_fence: int | None = None


class SwitchBody(BaseModel):
    expected_fence: int | None = None


def _conflict_response(exc: MigrationConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content=exc.payload)


def _backfill_kwargs(payload: BackfillBody) -> dict[str, Any]:
    amount = payload.amount
    if amount is None or isinstance(amount, str) and not amount.strip():
        amount = payload.value if payload.value is not None else payload.delta
    return {
        "site": payload.site,
        "amount": 2 if amount is None else amount,
        "window_start": payload.window_start or payload.window,
        "report_key": payload.report_key or payload.idempotency_key,
        "audit_id": payload.audit_id,
    }


@router.get("/state")
def state(site: str | None = "K") -> dict[str, Any]:
    return migration_service.state(site)


@router.get("/legacy")
def legacy(site: str | None = "K") -> dict[str, Any]:
    return migration_service.read_legacy(site)


@router.get("/windows")
def windows(site: str | None = "K") -> dict[str, Any]:
    return migration_service.read_windows(site)


@router.get("/receipts")
def receipts(site: str | None = "K") -> dict[str, Any]:
    return {"ok": True, "items": migration_service.list_receipts(site)}


@router.get("/receipt")
def receipt(report_key: str) -> dict[str, Any]:
    data = migration_service.receipt_info(report_key)
    if data is None:
        return {"ok": False, "message": f"没有补报单据：{report_key}"}
    return {"ok": True, "receipt": data}


@router.get("/reconciliation")
def reconciliation(site: str | None = "K") -> dict[str, Any]:
    latest = migration_service.latest_reconciliation(site)
    return {"ok": latest is not None, "reconciliation": latest}


@router.post("/legacy/backfill")
def legacy_backfill(payload: LegacyBody) -> dict[str, Any]:
    return migration_service.receive_legacy_total(payload.site or "K", payload.total)


@router.post("/backfill")
def backfill(payload: BackfillBody):
    kwargs = _backfill_kwargs(payload)
    kwargs["expected_fence"] = payload.expected_fence
    try:
        return migration_service.publish_backfill(**kwargs)
    except MigrationConflict as exc:
        return _conflict_response(exc)


@router.post("/backfill/conflict")
def backfill_conflict(payload: BackfillBody):
    kwargs = _backfill_kwargs(payload)
    try:
        return migration_service.conflict_drill(**kwargs)
    except MigrationConflict as exc:
        return _conflict_response(exc)


@router.post("/switch/confirm")
def confirm_switch(payload: SwitchBody | None = None):
    expected = payload.expected_fence if payload else None
    try:
        return migration_service.confirm_switch(expected)
    except MigrationConflict as exc:
        return _conflict_response(exc)


@router.post("/reconciliation/verify")
def verify(payload: SiteBody | None = None) -> dict[str, Any]:
    site = payload.site if payload else "K"
    result = migration_service.reconcile(site or "K")
    result["message"] = (
        "切换对账单复验通过：累计一致、无重复统计"
        if result["matched"] else "切换对账单复验不平，请检查差异"
    )
    return result


@router.post("/rollback")
def rollback() -> dict[str, Any]:
    return migration_service.rollback()


@router.post("/reset")
def reset(payload: SiteBody | None = None) -> dict[str, Any]:
    site = payload.site if payload else "K"
    return migration_service.reset_scenario(site or "K")
