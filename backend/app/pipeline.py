"""跨模块数据管道引擎：编排多模块协作的端到端数据流。

每个管道是一个有序处理阶段列表，每个阶段消费上游产物并产生下游输入。
管道执行过程写入审计记录，失败时保留中间产物供排查。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.store import store


@dataclass
class StageResult:
    stage: str
    module: str
    ok: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0
    artifact: str | None = None


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    stages: list[StageResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"

    def add_stage(self, result: StageResult) -> None:
        self.stages.append(result)
        if not result.ok:
            self.status = "failed"

    def finish(self) -> None:
        self.finished_at = time.time()
        if self.status == "running":
            self.status = "completed"


class PipelineEngine:
    """管道执行器，维护执行历史和中间产物。"""

    def __init__(self) -> None:
        self.runs: list[PipelineRun] = []
        self.artifacts: dict[str, dict[str, Any]] = {}

    def run_pipeline(
        self,
        name: str,
        stages: list[tuple[str, str, Callable[[dict[str, Any] | None], dict[str, Any]]]],
        context: dict[str, Any] | None = None,
    ) -> PipelineRun:
        run = PipelineRun(run_id=str(uuid.uuid4())[:8], pipeline_name=name)
        data: dict[str, Any] | None = context

        for stage_name, module, handler in stages:
            t0 = time.time()
            try:
                output = handler(data)
                result = StageResult(
                    stage=stage_name,
                    module=module,
                    ok=True,
                    output=output,
                    duration_ms=(time.time() - t0) * 1000,
                    artifact=_artifact_key(name, stage_name),
                )
                self.artifacts[result.artifact] = output
                data = output
            except Exception as exc:
                result = StageResult(
                    stage=stage_name,
                    module=module,
                    ok=False,
                    error=str(exc),
                    duration_ms=(time.time() - t0) * 1000,
                )
                run.add_stage(result)
                run.finish()
                self.runs.append(run)
                self._write_audit(run)
                return run
            run.add_stage(result)

        run.finish()
        self.runs.append(run)
        self._write_audit(run)
        return run

    def get_artifact(self, pipeline: str, stage: str) -> dict[str, Any] | None:
        return self.artifacts.get(_artifact_key(pipeline, stage))

    def _write_audit(self, run: PipelineRun) -> None:
        rows = store.rows("query_audit")
        audit_entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "记录编号": f"AUDIT-{run.run_id}",
            "查询语句": f"pipeline:{run.pipeline_name}",
            "执行耗时": f"{sum(s.duration_ms for s in run.stages):.0f}ms",
            "扫描行数": str(len(run.stages)),
            "返回行数": str(1 if run.status == "completed" else 0),
            "数据来源": "跨模块管道",
            "操作用户": "pipeline_engine",
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "正常" if run.status == "completed" else "慢查询",
            "pending": False,
            "abnormal": run.status == "failed",
        }
        rows.append(audit_entry)


def _artifact_key(pipeline: str, stage: str) -> str:
    digest = hashlib.md5(f"{pipeline}::{stage}".encode()).hexdigest()[:8]
    return f"{pipeline}/{stage}/{digest}"


# 全局单例
engine = PipelineEngine()