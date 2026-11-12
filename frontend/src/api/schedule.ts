/** 定时调度相关接口的类型与请求封装，供任务页与报告页共用。 */

export type RuleCheck = {
  ok: boolean
  reasons: string[]
  warnings: string[]
  standards: Record<string, string>
}

export type ConflictHit = {
  任务编号?: string
  任务名称?: string
  优先级?: string
  窗口?: string
  重叠分钟?: number
  顺延到?: string
  受影响任务?: string
  原定开始?: string
  顺延后开始?: string
  说明?: string
}

export type PlannedRun = {
  序号: number
  任务编号: string
  任务名称: string
  优先级: string
  原定开始: string
  计划开始: string
  计划结束: string
  顺延分钟: number
  跨午夜: boolean
  冲突任务: ConflictHit[]
  顺延原因: string
}

export type AffectedReport = {
  任务编号: string
  任务名称: string
  报告编号: string
  报告标题: string
  优先级: string
  计划生成时间: string
  计划结束: string
  顺延分钟: number
  顺延原因: string
  冲突任务: ConflictHit[]
  原定开始: string
}

export type PreviewTask = {
  任务编号: string
  任务名称: string
  报告编号: string
  阶段: Record<string, string>
  已完成阶段: string[]
  当前阶段: string | null
  runs: Array<{
    序号: number
    计划开始: string
    计划结束: string
    顺延分钟: number
    冲突任务: ConflictHit[]
    顺延原因: string
    已执行?: boolean
    阶段?: Record<string, string>
    附加顺延原因?: string[]
  }>
}

export type PreviewRecord = {
  任务编号: string
  任务名称: string
  分析类型: string
  输出目标: string
  通知渠道: string
  报告编号: string
  优先级: string
  调度时区: string
  执行窗口: number
  冲突阈值: number
  调度表达式: string
  预演起点: string
  ok: boolean
  has_conflict: boolean
  rules: RuleCheck
  summary: string
  max_delay_minutes: number
  runs: PlannedRun[]
  conflicts: ConflictHit[]
  affected_reports: AffectedReport[]
  shifted_tasks: Array<{
    任务编号: string
    任务名称: string
    原定开始: string
    顺延后开始: string
    顺延分钟: number
  }>
  tasks: PreviewTask[]
  fingerprint: string
  confirmed: boolean
  试算?: boolean
  message?: string
}

export type RunRecord = {
  id: number
  任务编号: string
  任务名称: string
  run_seq: number
  计划开始: string
  计划结束: string
  阶段: Record<string, string>
  当前阶段: string | null
  已完成阶段: string[]
  状态: string
  尝试次数: number
  实际开始: string | null
  实际结束: string | null
  异常原因: string
  失败阶段?: string | null
  续跑?: boolean
  事件: Array<{ 时间: string; 说明: string; 从阶段: string }>
}

export type QueueItem = {
  任务编号: string
  任务名称: string
  报告编号: string
  计划开始: string
  计划结束: string
  顺延分钟: number
  冲突任务: ConflictHit[]
  顺延原因: string
  队列状态: string
}

export type ScheduleRules = {
  执行窗口分钟: [number, number]
  冲突阈值分钟: [number, number]
  优先级: string[]
  分析类型: string[]
  通知渠道: string[]
  预演次数: number
  允许最大顺延分钟: number
  运行阶段: string[]
  时区: string
  允许提交条件: string
}

export const SCHEDULE_STAGES = ['数据准备', '生成报告', '分发通知'] as const
