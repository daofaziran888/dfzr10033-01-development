"""字段变更对照接口：版本化快照、别名版本、差异明细与投产校验。

固定路径单独放在一个 router 里，并在通用 ``/api/{module}`` 资源路由之前注册，
避免 ``field-diff`` 被当成普通业务模块。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services import field_diff

router = APIRouter(prefix="/api/field-diff", tags=["字段变更对照"])


def _body(payload: dict[str, Any] | None) -> dict[str, Any]:
    return payload or {}


# -- 总览 --

@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"ok": True, **field_diff.summary()}


# -- 来源字段快照 --

@router.post("/scans")
def scan_source(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    source_id = body.get("source_id")
    if source_id is None:
        raise HTTPException(status_code=400, detail="缺少 source_id")
    result = field_diff.scan_source(
        int(source_id),
        batch_no=body.get("batch_no"),
        fields=body.get("fields"),
        scanned_by=str(body.get("scanned_by") or "scanner"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/scans")
def list_scans(source_id: int | None = Query(default=None)) -> dict[str, Any]:
    rows = field_diff.list_scans(source_id)
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/scans/{scan_id}")
def get_scan(scan_id: int) -> dict[str, Any]:
    scan = field_diff.get_scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail=f"字段快照 {scan_id} 不存在")
    return scan


# -- 字段别名版本 --

@router.get("/models/{model_id}/overview")
def model_overview(model_id: int) -> dict[str, Any]:
    result = field_diff.model_overview(model_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/models/{model_id}/aliases")
def list_alias_versions(model_id: int) -> dict[str, Any]:
    return {"ok": True, **field_diff.list_alias_versions(model_id)}


@router.post("/models/{model_id}/aliases")
def save_alias_version(model_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    result = field_diff.save_alias_version(
        model_id,
        body.get("entries") or [],
        confirmed_by=str(body.get("confirmed_by") or "validator"),
        note=str(body.get("note") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


# -- 差异批次 --

@router.post("/diffs")
def create_diff(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    model_id = body.get("model_id")
    if model_id is None:
        raise HTTPException(status_code=400, detail="缺少 model_id")
    result = field_diff.create_diff_batch(
        int(model_id),
        source_id=int(body["source_id"]) if body.get("source_id") is not None else None,
        baseline_scan_id=int(body["baseline_scan_id"]) if body.get("baseline_scan_id") is not None else None,
        target_scan_id=int(body["target_scan_id"]) if body.get("target_scan_id") is not None else None,
        alias_version=int(body["alias_version"]) if body.get("alias_version") is not None else None,
        requested_by=str(body.get("requested_by") or "validator"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/diffs")
def list_diffs(
    model_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    source_id: int | None = Query(default=None),
) -> dict[str, Any]:
    rows = field_diff.list_diff_batches(model_id=model_id, status=status, source_id=source_id)
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/diffs/{batch_id}")
def get_diff(batch_id: int) -> dict[str, Any]:
    batch = field_diff.get_diff_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"差异批次 {batch_id} 不存在")
    return batch


@router.get("/diffs/{batch_id}/entries")
def list_diff_entries(batch_id: int, resolution: str | None = Query(default=None)) -> dict[str, Any]:
    if field_diff.get_diff_batch(batch_id, include_entries=False) is None:
        raise HTTPException(status_code=404, detail=f"差异批次 {batch_id} 不存在")
    rows = field_diff.list_diff_entries(batch_id, resolution=resolution)
    return {"ok": True, "items": rows, "total": len(rows)}


@router.post("/diffs/{batch_id}/confirm")
def confirm_batch(batch_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    result = field_diff.confirm_diff_batch(batch_id, reviewer=str(body.get("reviewer") or "validator"))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/entries/{entry_id}/confirm")
def confirm_entry(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    resolution = str(body.get("resolution") or "").strip()
    if not resolution:
        raise HTTPException(status_code=400, detail="缺少 resolution")
    result = field_diff.confirm_diff_entry(
        entry_id,
        resolution,
        reviewer=str(body.get("reviewer") or "validator"),
        note=str(body.get("note") or ""),
        alias=body.get("alias"),
        physical_column=body.get("physical_column"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


# -- 投产校验 --

@router.post("/models/{model_id}/promotions")
def start_promotion(model_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    result = field_diff.start_promotion(
        model_id, requested_by=str(body.get("requested_by") or "model_owner"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/models/{model_id}/promotions")
def list_promotions(model_id: int) -> dict[str, Any]:
    rows = field_diff.list_promotions(model_id)
    return {"ok": True, "items": rows, "total": len(rows)}


@router.post("/promotions/{promotion_id}/finish")
def finish_promotion(promotion_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body(payload)
    result = field_diff.finish_promotion(
        promotion_id, concluded_by=str(body.get("concluded_by") or "model_owner"),
    )
    if not result.get("message"):
        raise HTTPException(status_code=400, detail="投产记录不存在")
    return result


@router.get("/promotions/{promotion_id}")
def get_promotion(promotion_id: int) -> dict[str, Any]:
    result = field_diff.get_promotion(promotion_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


# -- 历史缺列回查 --

@router.get("/confirmed-missing")
def lookup_confirmed_missing(
    field_id: str = Query(...),
    source_id: int | None = Query(default=None),
    model_id: int | None = Query(default=None),
) -> dict[str, Any]:
    rows = field_diff.lookup_confirmed_missing(field_id, source_id=source_id, model_id=model_id)
    return {"ok": True, "field_id": field_id, "items": rows, "total": len(rows)}
