"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DimensionTreeService(BaseModuleService):
    MODULE = "dimension_tree"
    ENTITY = "维度层级"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["层级编号", "维度名称", "层级路径", "叶子字段", "汇总规则", "缓慢变化策略", "行数预估", "版本标签"]
    REQUIRED_FIELDS = ["层级编号", "维度名称", "层级路径"]
    STATUS_ORDER = ["草稿", "构建中", "可用", "已归档"]
    ACTION_RULES = {"启动构建": "构建中", "标记可用": "可用", "归档层级": "已归档"}
    NEGATIVE_ACTIONS = []
