"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class ResultMartService(BaseModuleService):
    MODULE = "result_mart"
    ENTITY = "数据Mart"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["集市编号", "集市名称", "数据来源", "主题域", "行数统计", "刷新周期", "下游消费", "最后刷新"]
    REQUIRED_FIELDS = ["集市编号", "集市名称", "数据来源"]
    STATUS_ORDER = ["草稿", "刷新中", "可用", "刷新失败"]
    ACTION_RULES = {"启动刷新": "刷新中", "标记可用": "可用", "标记失败": "刷新失败"}
    NEGATIVE_ACTIONS = []
