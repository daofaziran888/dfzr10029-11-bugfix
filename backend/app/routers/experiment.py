"""分析实验接口：阈值版本管理、按版本重算显著性、阈值判定记录与结果导出。"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, EntryPayload
from app.services.experiment import (
    SCOPE_FROZEN,
    SCOPE_RUNTIME,
    SCOPES,
    ExperimentService,
)

router = APIRouter(prefix="/api/experiment", tags=["分析实验"])
service = ExperimentService()
LIST_FIELDS = ["方案编号", "实验名称", "分析数据集", "样本范围", "分组字段", "目标度量", "基准区间", "显著阈值"]
STATUSES = ["草稿", "已设计", "运行中", "已完成"]

# 阈值判定记录导出列：明确当前结论使用的阈值版本、显著阈值与基准区间。
JUDGEMENT_EXPORT_FIELDS = [
    "id", "方案编号", "分组", "版本编号", "版本名称", "处理范围",
    "显著阈值", "基准区间", "实验组指标值", "对照组指标值", "分组差异",
    "z统计量", "p值", "阈值判定", "不显著原因", "判定条件", "重算时间",
]


def csv_cell(value: Any) -> Any:
    """防公式注入：危险起始字符加前缀；引号与包裹交给 csv.writer 统一处理。"""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in "=+-@":
        return "'" + text
    return text


def _validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"处理范围仅支持：{'、'.join(SCOPES)}")
    return scope


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/threshold-versions")
def list_threshold_versions() -> dict[str, Any]:
    """显著阈值版本业务明细：设计冻结版本与运行期生效版本一并返回。"""
    return {"items": service.list_threshold_versions()}


@router.post("/recalculate")
def recalculate(
    payload: EntryPayload | None = None,
    scope: str = Query(default=SCOPE_RUNTIME, description=f"{SCOPE_FROZEN} / {SCOPE_RUNTIME}"),
    version_id: int | None = Query(default=None, description="显式指定阈值版本时优先于处理范围"),
    experiment_id: int | None = Query(default=None),
) -> ActionResult:
    """读取既有数据后按阈值版本重算显著性与基准区间；重复提交只写一份有效结果。"""
    _validate_scope(scope)
    try:
        records = service.recalculate(
            experiment_id=experiment_id,
            version_id=version_id,
            scope=scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ActionResult(
        ok=True,
        message=f"已按阈值版本重算 {len(records)} 条分组判定记录",
        entry={"items": records, "count": len(records)},
    )


@router.get("/judgements")
def list_judgements(
    experiment_id: int | None = Query(default=None),
    version_id: int | None = Query(default=None),
    scope: str | None = Query(default=None),
) -> dict[str, Any]:
    """跨分析模块读取阈值判定记录；列表按记录 id 与业务键关联，杜绝错位。"""
    if scope is not None:
        _validate_scope(scope)
    items = service.list_judgements(
        experiment_id=experiment_id, version_id=version_id, scope=scope)
    return {"items": items, "total": len(items)}


@router.get("/judgements/{judgement_id}")
def get_judgement(judgement_id: int) -> dict[str, Any]:
    """详情读取单条阈值判定记录，保留判定条件与业务输入快照，不残留旧记录。"""
    record = service.get_judgement(judgement_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"阈值判定记录 {judgement_id} 不存在")
    return record


@router.get("/results")
def get_experiment_results(
    experiment_id: int | None = Query(default=None),
    version_id: int | None = Query(default=None),
    scope: str = Query(default=SCOPE_RUNTIME),
) -> dict[str, Any]:
    """实验结果接口编排阈值判定记录，供结果页与消费方验证当前口径结论。"""
    _validate_scope(scope)
    try:
        return service.experiment_results(
            experiment_id=experiment_id, version_id=version_id, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/export")
def export_entries(
    experiment_id: int | None = Query(default=None),
    version_id: int | None = Query(default=None),
    scope: str = Query(default=SCOPE_RUNTIME),
) -> StreamingResponse:
    """导出当前阈值版本下的显著性结论，表头与每行都写明阈值和基准区间。"""
    _validate_scope(scope)
    try:
        data = service.experiment_results(
            experiment_id=experiment_id, version_id=version_id, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    version = data["阈值版本"]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow([
        f"# 结论口径：版本编号={version['版本编号']}",
        f"显著阈值={version['显著阈值']}",
        f"基准区间={version['基准区间']}",
        f"处理范围={data['处理范围']}",
    ])
    writer.writerow(JUDGEMENT_EXPORT_FIELDS)
    for item in data["items"]:
        writer.writerow([csv_cell(item.get(k, "")) for k in JUDGEMENT_EXPORT_FIELDS])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=experiment_judgement.csv"},
    )


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"实验方案 {entry_id} 不存在")
    return entry
