"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class AbConfigService(BaseModuleService):
    MODULE = "ab_config"
    ENTITY = "AB实验"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["实验编号", "实验名称", "分流比例", "指标集合", "对照组", "实验组", "置信水平", "运行期间"]
    REQUIRED_FIELDS = ["实验编号", "实验名称", "分流比例"]
    STATUS_ORDER = ["草稿", "运行中", "已结束", "已终止"]
    ACTION_RULES = {"启动实验": "运行中", "结束实验": "已结束", "终止实验": "已终止"}
    NEGATIVE_ACTIONS = []
