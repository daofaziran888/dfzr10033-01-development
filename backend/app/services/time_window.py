"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class TimeWindowService(BaseModuleService):
    MODULE = "time_window"
    ENTITY = "窗口定义"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["窗口编号", "窗口类型", "滑动宽度", "滑动步长", "对齐基准", "缺值策略", "窗口别名", "最后计算"]
    REQUIRED_FIELDS = ["窗口编号", "窗口类型", "滑动宽度"]
    STATUS_ORDER = ["草稿", "启用中", "已暂停", "已废弃"]
    ACTION_RULES = {"启用窗口": "启用中", "暂停窗口": "已暂停", "废弃窗口": "已废弃"}
    NEGATIVE_ACTIONS = []
