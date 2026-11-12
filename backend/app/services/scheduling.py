"""调度窗口预演引擎：把 cron 表达式展开成绝对时间窗口，并按优先级解决冲突。

业务口径（本模块唯一权威定义）：

* **时区**：每条任务带自己的 IANA 时区；所有窗口先换算成 UTC 绝对时间再比较，
  因此跨午夜、跨时区互不干扰。展示时换回任务自己的时区，同时给出 UTC 偏移。
* **优先级**：`P1` > `P2` > `P3`。数值越小越优先，由业务登记时指定；
  同级任务按「任务编号升序」定先后，编号小的优先，保证结果可复现。
* **执行窗口**：任务从开始时刻起独占 `执行窗口` 分钟。两个窗口的重叠分钟数
  **超过双方冲突阈值中更严格的那个** 即视为冲突（阈值 0 表示窗口不能重叠）。
* **顺延**：优先级低的任务让路，开始时间顺延到「冲突窗口结束时刻减去允许重叠」
  之后，重新检查直到无冲突；高优先级任务的时间不变。
* **跨午夜**：cron 按任务时区的本地日历展开，23:50 + 30 分钟会自然落到
  次日 00:20，不按自然日截断。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2}
STAGES = ["数据准备", "生成报告", "分发通知"]
MAX_PLAN_MINUTES = 7 * 24 * 60
MAX_ITERATIONS = 24
DEFAULT_TZ = "Asia/Shanghai"


class CronError(ValueError):
    """调度表达式无法解析时抛出，调用方转成可读的校验失败原因。"""


@dataclass(frozen=True)
class CronField:
    values: frozenset[int]
    is_wildcard: bool

    def matches(self, value: int) -> bool:
        return self.is_wildcard or value in self.values


_INT_RE = re.compile(r"^\d+$")


def _parse_int(token: str, low: int, high: int) -> int:
    if not _INT_RE.match(token):
        raise CronError(f"「{token}」不是合法数字")
    value = int(token)
    if not low <= value <= high:
        raise CronError(f"「{token}」超出允许范围 {low}-{high}")
    return value


def _parse_field(raw: str, low: int, high: int, *, dow: bool = False) -> CronField:
    raw = raw.strip()
    if not raw:
        raise CronError("字段不能为空")
    if raw == "*":
        return CronField(frozenset(), True)
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError("列表中存在空项")
        step = 1
        if "/" in part:
            base, step_raw = part.split("/", 1)
            step = _parse_int(step_raw, 1, high)
            if step < 1:
                raise CronError(f"步长「{step_raw}」必须大于 0")
            part = base
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_raw, end_raw = part.split("-", 1)
            start, end = _parse_int(start_raw, low, high), _parse_int(end_raw, low, high)
            if start > end:
                raise CronError(f"区间「{part}」起点大于终点")
        else:
            start = end = _parse_int(part, low, high)
        values.update(range(start, end + 1, step))
    if dow:
        # cron 惯例：0 和 7 都表示周日。这里统一收敛到 0-6（0=周日）。
        values = {0 if value == 7 else value for value in values}
        if any(value > 6 for value in values):
            raise CronError("星期字段只允许 0-7（0/7 表示周日）")
    return CronField(frozenset(values), False)


@dataclass(frozen=True)
class CronExpression:
    """标准五段 cron：分 时 日 月 周。支持 `*`、列表、区间与 `/步长`。"""

    raw: str
    minute: CronField
    hour: CronField
    day_of_month: CronField
    month: CronField
    day_of_week: CronField

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        expression = (expression or "").strip()
        fields = expression.split()
        if len(fields) != 5:
            raise CronError("调度表达式必须是 5 段：分 时 日 月 周")
        minute = _parse_field(fields[0], 0, 59)
        hour = _parse_field(fields[1], 0, 23)
        dom = _parse_field(fields[2], 1, 31)
        month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7, dow=True)
        return cls(expression, minute, hour, dom, month, dow)

    def matches_date(self, day: date) -> bool:
        if not self.month.matches(day.month):
            return False
        dom_match = self.day_of_month.matches(day.day)
        # 标准 cron：日与周同时受限时取「或」；任一为通配则取「与」。
        cron_dow = (day.weekday() + 1) % 7  # Python Monday=0 -> cron Sunday=0
        dow_match = self.day_of_week.matches(cron_dow)
        if not self.day_of_month.is_wildcard and not self.day_of_week.is_wildcard:
            return dom_match or dow_match
        return dom_match and dow_match


def parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or DEFAULT_TZ)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - 依赖系统 tzdata
        raise CronError(f"时区「{name}」不可识别，请使用 IANA 名称，例如 Asia/Shanghai") from exc


def _local_candidates(
    expression: CronExpression,
    tz: ZoneInfo,
    start: datetime,
    horizon_minutes: int,
) -> list[datetime]:
    """在任务时区的本地日历里展开候选时刻，返回 UTC aware datetime。"""
    local_start = start.astimezone(tz)
    local_end = (start + timedelta(minutes=horizon_minutes)).astimezone(tz)
    candidates: list[datetime] = []
    first_day = local_start.date()
    last_day = local_end.date()
    days = (last_day - first_day).days + 1
    for offset in range(days + 1):
        day = first_day + timedelta(days=offset)
        if not expression.matches_date(day):
            continue
        for hour in sorted(expression.hour.values) if not expression.hour.is_wildcard else range(24):
            if not expression.hour.matches(hour):
                continue
            for minute in sorted(expression.minute.values) if not expression.minute.is_wildcard else range(60):
                if not expression.minute.matches(minute):
                    continue
                local_dt = datetime.combine(day, time(hour, minute), tzinfo=tz)
                # 夏令时跳过/重复的时刻按 fold 规则归一成真实绝对时间。
                utc_dt = local_dt.astimezone(timezone.utc)
                if start <= utc_dt <= start + timedelta(minutes=horizon_minutes):
                    candidates.append(utc_dt)
    return sorted(set(candidates))


def next_occurrences(
    expression: CronExpression,
    tz: ZoneInfo,
    *,
    after: datetime,
    count: int,
    horizon_minutes: int = MAX_PLAN_MINUTES,
) -> list[datetime]:
    """返回 `after` 之后（含）最近 `count` 个绝对时间点。"""
    horizon = max(horizon_minutes, 60)
    while horizon <= 400 * 24 * 60:
        found = _local_candidates(expression, tz, after, horizon)
        if len(found) >= count:
            return found[:count]
        horizon *= 2
    return _local_candidates(expression, tz, after, 400 * 24 * 60)


@dataclass
class TaskSpec:
    """一条排入时间线的任务（候选任务或已启用任务，结构一致）。"""

    task_id: int
    code: str
    name: str
    analysis_type: str
    expression: str
    timezone_name: str
    window_minutes: int
    conflict_threshold: int
    priority: str
    output_target: str
    notification_channel: str
    report_code: str = ""
    cron: CronExpression = field(init=False)
    tz: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        self.cron = CronExpression.parse(self.expression)
        self.tz = parse_timezone(self.timezone_name)

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, 9)

    @property
    def precedence_key(self) -> tuple[int, int]:
        """数值越小优先级越高；同级按任务编号升序。"""
        return (self.priority_rank, self.task_id)

    def occurrences(self, after: datetime, count: int) -> list[datetime]:
        return next_occurrences(self.cron, self.tz, after=after, count=count)

    def fingerprint(self) -> str:
        payload = {
            "分析类型": self.analysis_type,
            "调度表达式": self.expression,
            "调度时区": self.timezone_name,
            "执行窗口": self.window_minutes,
            "冲突阈值": self.conflict_threshold,
            "优先级": self.priority,
            "输出目标": self.output_target,
            "通知渠道": self.notification_channel,
            "报告编号": self.report_code,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Interval:
    task: TaskSpec
    base_start: datetime
    start: datetime
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.task.window_minutes)

    def overlap_minutes(self, other: "Interval") -> int:
        delta = min(self.end, other.end) - max(self.start, other.start)
        return max(0, int(delta.total_seconds() // 60))

    def allowed_overlap(self, other: "Interval") -> int:
        """冲突以双方更严格的阈值判定。"""
        return min(self.task.conflict_threshold, other.task.conflict_threshold)

    def conflicts_with(self, other: "Interval") -> bool:
        return self.overlap_minutes(other) > self.allowed_overlap(other)


def _format_local(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _format_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe_window(moment: datetime, task: TaskSpec) -> dict[str, str]:
    return {
        "start_iso": _format_iso(moment),
        "start_local": _format_local(moment, task.tz),
        "display": f"{_format_local(moment, task.tz)} ({task.timezone_name})",
    }


def _place_with_reasons(interval: Interval, placed: list[Interval]) -> Interval:
    """把单个窗口放到时间线上；与已放置窗口冲突则顺延，并记录每次冲突。"""
    base = interval.base_start
    current = base
    conflicts: list[dict[str, Any]] = interval.conflicts
    for _ in range(MAX_ITERATIONS):
        trial = Interval(task=interval.task, base_start=base, start=current)
        blockers = [item for item in placed if trial.conflicts_with(item)]
        if not blockers:
            return Interval(task=interval.task, base_start=base, start=current, conflicts=conflicts)
        # 与多个窗口冲突时，退到「最晚结束时刻 - 允许重叠分钟」，再重新检查。
        allowed = min(
            interval.task.conflict_threshold,
            min(item.task.conflict_threshold for item in blockers),
        )
        latest_end = max(item.end for item in blockers)
        next_start = latest_end - timedelta(minutes=allowed)
        if next_start <= current:
            next_start = current + timedelta(minutes=1)
        for item in blockers:
            conflicts.append({
                "任务编号": item.task.code,
                "任务名称": item.task.name,
                "优先级": item.task.priority,
                "窗口": f"{_format_local(item.start, item.task.tz)}–{_format_local(item.end, item.task.tz)}",
                "重叠分钟": trial.overlap_minutes(item),
                "顺延到": describe_window(next_start, interval.task)["display"],
            })
        current = next_start
    conflicts.append({"说明": "顺延次数超过上限，未能排入可用窗口"})
    return Interval(task=interval.task, base_start=base, start=current, conflicts=conflicts)


@dataclass
class PlannedRun:
    seq: int
    task: TaskSpec
    base_start: datetime
    start: datetime
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def delay_minutes(self) -> int:
        return max(0, int((self.start - self.base_start).total_seconds() // 60))

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.task.window_minutes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "序号": self.seq,
            "任务编号": self.task.code,
            "任务名称": self.task.name,
            "优先级": self.task.priority,
            "原定开始": describe_window(self.base_start, self.task)["display"],
            "计划开始": describe_window(self.start, self.task)["display"],
            "计划结束": describe_window(self.end, self.task)["display"],
            "顺延分钟": self.delay_minutes,
            "跨午夜": self.start.astimezone(self.task.tz).date()
            != self.base_start.astimezone(self.task.tz).date(),
            "冲突任务": self.conflicts,
            "顺延原因": self.reason(),
        }

    def reason(self) -> str:
        if self.delay_minutes == 0:
            return "无需顺延"
        parts = [
            f"与《{item.get('任务名称')}》({item.get('任务编号')}) 窗口 {item.get('窗口')} "
            f"重叠 {item.get('重叠分钟')} 分钟，超出冲突阈值，顺延至 {item.get('顺延到')}"
            for item in self.conflicts
            if item.get("任务编号")
        ]
        return "；".join(parts) or f"顺延 {self.delay_minutes} 分钟"


@dataclass
class TimelinePlan:
    candidate: TaskSpec
    runs: list[PlannedRun]
    shifted: list[PlannedRun]
    placed: list[Interval]

    @property
    def has_conflict(self) -> bool:
        return bool(self.runs and any(run.delay_minutes for run in self.runs)) or bool(self.shifted)


def plan_timeline(
    candidate: TaskSpec,
    existing: Iterable[TaskSpec],
    *,
    now: datetime,
    count: int = 5,
) -> TimelinePlan:
    """把候选任务与已启用任务放进同一条时间线，按优先级解决冲突。

    高优先级任务保持原定时间，低优先级任务顺延；同优先级按任务编号升序定先后。
    """
    events: list[tuple[TaskSpec, datetime]] = []
    for task in existing:
        for moment in task.occurrences(now, count):
            events.append((task, moment))
    for moment in candidate.occurrences(now, count):
        events.append((candidate, moment))

    ordered = sorted(
        events,
        key=lambda item: (item[0].priority_rank, item[0].task_id, item[1]),
    )
    placed: list[Interval] = []
    for task, moment in ordered:
        interval = _place_with_reasons(
            Interval(task=task, base_start=moment, start=moment), placed
        )
        placed.append(interval)

    runs = [
        PlannedRun(seq=0, task=item.task, base_start=item.base_start,
                   start=item.start, conflicts=item.conflicts)
        for item in placed
        if item.task is candidate
    ]
    runs.sort(key=lambda item: item.base_start)
    for index, run in enumerate(runs, start=1):
        run.seq = index
    shifted = [
        PlannedRun(seq=0, task=item.task, base_start=item.base_start,
                   start=item.start, conflicts=item.conflicts)
        for item in placed
        if item.task is not candidate and item.start != item.base_start
    ]
    shifted.sort(key=lambda item: item.start)
    return TimelinePlan(candidate=candidate, runs=runs, shifted=shifted, placed=placed)


def projected_report_times(plan: TimelinePlan) -> list[dict[str, Any]]:
    """按任务汇总预演后的报告生成时间，供报告清单跨模块同步。

    每个任务取**最近一次**运行（最早的计划开始）作为报告清单上的「计划生成时间」，
    冲突与顺延原因同预演结果逐字一致。
    """
    groups: dict[str, dict[str, Any]] = {}
    for run in list(plan.runs) + list(plan.shifted):
        if not run.task.report_code:
            continue
        group = groups.setdefault(run.task.task_id, {
            "任务编号": run.task.code,
            "任务名称": run.task.name,
            "报告编号": run.task.report_code,
            "报告标题": run.task.analysis_type,
            "优先级": run.task.priority,
            "_first": None,
        })
        if group["_first"] is not None and run.start >= group["_first"]:
            continue
        group.update({
            "计划生成时间": describe_window(run.start, run.task)["display"],
            "计划结束": describe_window(run.end, run.task)["display"],
            "顺延分钟": run.delay_minutes,
            "顺延原因": run.reason(),
            "冲突任务": run.conflicts,
            "原定开始": describe_window(run.base_start, run.task)["display"],
            "_first": run.start,
        })
    for group in groups.values():
        group.pop("_first", None)
    return list(groups.values())


def shift_runs(
    record: dict[str, Any],
    delay_minutes: int,
    *,
    skip_seq: int | None = None,
) -> None:
    """实际运行延迟后，把预演记录里的后续运行整体顺延同样的分钟数。"""
    if delay_minutes <= 0:
        return
    for task in record.get("tasks", []):
        shifted = 0
        for run in task.get("runs", []):
            if run.get("已执行"):
                continue
            if skip_seq is not None and int(run.get("序号") or 0) == skip_seq \
                    and task.get("任务编号") == record.get("任务编号"):
                continue
            run["计划开始"] = _shift_display(run.get("计划开始", ""), delay_minutes)
            run["计划结束"] = _shift_display(run.get("计划结束", ""), delay_minutes)
            if run.get("原定开始"):
                run["原定开始"] = _shift_display(run["原定开始"], delay_minutes)
            run["顺延分钟"] = int(run.get("顺延分钟", 0)) + delay_minutes
            notes = run.setdefault("附加顺延原因", [])
            notes.append(f"实际运行延迟 {delay_minutes} 分钟，队列同步顺延")
            shifted += 1
        if shifted:
            task["顺延说明"] = f"实际运行延迟 {delay_minutes} 分钟，后续 {shifted} 次运行同步顺延"


def _shift_display(display: str, minutes: int) -> str:
    """把 `2026-11-12 00:20 (Asia/Shanghai)` 这类展示串整体平移。"""
    if not display:
        return display
    if " (" not in display:
        return display
    stamp, zone = display.split(" (", 1)
    zone = zone.rstrip(")")
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except ValueError:
        return display
    shifted = moment + timedelta(minutes=minutes)
    return f"{shifted.strftime('%Y-%m-%d %H:%M')} ({zone})"


def parse_clock(value: str | None, tz_name: str) -> datetime | None:
    """解析 `YYYY-MM-DD HH:MM`（按任务时区理解）；空值返回 None。"""
    if not value:
        return None
    try:
        tz = parse_timezone(tz_name)
        moment = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except (ValueError, CronError):
        return None
    return moment.replace(tzinfo=tz).astimezone(timezone.utc)
