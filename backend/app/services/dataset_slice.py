"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DatasetSliceService(BaseModuleService):
    MODULE = "dataset_slice"
    ENTITY = "切片配置"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["切片编号", "源立方", "维度过筛", "度量过滤", "切片标签", "行数统计", "创建人", "最后刷新"]
    REQUIRED_FIELDS = ["切片编号", "源立方", "维度过筛"]
    STATUS_ORDER = ["草稿", "共享中", "已归档", "已废弃"]
    ACTION_RULES = {"发布共享": "共享中", "归档切片": "已归档", "废弃切片": "已废弃"}
    NEGATIVE_ACTIONS = []
