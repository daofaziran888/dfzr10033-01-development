"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class BenchmarkSetService(BaseModuleService):
    MODULE = "benchmark_set"
    ENTITY = "分析基线"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["基线编号", "基线名称", "数据范围", "基线时段", "对比维度", "指标数值", "方差阈值", "锁定状态"]
    REQUIRED_FIELDS = ["基线编号", "基线名称", "数据范围"]
    STATUS_ORDER = ["草稿", "已锁定", "对比中", "已过期"]
    ACTION_RULES = {"锁定基线": "已锁定", "启动对比": "对比中", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []
