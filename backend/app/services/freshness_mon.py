"""新鲜度监控领域服务：数据集SLA监控，跨模块延迟告警与数据质量追踪。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FreshnessMonService(BaseModuleService):
    MODULE = "freshness_mon"
    ENTITY = "新鲜度规则"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["规则编号", "目标数据集", "SLA秒数", "检测频率", "延迟阈值", "告警级别", "触发次数", "最后检查"]
    REQUIRED_FIELDS = ["规则编号", "目标数据集"]
    STATUS_ORDER = ["草稿", "监控中", "已告警", "已暂停"]
    ACTION_RULES = {"启用监控": "监控中", "暂停监控": "已暂停", "确认告警": "已告警"}
    NEGATIVE_ACTIONS = ["暂停监控"]

    def check_data_freshness(self, dataset_ref: str, sla_seconds: int = 3600) -> dict[str, Any]:
        """检查指定数据集的新鲜度，跨模块收集最后更新时间。

        跨模块数据流：
        - 读取 source_registry 获取最近同步时间
        - 读取 result_cache 获取缓存刷新时间
        - 读取 data_lake_read 获取最后扫描时间
        - 如果超时，写入警报并更新数据契约
        """
        freshness_status = {
            "source_registry": _check_source_freshness(dataset_ref, sla_seconds),
            "result_cache": _check_cache_freshness(dataset_ref, sla_seconds),
            "data_lake_read": _check_lake_freshness(dataset_ref, sla_seconds),
        }

        is_stale = any(s["stale"] for s in freshness_status.values() if s.get("stale"))

        if is_stale:
            # 触发告警
            rows = store.rows(self.MODULE)
            mon_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            mon = {
                "id": mon_id,
                "规则编号": f"FRES-A{mon_id:04d}",
                "目标数据集": dataset_ref,
                "SLA秒数": str(sla_seconds),
                "检测频率": "on_check",
                "延迟阈值": f"超过SLA({sla_seconds}s)",
                "告警级别": "WARNING",
                "触发次数": "1",
                "最后检查": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "已告警",
                "pending": True,
                "abnormal": True,
            }
            rows.append(mon)

            # 更新契约状态
            _mark_contract_breached(dataset_ref)

        return {
            "ok": True,
            "dataset": dataset_ref,
            "sla_seconds": sla_seconds,
            "is_stale": is_stale,
            "breakdown": freshness_status,
        }


def _check_source_freshness(dataset_ref: str, sla_seconds: int) -> dict[str, Any]:
    rows = store.rows("source_registry")
    for r in rows:
        if dataset_ref in str(r.get("源编号", "")) or dataset_ref in str(r.get("连接地址", "")):
            sync_time = r.get("最近同步", "")
            stale = _is_stale(sync_time, sla_seconds)
            return {"source": r.get("源编号"), "last_sync": sync_time, "stale": stale}
    return {"source": "unknown", "last_sync": "N/A", "stale": False}


def _check_cache_freshness(dataset_ref: str, sla_seconds: int) -> dict[str, Any]:
    rows = store.rows("result_cache")
    for r in rows:
        if dataset_ref in str(r.get("查询摘要", "")):
            create_time = r.get("创建时间", "")
            stale = _is_stale(create_time, sla_seconds)
            return {"entry_id": r["id"], "last_refresh": create_time, "stale": stale}
    return {"entry_id": 0, "last_refresh": "N/A", "stale": False}


def _check_lake_freshness(dataset_ref: str, sla_seconds: int) -> dict[str, Any]:
    rows = store.rows("data_lake_read")
    for r in rows:
        if dataset_ref in str(r.get("存储位置", "")):
            scan_time = r.get("最后扫描", "")
            stale = _is_stale(scan_time, sla_seconds)
            return {"path_id": r["id"], "last_scan": scan_time, "stale": stale}
    return {"path_id": 0, "last_scan": "N/A", "stale": False}


def _is_stale(timestamp_str: str, sla_seconds: int) -> bool:
    if not timestamp_str or timestamp_str == "N/A":
        return True
    try:
        ts = time.mktime(time.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S"))
        return time.time() - ts > sla_seconds
    except (ValueError, OverflowError, AttributeError):
        return False


def _mark_contract_breached(dataset_ref: str) -> None:
    contracts = store.rows("data_contract")
    for c in contracts:
        if c.get("status") == "已承诺" and dataset_ref in str(c.get("数据集", "")):
            c["status"] = "已违约"