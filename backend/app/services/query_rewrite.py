"""查询改写领域服务：SQL改写规则管理，代价评估，与联邦查询和聚合引擎的交互。"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QueryRewriteService(BaseModuleService):
    MODULE = "query_rewrite"
    ENTITY = "改写规则"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["规则编号", "原始模式", "替换模式", "适用模型", "改写条件", "代价阈值", "命中次数", "最后命中"]
    REQUIRED_FIELDS = ["规则编号", "原始模式", "替换模式"]
    STATUS_ORDER = ["草稿", "已生效", "待验证", "已废弃"]
    ACTION_RULES = {"启效规则": "已生效", "提交验证": "待验证", "废弃规则": "已废弃"}
    NEGATIVE_ACTIONS = ["废弃规则"]

    def rewrite_query(
        self, raw_query: str, model_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """应用已生效规则改写查询：读取 semantic_model 和 query_audit，写入 result_cache 和 cost_govern。"""
        rules = store.rows(self.MODULE)
        active_rules = [r for r in rules if r.get("status") == "已生效"]

        if not active_rules:
            return {"ok": True, "rewritten": raw_query, "rules_applied": 0, "savings": 0}

        rewritten = raw_query
        rules_applied = 0

        for rule in active_rules:
            pattern = rule.get("原始模式", "")
            replacement = rule.get("替换模式", "")
            condition = rule.get("改写条件", "")

            if pattern and pattern in rewritten:
                # 检查改写条件（从语义模型匹配）
                if model_ids:
                    matched = _match_models(condition, model_ids)
                    if not matched:
                        continue

                rewritten = rewritten.replace(pattern, replacement)
                rules_applied += 1

                # 更新规则命中计数
                try:
                    hit_count = int(rule.get("命中次数", "0"))
                except (ValueError, TypeError):
                    hit_count = 0
                rule["命中次数"] = str(hit_count + 1)
                rule["最后命中"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # 估算节省量并写入成本治理
        savings = _estimate_savings(raw_query, rewritten, rules_applied)
        if savings > 0:
            _record_cost_savings(savings, rules_applied)

        # 写入结果缓存（记录改写前后对比）
        _cache_rewrite_result(raw_query, rewritten)

        return {
            "ok": True,
            "original": raw_query,
            "rewritten": rewritten,
            "rules_applied": rules_applied,
            "estimated_savings_rows": savings,
        }

    def analyze_query_patterns(self) -> dict[str, Any]:
        """分析 query_audit 慢查询模式，生成改写规则建议。"""
        audits = store.rows("query_audit")
        slow_queries = [a for a in audits if a.get("status") == "慢查询"]

        patterns: dict[str, int] = {}
        for sq in slow_queries:
            stmt = sq.get("查询语句", "")
            # 简单模式提取
            if "CUBE_BUILD" in stmt:
                patterns["cube_build"] = patterns.get("cube_build", 0) + 1
            elif "FEDERATED" in stmt:
                patterns["federated_query"] = patterns.get("federated_query", 0) + 1
            elif "EXPORT" in stmt:
                patterns["export"] = patterns.get("export", 0) + 1
            else:
                patterns[stmt[:30]] = patterns.get(stmt[:30], 0) + 1

        recommendations = []
        for pattern, count in patterns.items():
            if count >= 2:
                recommendations.append({
                    "pattern": pattern,
                    "frequency": count,
                    "suggested_action": "CREATE_PUSHDOWN_RULE" if "federated" in pattern else "ADD_CACHE_HINT",
                })

        return {
            "ok": True,
            "slow_queries_count": len(slow_queries),
            "patterns_found": len(patterns),
            "recommendations": recommendations,
        }


def _match_models(condition: str, model_ids: list[int]) -> bool:
    if not condition:
        return True
    models = store.rows("semantic_model")
    for mid in model_ids:
        for m in models:
            if m.get("id") == mid and condition in str(m):
                return True
    return False


def _estimate_savings(original: str, rewritten: str, rules_applied: int) -> int:
    if original == rewritten:
        return 0
    base_savings = len(original) - len(rewritten)
    return max(0, abs(base_savings) * random.randint(100, 10000))


def _record_cost_savings(savings: int, rules_applied: int) -> None:
    rows = store.rows("cost_govern")
    cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    cost = {
        "id": cost_id,
        "策略编号": f"COST-RW{uuid.uuid4().hex[:8]}",
        "适用角色": "query_rewrite",
        "查询上限": str(savings),
        "并发上限": str(rules_applied),
        "月度配额": str(savings * 100),
        "告警阈值": str(savings * 2),
        "限流动作": "optimize",
        "生效时段": time.strftime("%Y-%m-%d"),
        "status": "已生效",
        "pending": False,
        "abnormal": False,
    }
    rows.append(cost)


def _cache_rewrite_result(original: str, rewritten: str) -> None:
    rows = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    import hashlib
    cache = {
        "id": cache_id,
        "条目编号": f"RESU-RW{uuid.uuid4().hex[:8]}",
        "查询摘要": f"rewrite_{hashlib.md5(original.encode()).hexdigest()[:12]}",
        "结果哈希": hashlib.md5(rewritten.encode()).hexdigest()[:12],
        "缓存命中": "0",
        "过期时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600)),
        "存储大小": str(len(rewritten)),
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "最后命中": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "result_data": [{"original": original, "rewritten": rewritten}],
    }
    rows.append(cache)