"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class ScheduleJobService(BaseModuleService):
    MODULE = "schedule_job"
    ENTITY = "预计算任务"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["任务编号", "目标立方", "Cron表达式", "时区设置", "依赖检查", "超时上限", "失败通知", "下次执行"]
    REQUIRED_FIELDS = ["任务编号", "目标立方", "Cron表达式"]
    STATUS_ORDER = ["待激活", "计算中", "已完成", "已停用"]
    ACTION_RULES = {"激活任务": "计算中", "立即执行": "已完成", "停用任务": "已停用"}
    NEGATIVE_ACTIONS = ["停用任务"]
