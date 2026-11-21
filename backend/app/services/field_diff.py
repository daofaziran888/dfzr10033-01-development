"""字段变更对照领域服务：来源扫描快照、字段别名版本与语义模型投产校验。

这个服务把「来源扫描到的字段」和「语义模型实际使用的字段别名」拆成两条可回放的
版本线，供投产校验按版核对：

* ``field_scan_batch``  每次扫描生成一个不可变的字段快照批次，按 (来源, 批次) 幂等；
* ``field_alias``       别名映射只追加、不覆盖，每个版本都能还原当时的映射；
* ``field_diff_batch``  一次对照的完整输入（基准快照、目标快照、别名版本）与结论；
* ``field_diff_entry``  新增 / 删除 / 改名 / 缺列明细，待确认与已确认严格分开；
* ``field_validation``  一次投产校验读取到的差异明细与结论；
* ``model_promotion``   投产记录，固定当次采用的扫描批次和别名版本。

关键规则：

1. 字段身份是 ``(来源ID, 字段标识)``，不同来源的同名列永远不会被合并；
2. 只有字段标识在目标快照里彻底消失才算「真正缺列」，字段标识仍在、只是物理列
   改名时，别名仍指向同一物理列，自动沿用；
3. 同一来源同一批次并发扫描只会留下一条「生效」快照，其余请求拿到同一个批次；
4. 已确认的差异批次不可改写，后续复核产生新批次，历史批次按原口径保留；
5. 投产校验先读取并冻结差异与别名版本，之后的别名更正不会回写这次校验的结论，
   但重新查询本次投产时会给出「当前别名下该度量能否投产」的对照结果。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from typing import Any, Iterable

from app.store import store

# 扫描、差异确认、投产校验共用一把可重入锁：同一来源同一批次的「检查 + 写入」
# 必须是一个原子操作，否则并发请求会各自认为自己是第一次写入。
_LOCK = threading.RLock()

SCAN_TABLE = "field_scan_batch"
ALIAS_TABLE = "field_alias"
DIFF_TABLE = "field_diff_batch"
ENTRY_TABLE = "field_diff_entry"
VALIDATION_TABLE = "field_validation"
PROMOTION_TABLE = "model_promotion"

SCAN_EFFECTIVE = "生效"
SCAN_HISTORY = "历史"
DIFF_PENDING = "待确认"
DIFF_CONFIRMED = "已确认"

# 自动结论：这些差异不需要人工确认，但仍然作为差异明细保留。
AUTO_ALIAS_KEPT = "别名沿用"
AUTO_NAME_MATCHED = "按物理列名沿用"
AUTO_IGNORED = "无需处理"

RESOLUTION_PENDING = "待确认"
RESOLUTION_MISSING = "确认缺列"
RESOLUTION_RENAMED = "确认改名"
RESOLUTION_ALIAS_FIXED = "已更正别名"
RESOLUTION_NEW_FIELD = "映射新增字段"
RESOLUTION_IGNORED = "忽略"
RESOLUTION_KEPT = AUTO_ALIAS_KEPT
RESOLUTION_NAMED = AUTO_NAME_MATCHED

RESOLVED_STATES = {
    RESOLUTION_MISSING,
    RESOLUTION_RENAMED,
    RESOLUTION_ALIAS_FIXED,
    RESOLUTION_NEW_FIELD,
    RESOLUTION_IGNORED,
    AUTO_ALIAS_KEPT,
    AUTO_NAME_MATCHED,
    AUTO_IGNORED,
}

CHANGE_ADDED = "新增"
CHANGE_REMOVED = "删除"
CHANGE_RENAMED = "改名"
CHANGE_MISSING = "缺列"
CHANGE_ALIAS_KEPT = "别名沿用"

_SQL_WORDS = {
    "select", "from", "where", "as", "and", "or", "not", "null", "case", "when",
    "then", "else", "end", "distinct", "over", "partition", "by", "order", "group",
    "having", "limit", "cast", "coalesce", "round", "sum", "count", "avg", "min",
    "max", "total", "filter", "with", "union", "all", "desc", "asc",
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _deep(value: Any) -> Any:
    return copy.deepcopy(value)


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    return _text(value).casefold()


def _split_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_text(item) for item in value if _text(item)]
    if value is None:
        return []
    parts = re.split(r"[,，;；、\n]+", str(value))
    return [part.strip() for part in parts if part.strip()]


def _field_id_for(source_id: int, ordinal: int, explicit: Any = None) -> str:
    if _text(explicit):
        return _text(explicit)
    return f"S{source_id}-C{ordinal:02d}"


def _normalize_fields(source_id: int, fields: Iterable[Any] | None) -> list[dict[str, Any]]:
    """把多种输入形态统一成带稳定字段标识的快照。

    字段标识优先取提交方显式给出的 ``field_id`` / ``字段标识``；没有时按来源内
    序号兜底。序号由同一来源的「物理列位置」决定，因此列改名仍能识别为改名，
    而不是一删一增。
    """
    normalized: list[dict[str, Any]] = []
    if fields is None:
        return normalized
    if isinstance(fields, str):
        fields = _split_list(fields)

    previous_by_name: dict[str, str] = {}
    for row in store.rows(SCAN_TABLE):
        if _int(row.get("source_id")) != source_id:
            continue
        for field in row.get("fields", []):
            previous_by_name.setdefault(_fold(field.get("physical_column") or field.get("name")), _text(field.get("field_id")))
            previous_by_name.setdefault(_fold(field.get("name")), _text(field.get("field_id")))

    used: set[str] = set()
    for index, item in enumerate(fields, start=1):
        if isinstance(item, dict):
            name = _text(item.get("name") or item.get("物理列") or item.get("column") or item.get("字段名"))
            physical = _text(item.get("physical_column") or item.get("物理列") or name)
            explicit = item.get("field_id") or item.get("字段标识")
            data_type = _text(item.get("data_type") or item.get("类型")) or "string"
        else:
            name = _text(item)
            physical = name
            explicit = None
            data_type = "string"
        if not name and not physical:
            continue
        if not name:
            name = physical
        if not physical:
            physical = name
        ordinal_id = _field_id_for(source_id, index, explicit)
        if _text(explicit):
            field_id = ordinal_id
        else:
            # 同名物理列优先继承上一版字段标识；没有命中时按位置兜底，位置也冲突时
            # 追加序号，避免两条不同列拿到同一个字段标识。
            inherited = previous_by_name.get(_fold(physical))
            field_id = inherited or ordinal_id
            if field_id in used and not inherited:
                suffix = 2
                while f"{ordinal_id}-{suffix}" in used:
                    suffix += 1
                field_id = f"{ordinal_id}-{suffix}"
        used.add(field_id)
        normalized.append({
            "field_id": field_id,
            "name": name,
            "physical_column": physical,
            "data_type": data_type,
            "ordinal": index,
        })
    return normalized


def _source_refs(model: dict[str, Any]) -> list[int]:
    refs: list[int] = []
    for part in re.split(r"[,，;；、\s]+", _text(model.get("源表映射"))):
        if ":" not in part:
            continue
        _, _, raw_id = part.partition(":")
        if raw_id.strip().isdigit() and int(raw_id) not in refs:
            refs.append(int(raw_id))
    return refs


def _source_no(source_id: int) -> str:
    source = store.find("source_registry", source_id)
    return _text(source.get("源编号")) if source else ""


def _latest_scan(source_id: int) -> dict[str, Any] | None:
    effective = [
        row for row in store.rows(SCAN_TABLE)
        if _int(row.get("source_id")) == source_id and row.get("status") == SCAN_EFFECTIVE
    ]
    if effective:
        return max(effective, key=lambda row: int(row.get("version", 0)))
    rows = [row for row in store.rows(SCAN_TABLE) if _int(row.get("source_id")) == source_id]
    return max(rows, key=lambda row: int(row.get("version", 0))) if rows else None


def _find_scan(source_id: int, batch_no: str) -> dict[str, Any] | None:
    for row in store.rows(SCAN_TABLE):
        if _int(row.get("source_id")) == source_id and row.get("batch_no") == batch_no:
            return row
    return None


def _discover_fields(source: dict[str, Any]) -> list[str]:
    # 延迟导入，避免 source_registry 将来反向依赖本模块时形成循环。
    from app.services.source_registry import _discover_schema

    return list(_discover_schema(_text(source.get("源类型")) or "unknown"))


def _field_by_id(fields: Iterable[dict[str, Any]], field_id: str) -> dict[str, Any] | None:
    for field in fields:
        if _text(field.get("field_id")) == field_id:
            return field
    return None


def _field_by_name(fields: Iterable[dict[str, Any]], name: str) -> dict[str, Any] | None:
    folded = _fold(name)
    for field in fields:
        if _fold(field.get("name")) == folded or _fold(field.get("physical_column")) == folded:
            return field
    return None


def _snapshot_map(scan: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not scan:
        return {}
    return {_text(field.get("field_id")): field for field in scan.get("fields", [])}


def _alias_tokens(model: dict[str, Any], source_id: int) -> list[dict[str, str]]:
    """把模型的「字段别名」拆成 alias -> physical column 的候选映射。"""
    tokens = _split_list(model.get("字段别名"))
    result = []
    for token in tokens:
        alias = token
        physical = token
        for separator in ("=", "=>", "->", ":"):
            if separator in token:
                left, _, right = token.partition(separator)
                if left.strip() and right.strip():
                    alias, physical = left.strip(), right.strip()
                break
        result.append({
            "alias": alias,
            "physical_column": physical,
            "source_id": str(source_id),
        })
    return result


# ---------------------------------------------------------------------------
# 扫描快照
# ---------------------------------------------------------------------------

def scan_source(
    source_id: int,
    batch_no: str | None = None,
    fields: Iterable[Any] | None = None,
    scanned_by: str = "scanner",
) -> dict[str, Any]:
    """扫描来源字段并落一个版本快照。

    幂等键是 ``(source_id, batch_no)``：同来源同批次并发调用只会生成一条生效
    快照，后来者直接拿到第一条的 id 和内容，不会覆盖已落盘输入。
    """
    with _LOCK:
        source = store.find("source_registry", source_id)
        if source is None:
            return {"ok": False, "message": f"数据源 {source_id} 不存在或已归档"}

        if fields is None:
            fields = _discover_fields(source)
        normalized = _normalize_fields(source_id, fields)
        if not normalized:
            return {"ok": False, "message": "来源字段快照不能为空"}

        digest = _digest({
            "source_id": source_id,
            "fields": [(f["field_id"], f["name"], f["physical_column"]) for f in normalized],
        })
        batch = _text(batch_no) or f"AUTO-{digest[:8]}"

        existing = _find_scan(source_id, batch)
        if existing is not None:
            return {
                "ok": True,
                "deduplicated": True,
                "input_conflict": existing.get("input_digest") != digest,
                "scan": _deep(existing),
            }

        versions = [
            int(row.get("version", 0))
            for row in store.rows(SCAN_TABLE)
            if _int(row.get("source_id")) == source_id
        ]
        version = max(versions, default=0) + 1

        # 新版本生效，旧版本保留快照内容，只把状态标记为历史。
        for row in store.rows(SCAN_TABLE):
            if _int(row.get("source_id")) == source_id and row.get("status") == SCAN_EFFECTIVE:
                row["status"] = SCAN_HISTORY

        row = {
            "id": _next_id(SCAN_TABLE),
            "scan_batch_id": f"SCAN-{source_id:04d}-{version:04d}",
            "source_id": source_id,
            "source_no": _source_no(source_id),
            "source_type": _text(source.get("源类型")),
            "batch_no": batch,
            "version": version,
            "fields": _deep(normalized),
            "field_count": len(normalized),
            "input_digest": digest,
            "scanned_by": scanned_by,
            "scanned_at": _now(),
            "status": SCAN_EFFECTIVE,
            "pending": False,
            "abnormal": False,
        }
        store.rows(SCAN_TABLE).append(row)
        return {"ok": True, "deduplicated": False, "input_conflict": False, "scan": _deep(row)}


def ensure_baseline_scan(source_id: int) -> dict[str, Any] | None:
    """来源还没有任何快照时，用当前发现结果补一个基线版本。"""
    with _LOCK:
        existing = _latest_scan(source_id)
        if existing is not None:
            return existing
        result = scan_source(source_id, batch_no=f"BASELINE-{source_id}", scanned_by="validator")
        return result.get("scan") if result.get("ok") else None


def list_scans(source_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(SCAN_TABLE)
            if source_id is None or _int(row.get("source_id")) == source_id]
    rows.sort(key=lambda row: (int(row.get("source_id", 0)), -int(row.get("version", 0))))
    return [_deep(row) for row in rows[:limit]]


def get_scan(scan_id: int) -> dict[str, Any] | None:
    scan = store.find(SCAN_TABLE, scan_id)
    return _deep(scan) if scan else None


# ---------------------------------------------------------------------------
# 字段别名版本
# ---------------------------------------------------------------------------

def _alias_rows(model_id: int, version: int | None = None) -> list[dict[str, Any]]:
    rows = [
        row for row in store.rows(ALIAS_TABLE)
        if _int(row.get("model_id")) == model_id
        and (version is None or _int(row.get("alias_version")) == version)
    ]
    rows.sort(key=lambda row: (int(row.get("alias_version", 0)), _int(row.get("source_id")), _text(row.get("alias"))))
    return rows


def current_alias_version(model_id: int) -> int:
    return max((int(row.get("alias_version", 0)) for row in _alias_rows(model_id)), default=0)


def _store_alias_version(
    model_id: int,
    entries: list[dict[str, Any]],
    confirmed_by: str,
    note: str,
) -> int:
    """追加一个别名版本；旧版本只改状态，映射内容原样保留。"""
    version = current_alias_version(model_id) + 1
    for row in store.rows(ALIAS_TABLE):
        if _int(row.get("model_id")) == model_id and row.get("status") == SCAN_EFFECTIVE:
            row["status"] = SCAN_HISTORY
    for entry in entries:
        source_id = _int(entry.get("source_id"))
        store.rows(ALIAS_TABLE).append({
            "id": _next_id(ALIAS_TABLE),
            "model_id": model_id,
            "source_id": source_id,
            "source_no": _source_no(source_id),
            "alias_version": version,
            "field_id": _text(entry.get("field_id")),
            "alias": _text(entry.get("alias")),
            "physical_column": _text(entry.get("physical_column") or entry.get("alias")),
            "status": SCAN_EFFECTIVE,
            "confirmed_by": confirmed_by,
            "note": note,
            "created_at": _now(),
        })
    return version


def _find_field_any_scan(source_id: int, physical: str) -> dict[str, Any] | None:
    """在所有历史快照里按物理列回找字段标识，优先最新的版本。

    别名初始化时目标快照可能已经缺了这一列，此时仍应绑定回它原来的字段标识，
    否则缺列会被误记成一个从没见过的 ``ALIAS-*`` 字段。
    """
    rows = sorted(
        (row for row in store.rows(SCAN_TABLE) if _int(row.get("source_id")) == source_id),
        key=lambda row: int(row.get("version", 0)),
        reverse=True,
    )
    for scan in rows:
        matched = _field_by_name(scan.get("fields", []), physical)
        if matched is not None:
            return matched
    return None


def bootstrap_alias_version(model: dict[str, Any]) -> int:
    """首次使用时按模型的「字段别名」生成 v1 映射，并绑定到来源快照的字段标识。"""
    with _LOCK:
        existing = current_alias_version(int(model["id"]))
        if existing:
            return existing
        refs = _source_refs(model) or [0]
        entries: list[dict[str, Any]] = []
        for source_id in refs:
            ensure_baseline_scan(source_id) if source_id else None
            for token in _alias_tokens(model, source_id):
                matched = _find_field_any_scan(source_id, token["physical_column"]) if source_id else None
                if matched is not None:
                    field_id = _text(matched.get("field_id"))
                    physical = _text(matched.get("physical_column") or matched.get("name"))
                else:
                    field_id = f"ALIAS-{source_id}-{token['physical_column']}"
                    physical = token["physical_column"]
                entries.append({
                    "source_id": source_id,
                    "field_id": field_id,
                    "alias": token["alias"],
                    "physical_column": physical,
                })
        return _store_alias_version(int(model["id"]), entries, "system", "模型字段别名初始化")


def save_alias_version(
    model_id: int,
    entries: list[dict[str, Any]],
    confirmed_by: str = "validator",
    note: str = "",
) -> dict[str, Any]:
    """显式追加一个别名版本。

    ``entries`` 是这次要改动的条目，其余映射从当前版本原样带入，因此每个版本都是
    一份完整映射快照，任何一版都能单独还原。条目可带 source_id / field_id /
    alias / physical_column。
    """
    with _LOCK:
        model = store.find("semantic_model", model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}
        if not entries:
            return {"ok": False, "message": "别名映射不能为空"}
        refs = _source_refs(model)
        current = _alias_rows(model_id, current_alias_version(model_id))
        merged: dict[tuple[int, str], dict[str, Any]] = {
            (_int(row.get("source_id")), _text(row.get("alias"))): {
                "source_id": _int(row.get("source_id")),
                "field_id": _text(row.get("field_id")),
                "alias": _text(row.get("alias")),
                "physical_column": _text(row.get("physical_column")),
            }
            for row in current
        }

        for entry in entries:
            source_id = _int(entry.get("source_id"), refs[0] if refs else 0)
            scan = _latest_scan(source_id) if source_id else None
            snapshot = scan.get("fields", []) if scan else []
            field_id = _text(entry.get("field_id"))
            physical = _text(entry.get("physical_column") or entry.get("physical") or entry.get("alias"))
            alias = _text(entry.get("alias") or physical)
            if not field_id:
                matched = _field_by_name(snapshot, physical)
                field_id = _text(matched.get("field_id")) if matched else f"ALIAS-{source_id}-{physical}"

            # 版本内按「别名」唯一：同一来源下别名不能同时指向两列，但两个别名
            # 可以指向同一物理列（字段标识允许共享）。
            merged[(source_id, alias)] = {
                "source_id": source_id,
                "field_id": field_id,
                "alias": alias,
                "physical_column": physical,
            }

        ordered = sorted(merged.values(), key=lambda row: (row["source_id"], row["alias"]))
        version = _store_alias_version(model_id, ordered, confirmed_by, note)
        # 模型列表展示的「字段别名」跟随当前版本；历史版本仍完整保存在 field_alias。
        model["字段别名"] = ",".join(row["alias"] for row in ordered)
        return {
            "ok": True,
            "model_id": model_id,
            "alias_version": version,
            "entries": [_deep(row) for row in _alias_rows(model_id, version)],
        }


def list_alias_versions(model_id: int) -> dict[str, Any]:
    rows = _alias_rows(model_id)
    versions: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        versions.setdefault(int(row.get("alias_version", 0)), []).append(_deep(row))
    return {
        "model_id": model_id,
        "current_version": current_alias_version(model_id),
        "versions": [
            {"alias_version": version, "entries": entries}
            for version, entries in sorted(versions.items())
        ],
    }


# ---------------------------------------------------------------------------
# 差异批次
# ---------------------------------------------------------------------------

def _measure_expressions(model: dict[str, Any]) -> list[str]:
    return _split_list(model.get("度量定义"))


def _measure_token_map(model: dict[str, Any]) -> dict[str, list[str]]:
    """measure expression -> 引用到的别名/物理列标识符。"""
    token_map: dict[str, list[str]] = {}
    for measure in _measure_expressions(model):
        tokens = [
            token for token in re.findall(r"[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*", measure)
            if token.casefold() not in _SQL_WORDS
        ]
        token_map[measure] = tokens
    return token_map


def _measures_for(alias: str, physical: str, model: dict[str, Any]) -> list[str]:
    """找出引用了该别名（或它指向的物理列）的度量表达式。"""
    candidates = {_fold(alias), _fold(physical)} - {""}
    matched: list[str] = []
    for measure, tokens in _measure_token_map(model).items():
        if any(_fold(token) in candidates for token in tokens):
            matched.append(measure)
    return matched


def _make_entry(
    batch: dict[str, Any],
    *,
    source_id: int,
    field_id: str,
    change_type: str,
    field_name: str = "",
    physical_column: str = "",
    baseline_column: str = "",
    alias: str = "",
    blocking: bool = False,
    blocking_measures: list[str] | None = None,
    resolution: str = RESOLUTION_PENDING,
    resolution_note: str = "",
) -> dict[str, Any]:
    return {
        # id 在真正入列表时分配，避免差异去重时留下空洞编号。
        "id": 0,
        "diff_batch_id": batch["id"],
        "diff_batch_no": batch["batch_no"],
        "source_id": source_id,
        "source_no": _source_no(source_id),
        "field_id": field_id,
        "field_name": field_name,
        "physical_column": physical_column,
        "baseline_column": baseline_column,
        "alias": alias,
        "change_type": change_type,
        "blocking": blocking,
        "blocking_measures": list(blocking_measures or []),
        "resolution": resolution,
        "resolution_note": resolution_note,
        "reviewer": "system" if resolution in RESOLVED_STATES else "",
        "resolved_at": _now() if resolution in RESOLVED_STATES else "",
        "auto_resolved": resolution in {AUTO_ALIAS_KEPT, AUTO_NAME_MATCHED, AUTO_IGNORED},
        "校验结论": "",
        "关联校验编号": "",
        "created_at": _now(),
    }


def _build_entries(
    model: dict[str, Any],
    batch: dict[str, Any],
    sources: list[int],
    baseline_scans: dict[int, dict[str, Any] | None],
    target_scans: dict[int, dict[str, Any]],
    aliases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    index: dict[tuple[int, str, str], dict[str, Any]] = {}

    def add(entry: dict[str, Any]) -> dict[str, Any]:
        key = (int(entry["source_id"]), entry["field_id"], entry["change_type"])
        if key in index:
            existing = index[key]
            # 差异明细按 (来源, 字段标识, 类型) 唯一；不同来源的同名列天然分开。
            if entry.get("alias") and not existing.get("alias"):
                existing["alias"] = entry["alias"]
            if entry.get("baseline_column") and not existing.get("baseline_column"):
                existing["baseline_column"] = entry["baseline_column"]
            if entry.get("blocking"):
                existing["blocking"] = True
                existing["blocking_measures"] = sorted(set(
                    existing.get("blocking_measures", []) + entry.get("blocking_measures", [])
                ))
            return existing
        index[key] = entry
        entries.append(entry)
        return entry

    # 1) 快照层面的新增 / 删除 / 改名
    for source_id in sources:
        baseline = baseline_scans.get(source_id)
        target = target_scans.get(source_id)
        baseline_map = _snapshot_map(baseline)
        target_map = _snapshot_map(target)
        alias_by_field = {
            (int(row.get("source_id")), _text(row.get("field_id"))): row
            for row in aliases
        }

        for field_id, field in target_map.items():
            if field_id not in baseline_map:
                add(_make_entry(
                    batch,
                    source_id=source_id,
                    field_id=field_id,
                    change_type=CHANGE_ADDED,
                    field_name=_text(field.get("name")),
                    physical_column=_text(field.get("physical_column") or field.get("name")),
                    alias=_text((alias_by_field.get((source_id, field_id)) or {}).get("alias")),
                ))
            else:
                before = baseline_map[field_id]
                before_physical = _text(before.get("physical_column") or before.get("name"))
                after_physical = _text(field.get("physical_column") or field.get("name"))
                if before_physical != after_physical or _text(before.get("name")) != _text(field.get("name")):
                    alias_row = alias_by_field.get((source_id, field_id))
                    add(_make_entry(
                        batch,
                        source_id=source_id,
                        field_id=field_id,
                        change_type=CHANGE_RENAMED,
                        field_name=_text(field.get("name")),
                        physical_column=after_physical,
                        baseline_column=before_physical,
                        alias=_text((alias_row or {}).get("alias")),
                        # 字段标识还在，说明只是改名，别名仍指向同一物理列。
                        resolution=RESOLUTION_RENAMED if not alias_row else AUTO_ALIAS_KEPT,
                        resolution_note="字段标识未变，别名仍指向同一物理列" if alias_row else "",
                    ))

        for field_id, field in baseline_map.items():
            if field_id not in target_map:
                add(_make_entry(
                    batch,
                    source_id=source_id,
                    field_id=field_id,
                    change_type=CHANGE_REMOVED,
                    field_name=_text(field.get("name")),
                    physical_column=_text(field.get("physical_column") or field.get("name")),
                    baseline_column=_text(field.get("physical_column") or field.get("name")),
                ))

    # 2) 别名层面的缺列 / 改名 / 同名列沿用
    for alias_row in aliases:
        source_id = _int(alias_row.get("source_id"))
        if source_id not in target_scans:
            continue
        target_fields = target_scans[source_id].get("fields", [])
        field_id = _text(alias_row.get("field_id"))
        alias = _text(alias_row.get("alias"))
        physical = _text(alias_row.get("physical_column"))
        target_field = _field_by_id(target_fields, field_id)

        if target_field is not None:
            target_physical = _text(target_field.get("physical_column") or target_field.get("name"))
            if target_physical != physical:
                add(_make_entry(
                    batch,
                    source_id=source_id,
                    field_id=field_id,
                    change_type=CHANGE_RENAMED,
                    field_name=_text(target_field.get("name")),
                    physical_column=target_physical,
                    baseline_column=physical,
                    alias=alias,
                    resolution=AUTO_ALIAS_KEPT,
                    resolution_note="字段标识未变，别名仍指向同一物理列",
                ))
            continue

        # 字段标识找不到时，再看同一来源里是否还有同名物理列：有则别名仍然有效。
        same_name = _field_by_name(target_fields, physical)
        if same_name is not None:
            add(_make_entry(
                batch,
                source_id=source_id,
                field_id=_text(same_name.get("field_id")),
                change_type=CHANGE_ALIAS_KEPT,
                field_name=_text(same_name.get("name")),
                physical_column=_text(same_name.get("physical_column") or same_name.get("name")),
                baseline_column=physical,
                alias=alias,
                resolution=AUTO_NAME_MATCHED,
                resolution_note="同一来源仍存在该物理列，别名继续沿用",
            ))
            continue

        measures = _measures_for(alias, physical, model)
        # 快照层已经记过这条删除时，不再重复造一条缺列；把阻断信息挂到删除条目上，
        # 让「删除/改名/新增」与「缺列」各自只有一条口径。
        removed = index.get((source_id, field_id, CHANGE_REMOVED))
        if removed is not None:
            removed["alias"] = removed.get("alias") or alias
            removed["resolution_note"] = "字段标识已从来源快照消失，别名无对应物理列"
            if measures:
                removed["blocking"] = True
                removed["blocking_measures"] = sorted(set(removed.get("blocking_measures", []) + measures))
            continue

        add(_make_entry(
            batch,
            source_id=source_id,
            field_id=field_id,
            change_type=CHANGE_MISSING,
            field_name=physical,
            physical_column=physical,
            baseline_column=physical,
            alias=alias,
            blocking=bool(measures),
            blocking_measures=measures,
            resolution=RESOLUTION_PENDING,
        ))

    next_id = _next_id(ENTRY_TABLE)
    for entry in entries:
        entry["id"] = next_id
        next_id += 1
    return entries


def create_diff_batch(
    model_id: int,
    source_id: int | None = None,
    baseline_scan_id: int | None = None,
    target_scan_id: int | None = None,
    alias_version: int | None = None,
    requested_by: str = "validator",
) -> dict[str, Any]:
    """生成一个版本化差异批次；完整输入会随批次冻结，供之后还原当次输入。"""
    with _LOCK:
        model = store.find("semantic_model", model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        sources = [source_id] if source_id is not None else _source_refs(model)
        if not sources:
            return {"ok": False, "message": "语义模型未绑定来源表，无法生成字段变更对照"}

        if alias_version is None:
            alias_version = bootstrap_alias_version(model)
        aliases = [_deep(row) for row in _alias_rows(model_id, alias_version)]
        if not aliases:
            return {"ok": False, "message": f"别名版本 v{alias_version} 不存在"}

        baseline_scans: dict[int, dict[str, Any] | None] = {}
        target_scans: dict[int, dict[str, Any]] = {}
        for sid in sources:
            target = store.find(SCAN_TABLE, target_scan_id) if target_scan_id else _latest_scan(sid)
            if target is None or _int(target.get("source_id")) != sid:
                target = ensure_baseline_scan(sid)
            if target is None:
                return {"ok": False, "message": f"来源 {sid} 没有可用的字段快照"}
            target_scans[sid] = _deep(target)

            baseline: dict[str, Any] | None = None
            if baseline_scan_id:
                candidate = store.find(SCAN_TABLE, baseline_scan_id)
                if candidate is not None and _int(candidate.get("source_id")) == sid:
                    baseline = _deep(candidate)
            if baseline is None:
                previous = [
                    _deep(row) for row in store.rows(SCAN_TABLE)
                    if _int(row.get("source_id")) == sid
                    and int(row.get("version", 0)) < int(target.get("version", 0))
                ]
                if previous:
                    baseline = max(previous, key=lambda row: int(row.get("version", 0)))
            baseline_scans[sid] = baseline

        input_payload = {
            "model_id": model_id,
            "model_no": _text(model.get("模型编号")),
            "sources": sources,
            "alias_version": int(alias_version),
            "aliases": [
                {key: row.get(key) for key in ("source_id", "field_id", "alias", "physical_column")}
                for row in aliases
            ],
            "baseline_scans": {
                str(sid): None if scan is None else {
                    "scan_batch_id": scan.get("scan_batch_id"),
                    "batch_no": scan.get("batch_no"),
                    "version": scan.get("version"),
                    "input_digest": scan.get("input_digest"),
                    "fields": scan.get("fields", []),
                }
                for sid, scan in baseline_scans.items()
            },
            "target_scans": {
                str(sid): {
                    "scan_batch_id": scan.get("scan_batch_id"),
                    "batch_no": scan.get("batch_no"),
                    "version": scan.get("version"),
                    "input_digest": scan.get("input_digest"),
                    "fields": scan.get("fields", []),
                }
                for sid, scan in target_scans.items()
            },
        }
        input_digest = _digest(input_payload)

        # 同一模型、同一输入重复生成时复用原批次；已确认批次不会被重算覆盖。
        for row in store.rows(DIFF_TABLE):
            if _int(row.get("model_id")) == model_id and row.get("input_digest") == input_digest:
                return {
                    "ok": True,
                    "deduplicated": True,
                    "batch": _deep(row),
                    "entries": [_deep(item) for item in _entries_for_batch(int(row["id"]))],
                }

        batch_id = _next_id(DIFF_TABLE)
        batch = {
            "id": batch_id,
            "batch_no": f"DIFF-{model_id:04d}-{batch_id:04d}",
            "model_id": model_id,
            "model_no": _text(model.get("模型编号")),
            "model_name": _text(model.get("模型名称")),
            "source_ids": sources,
            "baseline_scan_ids": [
                int(scan["id"]) for scan in baseline_scans.values() if scan is not None
            ],
            "target_scan_ids": [int(scan["id"]) for scan in target_scans.values()],
            "alias_version": int(alias_version),
            "input_digest": input_digest,
            "input_snapshot": _deep(input_payload),
            "requested_by": requested_by,
            "created_at": _now(),
            "status": DIFF_PENDING,
            "pending_count": 0,
            "confirmed_count": 0,
            "confirmed_at": "",
            "confirmed_by": "",
            "last_validation_no": "",
            "last_validation_result": "",
        }

        entries = _build_entries(
            model, batch, sources, baseline_scans, target_scans, aliases,
        )
        batch["pending_count"] = sum(1 for entry in entries if entry["resolution"] == RESOLUTION_PENDING)
        batch["confirmed_count"] = len(entries) - batch["pending_count"]
        if batch["pending_count"] == 0:
            batch["status"] = DIFF_CONFIRMED
            batch["confirmed_at"] = _now()
            batch["confirmed_by"] = requested_by

        store.rows(DIFF_TABLE).append(batch)
        store.rows(ENTRY_TABLE).extend(_deep(entries))
        return {
            "ok": True,
            "deduplicated": False,
            "batch": _deep(batch),
            "entries": _deep(entries),
        }


def _entries_for_batch(batch_id: int) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(ENTRY_TABLE) if _int(row.get("diff_batch_id")) == batch_id]
    rows.sort(key=lambda row: _int(row.get("id")))
    return rows


def get_diff_batch(batch_id: int, include_entries: bool = True) -> dict[str, Any] | None:
    batch = store.find(DIFF_TABLE, batch_id)
    if batch is None:
        return None
    result = _deep(batch)
    if include_entries:
        result["entries"] = [_deep(row) for row in _entries_for_batch(batch_id)]
        # 明确分开展示，避免调用方把待确认项误当已确认批次的一部分。
        result["pending_entries"] = [row for row in result["entries"] if row["resolution"] == RESOLUTION_PENDING]
        result["confirmed_entries"] = [row for row in result["entries"] if row["resolution"] in RESOLVED_STATES]
    return result


def list_diff_batches(
    model_id: int | None = None,
    status: str | None = None,
    source_id: int | None = None,
) -> list[dict[str, Any]]:
    rows = list(store.rows(DIFF_TABLE))
    if model_id is not None:
        rows = [row for row in rows if _int(row.get("model_id")) == model_id]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    if source_id is not None:
        rows = [row for row in rows if source_id in [int(s) for s in row.get("source_ids", [])]]
    rows.sort(key=lambda row: _int(row.get("id")), reverse=True)
    return [_deep(row) for row in rows]


def list_diff_entries(batch_id: int, resolution: str | None = None) -> list[dict[str, Any]]:
    rows = _entries_for_batch(batch_id)
    if resolution:
        rows = [row for row in rows if row.get("resolution") == resolution]
    return [_deep(row) for row in rows]


def _refresh_batch_status(batch: dict[str, Any], reviewer: str) -> None:
    entries = _entries_for_batch(int(batch["id"]))
    pending = [row for row in entries if row.get("resolution") == RESOLUTION_PENDING]
    batch["pending_count"] = len(pending)
    batch["confirmed_count"] = len(entries) - len(pending)
    if not pending and entries:
        batch["status"] = DIFF_CONFIRMED
        batch["confirmed_at"] = _now()
        batch["confirmed_by"] = reviewer
    else:
        batch["status"] = DIFF_PENDING


def confirm_diff_entry(
    entry_id: int,
    resolution: str,
    reviewer: str = "validator",
    note: str = "",
    alias: str | None = None,
    physical_column: str | None = None,
) -> dict[str, Any]:
    """确认一条差异；已确认批次不可改写，更正别名会追加新的别名版本。"""
    with _LOCK:
        entry = store.find(ENTRY_TABLE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"差异明细 {entry_id} 不存在"}
        batch = store.find(DIFF_TABLE, _int(entry.get("diff_batch_id")))
        if batch is None:
            return {"ok": False, "message": "差异批次不存在"}
        if batch.get("status") == DIFF_CONFIRMED:
            return {
                "ok": False,
                "message": "该差异批次已确认，历史口径不可改写；请重新生成对照批次",
                "batch_id": batch["id"],
            }
        if resolution not in {
            RESOLUTION_MISSING, RESOLUTION_RENAMED, RESOLUTION_ALIAS_FIXED,
            RESOLUTION_NEW_FIELD, RESOLUTION_IGNORED, RESOLUTION_KEPT,
        }:
            return {"ok": False, "message": f"不支持的处理结论：{resolution}"}

        new_alias_version = None
        if resolution == RESOLUTION_ALIAS_FIXED:
            model = store.find("semantic_model", _int(batch.get("model_id")))
            if model is None:
                return {"ok": False, "message": "语义模型不存在"}
            physical = _text(physical_column or entry.get("physical_column"))
            corrected_alias = _text(alias or entry.get("alias") or physical)
            latest = _latest_scan(_int(entry.get("source_id")))
            matched = _field_by_name(latest.get("fields", []), physical) if latest else None
            entries = [
                {
                    "source_id": int(entry.get("source_id")),
                    "field_id": _text(entry.get("field_id")),
                    "alias": corrected_alias,
                    "physical_column": physical,
                }
            ]
            if matched is not None and _text(matched.get("field_id")):
                entries[0]["field_id"] = _text(matched.get("field_id"))
            saved = save_alias_version(
                int(model["id"]), entries, confirmed_by=reviewer,
                note=note or f"差异明细 {entry_id} 复核更正别名",
            )
            if not saved.get("ok"):
                return saved
            new_alias_version = saved["alias_version"]
            entry["field_id"] = entries[0]["field_id"]
            entry["physical_column"] = physical
            entry["alias"] = corrected_alias

        entry["resolution"] = resolution
        entry["resolution_note"] = note
        entry["reviewer"] = reviewer
        entry["resolved_at"] = _now()
        entry["auto_resolved"] = False
        if new_alias_version is not None:
            entry["更正别名版本"] = new_alias_version
        # 复核后该条不再是阻断项，但原差异类型和当次输入保持不动。
        if resolution in RESOLVED_STATES:
            entry["blocking"] = False

        _refresh_batch_status(batch, reviewer)
        return {
            "ok": True,
            "entry": _deep(entry),
            "batch": _deep(batch),
            "alias_version": new_alias_version,
        }


def confirm_diff_batch(batch_id: int, reviewer: str = "validator") -> dict[str, Any]:
    """整批确认；只要还有一条待确认差异就不允许确认，避免混批。"""
    with _LOCK:
        batch = store.find(DIFF_TABLE, batch_id)
        if batch is None:
            return {"ok": False, "message": "差异批次不存在"}
        if batch.get("status") == DIFF_CONFIRMED:
            return {"ok": True, "already_confirmed": True, "batch": _deep(batch)}
        pending = [row for row in _entries_for_batch(batch_id) if row.get("resolution") == RESOLUTION_PENDING]
        if pending:
            return {
                "ok": False,
                "message": f"仍有 {len(pending)} 条待确认差异，不能并入已确认批次",
                "pending_entries": [_deep(row) for row in pending],
            }
        batch["status"] = DIFF_CONFIRMED
        batch["confirmed_at"] = _now()
        batch["confirmed_by"] = reviewer
        batch["pending_count"] = 0
        batch["confirmed_count"] = len(_entries_for_batch(batch_id))
        return {"ok": True, "batch": _deep(batch)}


# ---------------------------------------------------------------------------
# 投产校验
# ---------------------------------------------------------------------------

def _missing_aliases(
    aliases: list[dict[str, Any]],
    target_scans: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for row in aliases:
        source_id = _int(row.get("source_id"))
        scan = target_scans.get(source_id)
        if scan is None:
            continue
        fields = scan.get("fields", [])
        field_id = _text(row.get("field_id"))
        physical = _text(row.get("physical_column"))
        if _field_by_id(fields, field_id) is not None:
            continue
        if _field_by_name(fields, physical) is not None:
            continue
        missing.append(_deep(row))
    return missing


def _measure_status(
    model: dict[str, Any],
    aliases: list[dict[str, Any]],
    target_scans: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    missing = _missing_aliases(aliases, target_scans)
    missing_keys = {
        (int(row.get("source_id")), _text(row.get("field_id")), _fold(row.get("physical_column")))
        for row in missing
    }
    results: list[dict[str, Any]] = []
    for measure, tokens in _measure_token_map(model).items():
        folded_tokens = {_fold(token) for token in tokens}
        blocked_by: list[dict[str, Any]] = []
        for alias_row in aliases:
            if not (folded_tokens & {_fold(alias_row.get("alias")), _fold(alias_row.get("physical_column"))}):
                continue
            key = (
                int(alias_row.get("source_id")),
                _text(alias_row.get("field_id")),
                _fold(alias_row.get("physical_column")),
            )
            if key in missing_keys:
                blocked_by.append({
                    "source_id": key[0],
                    "field_id": key[1],
                    "alias": _text(alias_row.get("alias")),
                    "physical_column": _text(alias_row.get("physical_column")),
                })
        results.append({
            "measure": measure,
            "status": "阻断" if blocked_by else "可投产",
            "blocked_by": blocked_by,
            "aliases": sorted({
                _text(row.get("alias")) for row in aliases
                if folded_tokens & {_fold(row.get("alias")), _fold(row.get("physical_column"))}
            }),
        })
    return results


def start_promotion(model_id: int, requested_by: str = "model_owner") -> dict[str, Any]:
    """投产校验第一阶段：读取并冻结差异明细、扫描快照和别名版本。

    冻结之后复核人再更正别名，也不会改变本次校验读取到的事实；重新查询投产记录
    会同时给出「当次冻结输入下的结论」与「当前别名下的度量可投产性」。
    """
    with _LOCK:
        model = store.find("semantic_model", model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        # 只复用与当前状态一致的批次：别名在上一轮校验之后被更正、或来源又出了新
        # 快照时，必须按新输入重新生成批次，否则复投永远套用旧的别名版本。
        alias_version_now = current_alias_version(model_id) or bootstrap_alias_version(model)
        latest_ids = sorted(
            int(scan["id"]) for sid in (_source_refs(model) or [])
            for scan in [_latest_scan(sid)] if scan is not None
        )
        batch = None
        candidates = [row for row in store.rows(DIFF_TABLE) if _int(row.get("model_id")) == model_id]
        if candidates:
            newest = max(candidates, key=lambda row: _int(row.get("id")))
            if (
                _int(newest.get("alias_version")) == alias_version_now
                and sorted(int(sid) for sid in newest.get("target_scan_ids", [])) == latest_ids
            ):
                batch = newest
        if batch is None:
            created = create_diff_batch(
                model_id, alias_version=alias_version_now, requested_by=requested_by,
            )
            if not created.get("ok"):
                return created
            batch = store.find(DIFF_TABLE, int(created["batch"]["id"]))
        if batch is None:
            return {"ok": False, "message": "无法生成字段变更对照批次"}

        alias_version = int(batch.get("alias_version", 0))
        aliases = [_deep(row) for row in _alias_rows(model_id, alias_version)]
        entries = [_deep(row) for row in _entries_for_batch(int(batch["id"]))]

        # 按批次冻结的扫描 id 还原当次输入，而不是重新读最新快照：
        # 复核期间即使再扫描出新版本，这次投产看到的仍然是同一批输入。
        target_scans: dict[int, dict[str, Any]] = {}
        for scan_id in batch.get("target_scan_ids", []):
            scan = get_scan(int(scan_id))
            if scan is not None:
                target_scans[int(scan["source_id"])] = scan
        if not target_scans:
            for sid in batch.get("source_ids", []):
                live = _latest_scan(int(sid))
                if live is not None:
                    target_scans[int(sid)] = _deep(live)

        pinned = {
            "model_id": model_id,
            "diff_batch_id": int(batch["id"]),
            "diff_batch_no": batch.get("batch_no"),
            "input_digest": batch.get("input_digest"),
            "baseline_scan_ids": list(batch.get("baseline_scan_ids", [])),
            "target_scan_ids": list(batch.get("target_scan_ids", [])),
            "alias_version": alias_version,
            "aliases": aliases,
            "target_scans": target_scans,
            "entries": [
                {
                    key: entry.get(key) for key in (
                        "id", "source_id", "field_id", "field_name", "physical_column",
                        "baseline_column", "alias", "change_type", "blocking",
                        "blocking_measures", "resolution",
                    )
                }
                for entry in entries
            ],
        }
        measures = _measure_status(model, aliases, target_scans)
        missing_fields = _missing_aliases(aliases, target_scans)

        promotion_id = _next_id(PROMOTION_TABLE)
        validation_no = f"FVAL-{model_id:04d}-{promotion_id:04d}"
        promotion = {
            "id": promotion_id,
            "promotion_no": f"PROMO-{model_id:04d}-{promotion_id:04d}",
            "model_id": model_id,
            "model_no": _text(model.get("模型编号")),
            "model_name": _text(model.get("模型名称")),
            "status": "校验中",
            "result": "",
            "conclusion": "",
            "requested_by": requested_by,
            "started_at": _now(),
            "concluded_at": "",
            "diff_batch_id": int(batch["id"]),
            "diff_batch_no": batch.get("batch_no"),
            "diff_status": batch.get("status"),
            "baseline_scan_ids": list(batch.get("baseline_scan_ids", [])),
            "target_scan_ids": list(batch.get("target_scan_ids", [])),
            "scan_batch_ids": list(batch.get("target_scan_ids", [])),
            "alias_version": alias_version,
            "input_digest": batch.get("input_digest"),
            "validation_no": validation_no,
            "missing_field_ids": [_text(row.get("field_id")) for row in missing_fields],
            "missing_fields": _deep(missing_fields),
            "measures": measures,
            "promotable": all(item["status"] == "可投产" for item in measures),
            "pinned": _deep(pinned),
        }
        store.rows(PROMOTION_TABLE).append(promotion)

        validation = {
            "id": _next_id(VALIDATION_TABLE),
            "validation_no": validation_no,
            "model_id": model_id,
            "model_no": _text(model.get("模型编号")),
            "promotion_id": promotion_id,
            "diff_batch_id": int(batch["id"]),
            "diff_batch_no": batch.get("batch_no"),
            "baseline_scan_ids": list(batch.get("baseline_scan_ids", [])),
            "target_scan_ids": list(batch.get("target_scan_ids", [])),
            "alias_version": alias_version,
            "input_digest": batch.get("input_digest"),
            "entries": _deep(pinned["entries"]),
            "measures": _deep(measures),
            "missing_fields": _deep(missing_fields),
            "result": "",
            "conclusion": "",
            "created_at": _now(),
            "status": "读取完成",
        }
        store.rows(VALIDATION_TABLE).append(validation)
        promotion["validation_id"] = validation["id"]

        return {
            "ok": True,
            "promotion": _deep(promotion),
            "validation": _deep(validation),
            "pinned_input": _deep(pinned),
        }


def finish_promotion(promotion_id: int, concluded_by: str = "model_owner") -> dict[str, Any]:
    """投产校验第二阶段：按冻结输入写结论，并把结论回写到差异清单。"""
    with _LOCK:
        promotion = store.find(PROMOTION_TABLE, promotion_id)
        if promotion is None:
            return {"ok": False, "message": "投产记录不存在"}
        if promotion.get("status") == "已完成":
            return {
                "ok": True,
                "already_finished": True,
                "promotion": _deep(promotion),
                # 重新查询口径：冻结输入 + 当前别名下的对照结果。
                "promotion_detail": get_promotion(promotion_id),
            }

        validation = store.find(VALIDATION_TABLE, _int(promotion.get("validation_id")))
        model = store.find("semantic_model", _int(promotion.get("model_id")))
        if validation is None or model is None:
            return {"ok": False, "message": "投产校验记录或语义模型不存在"}

        measures = _deep(validation.get("measures", []))
        missing_fields = _deep(validation.get("missing_fields", []))
        blocking_measures = [item for item in measures if item["status"] == "阻断"]
        blocking_entries = [
            entry for entry in validation.get("entries", [])
            if entry.get("blocking") and entry.get("change_type") in (CHANGE_MISSING, CHANGE_REMOVED)
        ]
        promotable = not blocking_measures and not blocking_entries

        if promotable:
            conclusion = "字段变更校验通过，模型可随本版投产"
            result = "通过"
            model["status"] = "已投产"
            model["pending"] = False
            model["abnormal"] = False
            _update_contract_on_promotion(model)
            _audit_promotion(model, _text(promotion.get("promotion_no")), "PROMOTE")
        else:
            names = "、".join(
                f"{item.get('alias') or item.get('field_id')}" for item in missing_fields
            ) or "字段"
            measure_names = "、".join(item["measure"] for item in blocking_measures)
            conclusion = f"字段变更校验阻断：缺列 {names}"
            if measure_names:
                conclusion += f"，度量 {measure_names} 无法随本版投产"
            result = "阻断"
            model["status"] = "校验中"
            model["pending"] = True
            model["abnormal"] = True
            _audit_promotion(model, _text(promotion.get("promotion_no")), "BLOCK")

        promotion["status"] = "已完成"
        promotion["result"] = result
        promotion["conclusion"] = conclusion
        promotion["concluded_at"] = _now()
        promotion["concluded_by"] = concluded_by
        promotion["promotable"] = promotable
        promotion["blocking_measures"] = [
            item["measure"] for item in blocking_measures
        ]
        validation["result"] = result
        validation["conclusion"] = conclusion
        validation["status"] = "已完成"

        # 结论回写到差异清单；只写校验结论，不改动原差异类型与处理结论。
        batch = store.find(DIFF_TABLE, _int(promotion.get("diff_batch_id")))
        entry_conclusion = conclusion if not promotable else "本版投产校验通过"
        for entry in _entries_for_batch(_int(promotion.get("diff_batch_id"))):
            entry["校验结论"] = entry_conclusion
            entry["关联校验编号"] = validation.get("validation_no", "")
        if batch is not None:
            batch["last_validation_no"] = validation.get("validation_no", "")
            batch["last_validation_result"] = result

        return {
            "ok": promotable,
            "message": conclusion,
            "promotion": _deep(promotion),
            "validation": _deep(validation),
            "promotion_detail": get_promotion(promotion_id),
        }


def promote_model(model_id: int, requested_by: str = "model_owner") -> dict[str, Any]:
    """一次完成「读取差异 -> 写校验记录 -> 回写结论」的投产校验。"""
    started = start_promotion(model_id, requested_by=requested_by)
    if not started.get("ok"):
        return started
    promotion_id = int(started["promotion"]["id"])
    return finish_promotion(promotion_id, concluded_by=requested_by)


def get_promotion(promotion_id: int) -> dict[str, Any]:
    """重新查询本次投产：还原当次扫描/映射，并对照当前别名下的度量可投产性。"""
    with _LOCK:
        promotion = store.find(PROMOTION_TABLE, promotion_id)
        if promotion is None:
            return {"ok": False, "message": "投产记录不存在"}
        result = _deep(promotion)
        result["ok"] = True
        validation = store.find(VALIDATION_TABLE, _int(promotion.get("validation_id")))
        result["validation"] = _deep(validation) if validation else None

        pinned = _deep(promotion.get("pinned", {}))
        result["pinned"] = pinned
        result["scans"] = [
            scan for scan in (get_scan(int(sid)) for sid in promotion.get("target_scan_ids", []))
            if scan is not None
        ]
        result["baseline_scans"] = [
            scan for scan in (get_scan(int(sid)) for sid in promotion.get("baseline_scan_ids", []))
            if scan is not None
        ]
        alias_version = int(promotion.get("alias_version", 0))
        result["alias_snapshot"] = [_deep(row) for row in _alias_rows(int(promotion["model_id"]), alias_version)]

        current_version = current_alias_version(int(promotion["model_id"]))
        current_aliases = [_deep(row) for row in _alias_rows(int(promotion["model_id"]), current_version)]
        target_scans = {
            int(key): value for key, value in pinned.get("target_scans", {}).items()
        }
        if not target_scans:
            for sid in promotion.get("target_scan_ids", []):
                scan = get_scan(int(sid))
                if scan:
                    target_scans[int(scan["source_id"])] = scan
        model = store.find("semantic_model", int(promotion["model_id"]))
        current_measures = _measure_status(model, current_aliases, target_scans) if model else []
        current_missing = _missing_aliases(current_aliases, target_scans)

        result["current_alias_version"] = current_version
        result["current_aliases"] = current_aliases
        result["alias_changed_since_validation"] = current_version != alias_version
        result["current_measures"] = current_measures
        result["current_missing_fields"] = current_missing
        result["can_repromote"] = all(item["status"] == "可投产" for item in current_measures)
        result["pinned_measures"] = _deep(promotion.get("measures", []))

        # 从本次投产按字段标识查回上一轮已确认的缺列。
        history: dict[str, list[dict[str, Any]]] = {}
        for field_id in promotion.get("missing_field_ids", []):
            found = lookup_confirmed_missing(_text(field_id), model_id=int(promotion["model_id"]))
            if found:
                history[_text(field_id)] = found
        result["previous_confirmed_missing"] = history

        if result.get("status") == "已完成":
            if promotion.get("result") == "通过":
                summary = "本版投产校验通过"
            elif result["alias_changed_since_validation"] and result["can_repromote"]:
                summary = (
                    f"本次投产按 v{alias_version} 冻结的结论为「{promotion.get('conclusion')}」；"
                    f"复核人已更正到 v{current_version}，当前别名下相关度量可投产，需重新发起投产校验"
                )
            else:
                summary = promotion.get("conclusion") or "本版投产校验阻断"
        else:
            summary = "投产校验尚未写结论"
        result["summary"] = summary
        return result


def list_promotions(model_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(PROMOTION_TABLE)
            if model_id is None or _int(row.get("model_id")) == model_id]
    rows.sort(key=lambda row: _int(row.get("id")), reverse=True)
    return [_deep(row) for row in rows[:limit]]


def lookup_confirmed_missing(
    field_id: str,
    source_id: int | None = None,
    model_id: int | None = None,
) -> list[dict[str, Any]]:
    """按字段标识查上一轮已确认的缺列，连同当时的输入摘要一并返回。"""
    results: list[dict[str, Any]] = []
    for entry in store.rows(ENTRY_TABLE):
        if _text(entry.get("field_id")) != _text(field_id):
            continue
        if entry.get("change_type") not in (CHANGE_MISSING, CHANGE_REMOVED):
            continue
        if entry.get("resolution") not in (RESOLUTION_MISSING, RESOLUTION_ALIAS_FIXED, RESOLUTION_IGNORED):
            continue
        if source_id is not None and _int(entry.get("source_id")) != source_id:
            continue
        batch = store.find(DIFF_TABLE, _int(entry.get("diff_batch_id")))
        if batch is None or batch.get("status") != DIFF_CONFIRMED:
            continue
        if model_id is not None and _int(batch.get("model_id")) != model_id:
            continue
        record = _deep(entry)
        record["batch"] = {
            "id": batch.get("id"),
            "batch_no": batch.get("batch_no"),
            "model_id": batch.get("model_id"),
            "input_digest": batch.get("input_digest"),
            "baseline_scan_ids": batch.get("baseline_scan_ids", []),
            "target_scan_ids": batch.get("target_scan_ids", []),
            "alias_version": batch.get("alias_version"),
            "confirmed_at": batch.get("confirmed_at"),
            "confirmed_by": batch.get("confirmed_by"),
        }
        results.append(record)
    results.sort(key=lambda row: _text((row.get("batch") or {}).get("confirmed_at")), reverse=True)
    return results


def summary() -> dict[str, Any]:
    with _LOCK:
        scans = list(store.rows(SCAN_TABLE))
        batches = list(store.rows(DIFF_TABLE))
        promotions = list(store.rows(PROMOTION_TABLE))
        return {
            "scan_batches": len(scans),
            "effective_scans": sum(1 for row in scans if row.get("status") == SCAN_EFFECTIVE),
            "diff_batches": len(batches),
            "pending_batches": sum(1 for row in batches if row.get("status") == DIFF_PENDING),
            "confirmed_batches": sum(1 for row in batches if row.get("status") == DIFF_CONFIRMED),
            "pending_entries": sum(1 for row in store.rows(ENTRY_TABLE) if row.get("resolution") == RESOLUTION_PENDING),
            "promotions": len(promotions),
            "blocked_promotions": sum(1 for row in promotions if row.get("result") == "阻断"),
        }


# ---------------------------------------------------------------------------
# 与语义模型服务共用的辅助函数
# ---------------------------------------------------------------------------

def ensure_model_diff(model_id: int) -> dict[str, Any]:
    """取该模型最新对照批次；没有时按当前快照与别名生成一次。"""
    model = store.find("semantic_model", model_id)
    if model is None:
        return {"ok": False, "message": "语义模型不存在"}
    latest = None
    for row in store.rows(DIFF_TABLE):
        if _int(row.get("model_id")) == model_id:
            if latest is None or _int(row.get("id")) > _int(latest.get("id")):
                latest = row
    if latest is not None:
        return {"ok": True, "batch": _deep(latest), "created": False}
    created = create_diff_batch(model_id)
    if not created.get("ok"):
        return created
    return {"ok": True, "batch": created["batch"], "created": True}


def model_overview(model_id: int) -> dict[str, Any]:
    """给对照视图一个聚合入口：模型、快照、批次、当前别名和最近投产。"""
    with _LOCK:
        model = store.find("semantic_model", model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}
        sources = _source_refs(model)
        latest_promotion = None
        for row in store.rows(PROMOTION_TABLE):
            if _int(row.get("model_id")) == model_id:
                if latest_promotion is None or _int(row.get("id")) > _int(latest_promotion.get("id")):
                    latest_promotion = row
        return {
            "ok": True,
            "model": _deep(model),
            "source_ids": sources,
            "scans": [_deep(row) for sid in sources for row in [_latest_scan(sid)] if row],
            "batches": list_diff_batches(model_id=model_id),
            "alias_version": current_alias_version(model_id),
            "aliases": [_deep(row) for row in _alias_rows(model_id, current_alias_version(model_id))],
            "latest_promotion": _deep(latest_promotion) if latest_promotion else None,
        }


def _update_contract_on_promotion(model: dict[str, Any]) -> None:
    contracts = store.rows("data_contract")
    for contract in contracts:
        if contract.get("status") == "草稿" and _text(model.get("模型名称")) in _text(contract.get("数据集")):
            contract["Schema约束"] = model.get("字段别名", "")
            contract["status"] = "已承诺"


def _audit_promotion(model: dict[str, Any], promotion_no: str, action: str) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": audit_id,
        "记录编号": f"AUDIT-{_digest({'promo': promotion_no, 'action': action, 'ts': _now()})}",
        "查询语句": f"SEMANTIC_{action} model={model.get('模型编号')} promotion={promotion_no}",
        "执行耗时": "200ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "semantic_model",
        "操作用户": "validator",
        "查询时间": _now(),
        "status": "正常",
        "pending": False,
        "abnormal": action == "BLOCK",
    })
