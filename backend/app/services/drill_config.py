"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DrillConfigService(BaseModuleService):
    MODULE = "drill_config"
    ENTITY = "下钻路径"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["路径编号", "起始维度", "目标维度", "下钻类型", "关联键", "跳转规则", "展示模板", "最后使用"]
    REQUIRED_FIELDS = ["路径编号", "起始维度", "目标维度"]
    STATUS_ORDER = ["草稿", "已生效", "已禁用", "已废弃"]
    ACTION_RULES = {"启效路径": "已生效", "禁用路径": "已禁用", "废弃路径": "已废弃"}
    NEGATIVE_ACTIONS = []
