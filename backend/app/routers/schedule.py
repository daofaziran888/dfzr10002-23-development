"""定时分析任务接口：预演、规则校验、确认启用、队列与失败续跑。

接口分组：
- `GET  /api/schedule`                    列表
- `POST /api/schedule`                    登记（只校验必填，不自动启用）
- `POST /api/schedule/{id}/actions`       提交校验 / 启用配置 / 停用配置
- `POST /api/schedule/preview`            表单未保存时预演（不落库）
- `POST /api/schedule/{id}/preview`       对已登记任务预演并写入预演结果
- `POST /api/schedule/{id}/preview/confirm` 确认预演结果
- `GET  /api/schedule/{id}/preview`       读取已保存的预演结果
- `GET  /api/schedule/queue`              调度队列
- `GET  /api/schedule/{id}/runs`          运行记录（含阶段进度）
- `POST /api/schedule/{id}/runs`          执行一次运行 / 复现失败 / 失败后续跑
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.schedule import (
    ALLOWED_CHANNELS,
    MAX_DELAY_MINUTES,
    PREVIEW_COUNT,
    ScheduleService,
    STAGES,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    WINDOW_MAX,
    WINDOW_MIN,
)

router = APIRouter(prefix="/api/schedule", tags=["定时分析任务"])

service = ScheduleService()
FORM_TYPES = ["日报分析", "快照分析", "画像分析", "分层分析", "趋势分析"]

LIST_FIELDS = [
    "任务编号", "任务名称", "分析类型", "调度表达式", "调度时区", "执行窗口",
    "冲突阈值", "优先级", "输入参数", "输出目标", "通知渠道", "是否启用",
    "预演状态", "队列状态",
]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号或任务名称检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤定时分析任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/rules", response_model=dict)
def get_rules() -> dict[str, Any]:
    """调度规则口径：执行窗口范围、冲突阈值、优先级与允许提交条件。"""
    return {
        "执行窗口分钟": [WINDOW_MIN, WINDOW_MAX],
        "冲突阈值分钟": [THRESHOLD_MIN, THRESHOLD_MAX],
        "优先级": ["P1", "P2", "P3"],
        "分析类型": FORM_TYPES,
        "通知渠道": ALLOWED_CHANNELS,
        "预演次数": PREVIEW_COUNT,
        "允许最大顺延分钟": MAX_DELAY_MINUTES,
        "运行阶段": STAGES,
        "时区": "每条任务自带 IANA 时区，冲突按 UTC 绝对时间比较",
        "允许提交条件": "必填字段齐全、表达式 5 段合法、时区可识别、输出目标非空；启用前还需确认预演",
    }


@router.get("/queue", response_model=dict)
def get_queue() -> dict[str, Any]:
    """调度队列：已启用任务的首个待执行运行，按计划开始排序。"""
    items = service.queue()
    return {"items": items, "total": len(items)}


@router.post("/preview", response_model=dict)
def preview_unsaved(payload: EntryPayload) -> dict[str, Any]:
    """表单未保存时预演：输入分析类型、输出目标和通知渠道即可看到未来运行与顺延。"""
    values = payload.values or {}
    exclude_id = values.get("id")
    return service.preview_unsaved(values, exclude_id=int(exclude_id) if exclude_id else None)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出定时分析任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "schedule", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条定时任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"定时任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条定时任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少或非法字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="定时任务已登记，请先预演再确认启用", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交校验 / 启用配置 / 停用配置；未通过规则标准或未确认预演的任务不允许进入队列。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/preview", response_model=dict)
def preview_entry(entry_id: int, payload: EntryPayload | None = None) -> dict[str, Any]:
    """对已登记任务做窗口预演并保存结果；可带字段覆盖值先试算再决定是否落库。"""
    overrides = (payload.values or {}) if payload else {}
    record = service.preview(entry_id, overrides)
    if record is None:
        raise HTTPException(status_code=404, detail=f"定时任务 {entry_id} 不存在或已归档")
    return record


@router.get("/{entry_id}/preview", response_model=dict)
def get_preview(entry_id: int) -> dict[str, Any]:
    """读取已保存的预演结果；未预演时返回空结构而不是报错。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"定时任务 {entry_id} 不存在或已归档")
    record = entry.get("预演") or {}
    return {"任务编号": entry.get("任务编号"), "预演状态": entry.get("预演状态") or "", **(record or {})}


@router.post("/{entry_id}/preview/confirm", response_model=ActionResult)
def confirm_preview(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """确认预演结果；确认后才允许启用。"""
    remark = (payload.remark if payload else None) or ""
    record, message = service.confirm_preview(entry_id, remark=remark)
    if record is None:
        return ActionResult(ok=False, message=message)
    entry = service.get_entry(entry_id)
    return ActionResult(ok=True, message=message, entry=entry, preview=record)


@router.get("/{entry_id}/runs", response_model=dict)
def get_runs(entry_id: int) -> dict[str, Any]:
    """运行记录：每条运行保留阶段进度，失败后可从失败阶段继续。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"定时任务 {entry_id} 不存在或已归档")
    runs = service.get_runs(entry_id)
    return {"items": runs, "total": len(runs), "阶段": STAGES}


@router.post("/{entry_id}/runs", response_model=ActionResult)
def execute_run(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行一次运行；`fail_at` 指定阶段复现失败，再次调用即从失败阶段续跑。"""
    values = payload.values or {}
    try:
        run_seq = int(values.get("run_seq") or 1)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="run_seq 必须是正整数")
    row, message = service.execute_run(
        entry_id,
        run_seq=run_seq,
        fail_at=str(values.get("fail_at") or "") or None,
        actual_start=str(values.get("actual_start") or "") or None,
        note=str(values.get("note") or ""),
    )
    if row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=row.get("状态") != "失败", message=message, entry=row)
