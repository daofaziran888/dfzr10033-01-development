"""语义模型领域服务：管理表映射、字段别名、度量定义与计算口径，作为查询翻译层。"""
from __future__ import annotations

from typing import Any

from app.services import field_diff
from app.services.base import BaseModuleService
from app.store import store


class SemanticModelService(BaseModuleService):
    MODULE = "semantic_model"
    ENTITY = "语义定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模型编号", "模型名称", "源表映射", "字段别名", "度量定义", "维度定义", "计算口径", "版本序号"]
    REQUIRED_FIELDS = ["模型编号", "模型名称", "源表映射"]
    STATUS_ORDER = ["草稿", "校验中", "已投产", "已废弃"]
    ACTION_RULES = {"提交校验": "校验中", "投产模型": "已投产", "废弃模型": "已废弃"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """投产动作走字段变更对照校验，不再只看别名字符串非空。

        「投产模型」会读取该模型对应的差异明细、生成字段校验记录并把结论回写到
        差异清单；只有没有缺列阻断时才真正把模型置为已投产。
        """
        model = store.find(self.MODULE, entry_id)
        if model is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "投产模型":
            result = field_diff.promote_model(entry_id, requested_by="validator")
            if not result.get("ok"):
                return model, result.get("message", "字段变更校验阻断，模型未投产")
            return model, result.get("message", "模型已投产")
        return super().run_action(entry_id, action)

    def validate_and_promote(self, model_id: int) -> dict[str, Any]:
        """校验语义模型并投产：以字段变更对照的缺列结论作为准入条件。

        跨模块数据流：
        - 读取 source_registry 的字段快照（必要时先补基线扫描）
        - 读取/追加 semantic_model 的字段别名版本
        - 生成 field_diff_batch 差异明细与 field_validation 字段校验记录
        - 把校验结论回写到差异清单，并更新数据契约与审计日志
        """
        result = field_diff.promote_model(model_id, requested_by="validator")
        if not result.get("ok"):
            return {
                "ok": False,
                "message": result.get("message", "校验失败"),
                "issues": result.get("missing_field_ids", []),
                "promotion": result.get("promotion"),
                "missing_fields": result.get("missing_fields", []),
            }
        detail = result.get("promotion_detail") or {}
        return {
            "ok": True,
            "model_id": model_id,
            "status": "已投产",
            "promotion": result.get("promotion"),
            "validation": result.get("validation"),
            "fields_count": len(detail.get("alias_snapshot", [])),
        }

    def detect_schema_conflicts(self, source_id: int) -> dict[str, Any]:
        """检测数据源Schema变化与已投产语义模型之间的冲突。

        以字段变更对照的差异明细为准：字段标识消失的列为缺列，字段标识仍在、
        只是物理列改名的差异按「别名沿用」处理，不计入冲突。
        """
        models = store.rows(self.MODULE)
        conflicts = []

        for model in models:
            if model.get("status") != "已投产":
                continue
            if str(source_id) not in str(model.get("源表映射", "")):
                continue

            batch_result = field_diff.create_diff_batch(
                int(model["id"]), source_id=source_id, requested_by="schema_drift",
            )
            if not batch_result.get("ok"):
                continue
            batch = batch_result["batch"]
            entries = batch_result.get("entries", [])
            blocking = [
                entry for entry in entries
                if entry.get("change_type") in (field_diff.CHANGE_MISSING, field_diff.CHANGE_REMOVED)
                and entry.get("resolution") == field_diff.RESOLUTION_PENDING
            ]
            conflicts.append({
                "model_id": model["id"],
                "model_name": model.get("模型名称"),
                "source_id": source_id,
                "diff_batch_id": batch["id"],
                "diff_batch_no": batch["batch_no"],
                "diff_status": batch["status"],
                "schema_change": "；".join(
                    f"{entry.get('change_type')} {entry.get('physical_column') or entry.get('field_id')}"
                    for entry in entries
                ),
                "current_fields": [entry.get("alias") for entry in entries],
                "blocking_count": len(blocking),
                "blocking_fields": [entry.get("field_id") for entry in blocking],
            })

        return {
            "ok": True,
            "source_id": source_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        }


def _update_contract_on_promotion(model: dict[str, Any]) -> None:
    contracts = store.rows("data_contract")
    for c in contracts:
        if c.get("status") == "草稿" and model.get("模型名称", "") in str(c.get("数据集", "")):
            c["Schema约束"] = model.get("字段别名", "")
            c["status"] = "已承诺"
