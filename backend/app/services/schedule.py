"""定时分析任务业务规则：预演、规则校验、确认启用、失败续跑与报告队列同步。

调度口径集中在 `app.services.scheduling`，这里只负责把它和任务生命周期接起来：

* 登记时保留调度表达式、时区、执行窗口、冲突阈值与优先级，并生成预演。
* 预演结果必须落到任务记录上（`预演状态=已预演`），**确认预演且通过规则校验后**才可启用。
* 启用后任务进入调度队列；报告生成时间同时写入分析报告清单。
* 实际运行按阶段推进（数据准备 → 生成报告 → 分发通知）。失败时保留已完成阶段，
  下一次从失败阶段继续；实际开始时间晚于计划时，后续运行整体同步顺延。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.scheduling import (
    CronError,
    CronExpression,
    PRIORITY_RANK,
    STAGES,
    TaskSpec,
    TimelinePlan,
    describe_window,
    next_occurrences,
    parse_clock,
    parse_timezone,
    plan_timeline,
    projected_report_times,
    shift_runs,
)
from app.store import store

MODULE = "schedule"
REPORT_MODULE = "report"
RUN_MODULE = "schedule_run"

REQUIRED_FIELDS = ["任务编号", "任务名称", "分析类型", "调度表达式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# ---- 规则口径（业务确认后的标准，代码是唯一权威） ----
WINDOW_MIN, WINDOW_MAX = 5, 240
THRESHOLD_MIN, THRESHOLD_MAX = 0, 120
PREVIEW_COUNT = 5
MAX_DELAY_MINUTES = 240
ALLOWED_CHANNELS = ["邮件", "企业微信", "短信", "站内信"]
ACTIVE_STATUSES = ["已启用"]
PREVIEW_CONFIRMED = "已确认"
PREVIEW_DONE = "已预演"


class ScheduleRuleError(ValueError):
    """规则校验未通过；调用方把 reasons 原样返回给前端。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("；".join(reasons))
        self.reasons = reasons


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def _int_value(value: Any) -> int | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def build_task(values: dict[str, Any], *, task_id: int, code: str) -> TaskSpec:
    """把一份任务配置转成排程引擎的 TaskSpec；缺省值时按业务默认口径补齐。"""
    return TaskSpec(
        task_id=task_id,
        code=code,
        name=str(values.get("任务名称") or code),
        analysis_type=str(values.get("分析类型") or ""),
        expression=str(values.get("调度表达式") or "").strip(),
        timezone_name=str(values.get("调度时区") or "Asia/Shanghai").strip(),
        window_minutes=_int_value(values.get("执行窗口")) or 30,
        conflict_threshold=_int_value(values.get("冲突阈值")) or 0,
        priority=str(values.get("优先级") or "P3").strip().upper(),
        output_target=str(values.get("输出目标") or ""),
        notification_channel=str(values.get("通知渠道") or ""),
        report_code=str(values.get("报告编号") or ""),
    )


class ScheduleService:
    # ---------------------------------------------------------------- 列表/登记
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("任务编号", "")) or keyword in str(row.get("任务名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        code = str(values.get("任务编号")).strip()
        if any(row.get("任务编号") == code for row in rows):
            return None, [f"任务编号 {code} 已存在"]
        # 规则标准不通过的任务不允许保存：窗口范围、冲突阈值、优先级、表达式与时区都要先过关。
        validation = self.validate(values)
        if not validation["ok"]:
            return None, validation["reasons"]
        entry: dict[str, Any] = {"id": entry_id}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 窗口/阈值允许字符串形式提交，校验层按 int 处理，落库统一存整数。
        entry.update({
            "调度时区": str(values.get("调度时区") or "Asia/Shanghai"),
            "执行窗口": _int_value(values.get("执行窗口")),
            "冲突阈值": _int_value(values.get("冲突阈值")),
            "优先级": str(values.get("优先级")).upper(),
            "输入参数": values.get("输入参数") or "",
            "输出目标": values.get("输出目标") or "",
            "通知渠道": values.get("通知渠道") or "",
            "是否启用": "否",
            "报告编号": values.get("报告编号") or self._next_report_code(),
        })
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["预演状态"] = ""
        entry["预演"] = None
        entry["队列状态"] = "未入队"
        rows.append(entry)
        return entry, []

    def _next_report_code(self) -> str:
        rows = store.rows(REPORT_MODULE)
        numbers = []
        for row in rows:
            match = str(row.get("报告编号", ""))
            if match.startswith("REPO-") and match[5:].isdigit():
                numbers.append(int(match[5:]))
        return f"REPO-{max(numbers, default=0) + 1:04d}"

    def _next_report_title(self, code: str) -> str:
        return f"{code} 定时分析报告"

    # ---------------------------------------------------------------- 规则校验
    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        """校验执行窗口、冲突阈值、优先级与允许提交条件，返回结构化结果。"""
        reasons: list[str] = []
        warnings: list[str] = []
        window = _int_value(values.get("执行窗口"))
        threshold = _int_value(values.get("冲突阈值"))
        priority = str(values.get("优先级") or "").strip().upper()

        if window is None:
            reasons.append("执行窗口必须填写分钟数")
        elif not WINDOW_MIN <= window <= WINDOW_MAX:
            reasons.append(f"执行窗口 {window} 分钟超出允许范围 {WINDOW_MIN}-{WINDOW_MAX} 分钟")
        if threshold is None:
            reasons.append("冲突阈值必须填写分钟数（0 表示不允许窗口重叠）")
        elif not THRESHOLD_MIN <= threshold <= THRESHOLD_MAX:
            reasons.append(f"冲突阈值 {threshold} 分钟超出允许范围 {THRESHOLD_MIN}-{THRESHOLD_MAX} 分钟")
        if window is not None and threshold is not None and threshold >= window:
            reasons.append(
                f"冲突阈值 {threshold} 分钟不小于执行窗口 {window} 分钟，任何重叠都不会被判为冲突"
            )
        if priority not in PRIORITY_RANK:
            reasons.append(f"优先级「{priority or '未填写'}」不在业务口径 P1/P2/P3 内")
        if not str(values.get("输出目标") or "").strip():
            reasons.append("输出目标不能为空")
        channel = str(values.get("通知渠道") or "").strip()
        if channel and channel not in ALLOWED_CHANNELS:
            reasons.append(f"通知渠道「{channel}」不在允许清单：{'、'.join(ALLOWED_CHANNELS)}")
        if not channel:
            warnings.append("未选择通知渠道，失败时不会发出提醒")

        tz_name = str(values.get("调度时区") or "Asia/Shanghai").strip()
        try:
            parse_timezone(tz_name)
        except CronError as exc:
            reasons.append(str(exc))
        try:
            expression = CronExpression.parse(str(values.get("调度表达式") or ""))
        except CronError as exc:
            reasons.append(f"调度表达式不合法：{exc}")
        else:
            try:
                tz = parse_timezone(tz_name)
                occurrences = next_occurrences(expression, tz, after=_now(), count=1)
            except CronError:
                occurrences = []
            if not occurrences:
                reasons.append("调度表达式在可预见时间内不会触发，请检查日期/月份/星期组合")
            elif tz_name != "Asia/Shanghai":
                warnings.append(
                    f"任务使用 {tz_name} 时区，冲突判定已按 UTC 绝对时间换算，与 Asia/Shanghai 任务可比较"
                )

        return {
            "ok": not reasons,
            "reasons": reasons,
            "warnings": warnings,
            "standards": {
                "执行窗口": f"{WINDOW_MIN}-{WINDOW_MAX} 分钟",
                "冲突阈值": f"{THRESHOLD_MIN}-{THRESHOLD_MAX} 分钟且不大于执行窗口",
                "优先级口径": "P1 > P2 > P3，同级按任务编号升序；由业务登记时指定",
                "允许提交条件": "必填字段齐全、表达式 5 段合法、时区可识别、输出目标非空",
            },
        }

    # ---------------------------------------------------------------- 预演
    def _existing_tasks(self, *, exclude_id: int | None = None) -> list[TaskSpec]:
        specs: list[TaskSpec] = []
        for row in store.rows(MODULE):
            if row.get("status") not in ACTIVE_STATUSES:
                continue
            if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
                continue
            try:
                specs.append(build_task(row, task_id=int(row["id"]), code=str(row.get("任务编号"))))
            except CronError:
                continue
        return specs

    def preview(
        self,
        entry_id: int,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """对一条任务做窗口预演，返回未来若干次运行、冲突任务与顺延原因。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        merged = {**entry, **(overrides or {})}
        validation = self.validate(merged)
        if not validation["ok"]:
            return {
                "任务编号": entry.get("任务编号"),
                "ok": False,
                "rules": validation,
                "runs": [],
                "conflicts": [],
                "affected_reports": [],
                "message": "规则校验未通过，无法生成预演",
            }

        task = build_task(merged, task_id=entry_id, code=str(entry.get("任务编号")))
        now = self._preview_start(merged)
        plan = plan_timeline(task, self._existing_tasks(exclude_id=entry_id), now=now, count=PREVIEW_COUNT)
        record = self._build_record(entry, merged, task, plan, now)
        # 带覆盖值的是「试算」，不写回任务；只有与已保存配置一致的预演才算正式预演。
        saved_task = build_task(entry, task_id=entry_id, code=str(entry.get("任务编号")))
        if saved_task.fingerprint() == record["fingerprint"]:
            entry["预演"] = record
            entry["预演状态"] = PREVIEW_DONE
            entry["预演摘要"] = record["summary"]
            entry["预演指纹"] = record["fingerprint"]
            entry["异常原因"] = "" if validation["ok"] else "；".join(validation["reasons"])
            # 预演一生成就带到分析报告清单，确认/启用只更新状态，不改时间口径。
            entry["队列状态"] = entry.get("队列状态") or "未入队"
            self._sync_report_preview(entry, record)
        else:
            record["试算"] = True
            record["message"] = "当前参数与已保存配置不同，本结果仅为试算，未写入任务；保存并重新预演后才能确认"
        return record

    def preview_unsaved(
        self,
        values: dict[str, Any],
        *,
        exclude_id: int | None = None,
    ) -> dict[str, Any]:
        """表单未保存时的预演：不落库，仅返回结果，供登记页立即查看。"""
        code = str(values.get("任务编号") or "（未命名任务）").strip()
        merged = {**values}
        if not str(merged.get("报告编号") or "").strip():
            # 未保存表单还没有报告编号，用「下一个可用编号」预占，预演里就能看到受影响报告。
            merged["报告编号"] = self._next_report_code()
        validation = self.validate(merged)
        if not validation["ok"]:
            return {
                "任务编号": code,
                "ok": False,
                "rules": validation,
                "runs": [],
                "conflicts": [],
                "affected_reports": [],
                "message": "规则校验未通过，无法生成预演",
            }
        task_id = exclude_id or max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1
        task = build_task(merged, task_id=task_id, code=code)
        now = self._preview_start(merged)
        plan = plan_timeline(task, self._existing_tasks(exclude_id=exclude_id), now=now, count=PREVIEW_COUNT)
        fake_entry = {"任务编号": code, **merged, "id": task_id}
        return self._build_record(fake_entry, merged, task, plan, now)

    def _preview_start(self, values: dict[str, Any]) -> datetime:
        """预演起点取「现在」；表单可以传 预演起点 覆盖，方便复现冲突场景。"""
        override = parse_clock(str(values.get("预演起点") or ""), str(values.get("调度时区") or "Asia/Shanghai"))
        return override or _now()

    def _build_record(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        task: TaskSpec,
        plan: TimelinePlan,
        now: datetime,
    ) -> dict[str, Any]:
        runs = [run.as_dict() for run in plan.runs]
        conflicts = []
        for run in plan.runs:
            if run.delay_minutes:
                conflicts.extend(run.conflicts)
        for run in plan.shifted:
            conflicts.extend({
                "受影响任务": run.task.code,
                "任务名称": run.task.name,
                "优先级": run.task.priority,
                "原定开始": describe_window(run.base_start, run.task)["display"],
                "顺延后开始": describe_window(run.start, run.task)["display"],
                **item,
            } for item in run.conflicts)
        affected = projected_report_times(plan)
        max_delay = max((run.delay_minutes for run in plan.runs), default=0)
        summary = (
            f"未来 {len(runs)} 次运行中 {sum(1 for r in runs if r['顺延分钟'])} 次顺延，"
            f"最大顺延 {max_delay} 分钟"
            if plan.has_conflict
            else f"未来 {len(runs)} 次运行无冲突"
        )
        record = {
            "任务编号": entry.get("任务编号"),
            "任务名称": task.name,
            "分析类型": task.analysis_type,
            "输出目标": task.output_target,
            "通知渠道": task.notification_channel,
            "报告编号": task.report_code,
            "优先级": task.priority,
            "调度时区": task.timezone_name,
            "执行窗口": task.window_minutes,
            "冲突阈值": task.conflict_threshold,
            "调度表达式": task.expression,
            "预演起点": describe_window(now, task)["display"],
            "ok": True,
            "has_conflict": plan.has_conflict,
            "rules": self.validate(values),
            "summary": summary,
            "max_delay_minutes": max_delay,
            "runs": runs,
            "conflicts": conflicts,
            "affected_reports": affected,
            "shifted_tasks": [
                {
                    "任务编号": run.task.code,
                    "任务名称": run.task.name,
                    "原定开始": describe_window(run.base_start, run.task)["display"],
                    "顺延后开始": describe_window(run.start, run.task)["display"],
                    "顺延分钟": run.delay_minutes,
                }
                for run in plan.shifted
            ],
            "fingerprint": task.fingerprint(),
            "confirmed": False,
            "生成时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # 预演结果同时展开成「按任务的运行清单」，入队后直接复用，避免二次计算偏差。
        record["tasks"] = []
        self._sync_tasks(record)
        return record

    # ---------------------------------------------------------------- 确认/启用
    def confirm_preview(self, entry_id: int, *, remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """确认预演结果并写入启用资格；表达式变了必须重新预演。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"定时任务 {entry_id} 不存在或已归档"
        record = entry.get("预演")
        if not record or not record.get("ok"):
            return None, "尚未生成预演结果，请先执行窗口冲突预演"
        task = build_task(entry, task_id=entry_id, code=str(entry.get("任务编号")))
        if task.fingerprint() != record.get("fingerprint"):
            entry["预演状态"] = ""
            return None, "调度配置在预演后发生变更，请重新预演后再确认"
        if record.get("max_delay_minutes", 0) > MAX_DELAY_MINUTES:
            return None, f"预演最大顺延 {record['max_delay_minutes']} 分钟，超过允许上限 {MAX_DELAY_MINUTES} 分钟"
        record = copy.deepcopy(record)
        record["confirmed"] = True
        record["confirmed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["confirmed_by"] = "值班管理员"
        record["confirm_remark"] = remark or ""
        entry["预演"] = record
        entry["预演状态"] = PREVIEW_CONFIRMED
        entry["预演摘要"] = record["summary"]
        entry["异常原因"] = ""
        self._sync_report_preview(entry, record)
        return record, f"预演已确认：{record['summary']}"

    def _sync_report_preview(self, entry: dict[str, Any], record: dict[str, Any]) -> None:
        """把预演结果带到分析报告清单：计划生成时间、冲突与顺延一致。"""
        reports = store.rows(REPORT_MODULE)
        report_code = str(entry.get("报告编号") or "")
        if not report_code:
            return
        report = next((row for row in reports if row.get("报告编号") == report_code), None)
        if report is None:
            report = {
                "id": max((int(row.get("id", 0)) for row in reports), default=0) + 1,
                "status": "草稿",
                "pending": False,
                "abnormal": False,
                "报告编号": report_code,
                "报告标题": f"定时任务 {entry.get('任务编号')} 自动生成报告",
                "关联数据集": str(entry.get("输入参数") or "—"),
                "分析周期": "按调度",
                "报告模板": "定时分析模板",
                "生成时间": "待调度",
                "生成人": "调度器",
                "报告状态": "草稿",
                "来源任务": entry.get("任务编号"),
            }
            reports.append(report)
        latest = record.get("runs") or []
        if latest:
            first = latest[0]
            report["计划生成时间"] = first.get("计划开始")
            report["调度预演"] = record.get("summary")
            report["冲突任务"] = first.get("冲突任务") or []
            report["顺延原因"] = first.get("顺延原因")
            report["顺延分钟"] = first.get("顺延分钟", 0)
            report["预演口径"] = f"预演指纹 {record.get('fingerprint')}"
        report["来源任务"] = entry.get("任务编号")
        report["预演状态"] = entry.get("预演状态") or ""
        report["通知渠道"] = entry.get("通知渠道") or ""
        report["输出目标"] = entry.get("输出目标") or ""
        report["队列状态"] = entry.get("队列状态") or "未入队"

    # ---------------------------------------------------------------- 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"定时任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于定时分析任务可执行范围"
        if action in ("提交校验", "启用配置"):
            try:
                self._assert_submittable(entry, require_confirmed=action == "启用配置")
            except ScheduleRuleError as exc:
                return None, "；".join(exc.reasons)
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["是否启用"] = "是" if target == "已启用" else "否"
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS or bool(entry.get("异常原因"))
        if target == "已启用":
            self._enqueue(entry)
            return entry, f"定时任务已{action}并进入调度队列（{entry.get('预演摘要')}）"
        if target == "已停用":
            self._dequeue(entry)
        return entry, f"定时任务已{action}"

    def _assert_submittable(self, entry: dict[str, Any], *, require_confirmed: bool) -> None:
        validation = self.validate(entry)
        reasons = list(validation["reasons"])
        if require_confirmed:
            record = entry.get("预演")
            if not record or not record.get("ok"):
                reasons.append("尚未生成窗口冲突预演，不能启用")
            elif not record.get("confirmed"):
                reasons.append("预演结果尚未确认，请先确认顺延方案")
            else:
                task = build_task(entry, task_id=int(entry["id"]), code=str(entry.get("任务编号")))
                if task.fingerprint() != record.get("fingerprint"):
                    reasons.append("调度配置在预演后发生变更，请重新预演")
                if record.get("max_delay_minutes", 0) > MAX_DELAY_MINUTES:
                    reasons.append(
                        f"预演最大顺延 {record['max_delay_minutes']} 分钟，超过允许上限 {MAX_DELAY_MINUTES} 分钟"
                    )
        if reasons:
            raise ScheduleRuleError(reasons)

    def _enqueue(self, entry: dict[str, Any]) -> None:
        record = copy.deepcopy(entry.get("预演") or {})
        record["queue_status"] = "已入队"
        record["queued_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["预演"] = record
        entry["队列状态"] = "已入队"
        self._sync_report_preview(entry, record)

    def _dequeue(self, entry: dict[str, Any]) -> None:
        entry["队列状态"] = "已移出队列"
        record = entry.get("预演") or {}
        if record:
            record["queue_status"] = "已移出队列"
        report_code = str(entry.get("报告编号") or "")
        for report in store.rows(REPORT_MODULE):
            if report.get("报告编号") == report_code:
                report["预演状态"] = entry.get("预演状态") or ""
                report["队列状态"] = "已移出队列"

    def _sync_tasks(self, record: dict[str, Any]) -> None:
        """把预演按任务分组保存，实际执行时按任务推进阶段。"""
        tasks: list[dict[str, Any]] = []
        candidate_runs = record.get("runs") or []
        if candidate_runs:
            measured = self._measure_runs(candidate_runs)
            tasks.append({
                "任务编号": record.get("任务编号"),
                "任务名称": record.get("任务名称"),
                "报告编号": record.get("报告编号"),
                "阶段": {stage: "待执行" for stage in STAGES},
                "已完成阶段": [],
                "当前阶段": STAGES[0],
                "runs": measured,
            })
        for item in record.get("affected_reports") or []:
            if item.get("任务编号") == record.get("任务编号"):
                continue
            tasks.append({
                "任务编号": item.get("任务编号"),
                "任务名称": item.get("任务名称"),
                "报告编号": item.get("报告编号"),
                "阶段": {stage: "待执行" for stage in STAGES},
                "已完成阶段": [],
                "当前阶段": STAGES[0],
                "runs": [{
                    "序号": 1,
                    "任务编号": item.get("任务编号"),
                    "任务名称": item.get("任务名称"),
                    "计划开始": item.get("计划生成时间"),
                    "计划结束": item.get("计划结束"),
                    "顺延分钟": item.get("顺延分钟", 0),
                    "冲突任务": item.get("冲突任务") or [],
                    "顺延原因": item.get("顺延原因"),
                    "已执行": False,
                    "阶段": {stage: "待执行" for stage in STAGES},
                    "当前阶段": STAGES[0],
                }],
            })
        record["tasks"] = tasks

    def _measure_runs(self, source: list[dict[str, Any]]) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for item in source:
            runs.append({
                "序号": item.get("序号"),
                "任务编号": item.get("任务编号"),
                "任务名称": item.get("任务名称"),
                "计划开始": item.get("计划开始"),
                "计划结束": item.get("计划结束"),
                "顺延分钟": item.get("顺延分钟", 0),
                "冲突任务": item.get("冲突任务") or [],
                "顺延原因": item.get("顺延原因"),
                "跨午夜": item.get("跨午夜", False),
                "已执行": False,
                "阶段": {stage: "待执行" for stage in STAGES},
                "当前阶段": STAGES[0],
            })
        return runs

    # ---------------------------------------------------------------- 运行时
    def _run_row(self, entry_id: int, run_seq: int) -> dict[str, Any] | None:
        for row in store.rows(RUN_MODULE):
            if int(row.get("task_id", 0)) == entry_id and int(row.get("run_seq", 0)) == run_seq:
                return row
        return None

    def _new_run_row(self, entry: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(RUN_MODULE)
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "task_id": int(entry["id"]),
            "任务编号": entry.get("任务编号"),
            "任务名称": entry.get("任务名称"),
            "run_seq": int(run.get("序号") or 1),
            "计划开始": run.get("计划开始"),
            "计划结束": run.get("计划结束"),
            "阶段": {stage: "待执行" for stage in STAGES},
            "当前阶段": STAGES[0],
            "已完成阶段": [],
            "状态": "待执行",
            "尝试次数": 0,
            "实际开始": None,
            "实际结束": None,
            "异常原因": "",
            "事件": [],
        }
        rows.append(row)
        return row

    def _runs_for(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        record = entry.get("预演") or {}
        for task in record.get("tasks", []):
            if task.get("任务编号") == entry.get("任务编号"):
                return task.get("runs", [])
        return []

    def execute_run(
        self,
        entry_id: int,
        *,
        run_seq: int = 1,
        fail_at: str | None = None,
        actual_start: str | None = None,
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """推进一次运行。`fail_at` 用于复现某阶段失败；失败即停在该阶段。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"定时任务 {entry_id} 不存在或已归档"
        if entry.get("status") != "已启用":
            return None, "任务未启用，不能执行调度运行"
        runs = self._runs_for(entry)
        target = next((run for run in runs if int(run.get("序号") or 0) == run_seq), None)
        if target is None:
            return None, f"预演中没有第 {run_seq} 次运行，请重新预演"
        row = self._run_row(entry_id, run_seq) or self._new_run_row(entry, target)
        if row["状态"] == "已完成":
            return row, "该次运行已完成，无需重复执行"
        if fail_at is not None and fail_at not in STAGES:
            return None, f"失败阶段「{fail_at}」不在允许阶段：{'、'.join(STAGES)}"

        attempt = int(row.get("尝试次数", 0)) + 1
        row["尝试次数"] = attempt
        row["实际开始"] = row.get("实际开始") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resumed_from = row.get("当前阶段") or STAGES[0]
        row["失败阶段"] = None
        row["状态"] = "执行中"
        row["续跑"] = attempt > 1
        row["事件"].append({
            "时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "说明": note or ("失败后续跑" if attempt > 1 else "开始执行"),
            "从阶段": resumed_from,
        })

        # 实际开始晚于计划：后续运行与报告队列同步顺延，口径与预演一致。
        delay = self._delay_minutes(target.get("计划开始"), actual_start)
        if delay > 0:
            self._apply_delay(entry, delay, skip_seq=run_seq)

        while row.get("当前阶段") in STAGES:
            stage = row["当前阶段"]
            if fail_at == stage:
                row["阶段"][stage] = "失败"
                row["状态"] = "失败"
                row["异常原因"] = f"{stage}阶段执行失败，已保留此前完成的阶段"
                row["失败阶段"] = stage
                entry["abnormal"] = True
                entry["异常原因"] = row["异常原因"]
                done = "、".join(row["已完成阶段"]) or "无"
                return row, f"{stage}失败：已完成 {done}；下次从 {stage} 继续"
            row["阶段"][stage] = "已完成"
            if stage not in row["已完成阶段"]:
                row["已完成阶段"].append(stage)
            row["当前阶段"] = self._next_stage(stage)

        row["状态"] = "已完成"
        row["实际结束"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row["异常原因"] = ""
        entry["abnormal"] = False
        entry["异常原因"] = ""
        self._refresh_reports(entry, row)
        self._complete_preview_run(entry, run_seq, row)
        return row, f"运行完成：{'、'.join(row['已完成阶段'])}"

    def _next_stage(self, stage: str) -> str | None:
        index = STAGES.index(stage)
        return STAGES[index + 1] if index + 1 < len(STAGES) else None

    def _delay_minutes(self, planned_display: str | None, actual_start: str | None) -> int:
        if not actual_start or not planned_display:
            return 0
        try:
            planned = datetime.strptime(str(planned_display).split(" (")[0], "%Y-%m-%d %H:%M")
            actual = datetime.strptime(str(actual_start).strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return 0
        return max(0, int((actual - planned).total_seconds() // 60))

    def _apply_delay(self, entry: dict[str, Any], delay: int, *, skip_seq: int | None = None) -> None:
        """实际运行晚于计划：预演记录与报告队列一起顺延，二者取同一份数据。"""
        record = entry.get("预演") or {}
        shift_runs(record, delay, skip_seq=skip_seq)
        entry["队列顺延"] = f"实际延迟 {delay} 分钟，报告队列同步顺延"
        entry["预演"] = record
        for task in record.get("tasks", []):
            report_code = str(task.get("报告编号") or "")
            if not report_code:
                continue
            next_run = next(
                (run for run in task.get("runs", []) if not run.get("已执行")),
                None,
            )
            if next_run is None:
                continue
            for report in store.rows(REPORT_MODULE):
                if report.get("报告编号") != report_code:
                    continue
                report["计划生成时间"] = next_run.get("计划开始")
                report["计划结束"] = next_run.get("计划结束")
                report["顺延分钟"] = next_run.get("顺延分钟", 0)
                report["顺延原因"] = next_run.get("顺延原因")
                report["队列顺延"] = f"实际延迟 {delay} 分钟，队列同步顺延"
                report["预演口径"] = f"预演指纹 {record.get('fingerprint')}"

    def _refresh_reports(self, entry: dict[str, Any], row: dict[str, Any]) -> None:
        """报告完成后回写实际生成时间；队列里下一次的计划时间保持与预演一致。"""
        report_code = str(entry.get("报告编号") or "")
        record = entry.get("预演") or {}
        for report in store.rows(REPORT_MODULE):
            if report.get("报告编号") != report_code:
                continue
            report["生成时间"] = row.get("实际结束") or row.get("实际开始")
            report["报告状态"] = "已生成"
            report["status"] = "已生成"
            report["pending"] = False
            report["abnormal"] = False
            report["已完成阶段"] = ";".join(row.get("已完成阶段") or [])
            report["调度预演"] = record.get("summary")
            next_run = next(
                (run for run in self._runs_for(entry) if not run.get("已执行")),
                None,
            )
            if next_run is not None:
                report["下次计划生成时间"] = next_run.get("计划开始")
                report["计划结束"] = next_run.get("计划结束")
                report["顺延分钟"] = next_run.get("顺延分钟", 0)
                report["顺延原因"] = next_run.get("顺延原因")

    def _complete_preview_run(self, entry: dict[str, Any], run_seq: int, row: dict[str, Any]) -> None:
        record = entry.get("预演") or {}
        for task in record.get("tasks", []):
            for run in task.get("runs", []):
                if int(run.get("序号") or 0) == run_seq and task.get("任务编号") == entry.get("任务编号"):
                    run["已执行"] = True
                    run["阶段"] = dict(row.get("阶段") or {})
                    run["当前阶段"] = row.get("当前阶段")
        entry["预演"] = record

    def get_runs(self, entry_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(RUN_MODULE) if int(row.get("task_id", 0)) == entry_id]

    def queue(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """调度队列：所有已启用任务的首个待执行运行，按计划开始排序。"""
        now = now or _now()
        queue: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            if entry.get("status") != "已启用":
                continue
            record = entry.get("预演") or {}
            for task in record.get("tasks", []):
                for run in task.get("runs", []):
                    if run.get("已执行") or int(run.get("序号") or 0) != 1:
                        continue
                    queue.append({
                        "任务编号": task.get("任务编号"),
                        "任务名称": task.get("任务名称"),
                        "报告编号": task.get("报告编号"),
                        "计划开始": run.get("计划开始"),
                        "计划结束": run.get("计划结束"),
                        "顺延分钟": run.get("顺延分钟", 0),
                        "冲突任务": run.get("冲突任务") or [],
                        "顺延原因": run.get("顺延原因"),
                        "队列状态": entry.get("队列状态") or record.get("queue_status") or "未入队",
                    })
        return queue
