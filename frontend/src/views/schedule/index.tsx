import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import {
  SCHEDULE_STAGES,
  type PreviewRecord,
  type QueueItem,
  type RunRecord,
  type ScheduleRules,
} from '@/api/schedule'

const ENDPOINT = '/api/schedule'
const columns = [
  "任务编号",
  "任务名称",
  "分析类型",
  "调度表达式",
  "调度时区",
  "执行窗口",
  "优先级",
  "输出目标",
  "通知渠道",
  "预演状态",
  "队列状态",
  "status",
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]

type Row = Record<string, string | number | null>

const EMPTY_FORM = {
  任务编号: '',
  任务名称: '',
  分析类型: '日报分析',
  调度表达式: '50 23 * * *',
  调度时区: 'Asia/Shanghai',
  执行窗口: '30',
  冲突阈值: '0',
  优先级: 'P2',
  输入参数: 'dataset=daily_sales',
  输出目标: '日报看板',
  通知渠道: '邮件',
  报告编号: '',
}

type FormState = Record<keyof typeof EMPTY_FORM, string>

function num(value: string): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export default function ScheduleView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [rules, setRules] = useState<ScheduleRules | null>(null)
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM })
  const [preview, setPreview] = useState<PreviewRecord | null>(null)
  const [previewSource, setPreviewSource] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [failAt, setFailAt] = useState('生成报告')
  const filterFields = useMemo(() => ['任务编号', '任务名称', 'status'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('定时任务列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时分析任务列表读取失败')
    }
  }, [filters])

  const reloadAux = useCallback(async () => {
    try {
      const [rulesResponse, queueResponse] = await Promise.all([
        request(`${ENDPOINT}/rules`),
        request(`${ENDPOINT}/queue`),
      ])
      if (rulesResponse.ok) setRules(await rulesResponse.json())
      if (queueResponse.ok) {
        const payload = await queueResponse.json()
        setQueue(payload.items ?? [])
      }
    } catch {
      /* 辅助信息失败不挡住主列表 */
    }
  }, [])

  useEffect(() => { void reload() }, [reload])
  useEffect(() => { void reloadAux() }, [reloadAux])

  async function loadRuns(entryId: number) {
    try {
      const response = await request(`${ENDPOINT}/${entryId}/runs`)
      if (response.ok) {
        const payload = await response.json()
        setRuns(payload.items ?? [])
      }
    } catch {
      /* 运行记录按需加载，失败时保留上一次 */
    }
  }

  async function previewForm(target: 'form' | 'selected', entryId?: number) {
    setErrorMessage('')
    setNotice('')
    try {
      const values = {
        ...form,
        执行窗口: num(form.执行窗口),
        冲突阈值: num(form.冲突阈值),
        ...(entryId ? { id: entryId } : {}),
      }
      const path = target === 'selected' && entryId
        ? `${ENDPOINT}/${entryId}/preview`
        : `${ENDPOINT}/preview`
      const response = await request(path, { method: 'POST', body: JSON.stringify({ values }) })
      if (!response.ok) throw new Error('预演未生成，请检查调度表达式与规则口径')
      const record: PreviewRecord = await response.json()
      setPreview(record)
      setPreviewSource(target === 'selected' && entryId ? `任务 ${entryId}` : '未保存表单')
      if (!record.ok && record.message) setErrorMessage(record.message)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预演失败')
    }
  }

  async function saveForm() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({
          values: { ...form, 执行窗口: num(form.执行窗口), 冲突阈值: num(form.冲突阈值) },
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message)
        return
      }
      setSelectedId(payload.entry.id)
      setNotice('任务已登记为草稿，下一步执行预演并确认后才可启用')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登记失败')
    }
  }

  async function runPreviewOnRow(row: Row) {
    const entryId = Number(row.id)
    const patch: Partial<FormState> = {
      任务编号: String(row['任务编号'] ?? ''),
      任务名称: String(row['任务名称'] ?? ''),
      分析类型: String(row['分析类型'] ?? ''),
      调度表达式: String(row['调度表达式'] ?? ''),
      调度时区: String(row['调度时区'] ?? ''),
      执行窗口: String(row['执行窗口'] ?? ''),
      冲突阈值: String(row['冲突阈值'] ?? ''),
      优先级: String(row['优先级'] ?? ''),
      输出目标: String(row['输出目标'] ?? ''),
      通知渠道: String(row['通知渠道'] ?? ''),
      报告编号: String(row['报告编号'] ?? ''),
    }
    setForm((prev) => ({ ...prev, ...patch }))
    setSelectedId(entryId)
    await previewForm('selected', entryId)
    await loadRuns(entryId)
  }

  async function confirmPreview() {
    if (!selectedId) {
      setErrorMessage('请先在列表中选择一条已登记任务再确认预演')
      return
    }
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${selectedId}/preview/confirm`, {
        method: 'POST',
        body: JSON.stringify({ remark: '确认顺延方案' }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message)
        return
      }
      setNotice(payload.message)
      await reload()
      await reloadAux()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '确认预演失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message)
        return
      }
      setNotice(payload.message)
      setSelectedId(Number(row.id))
      await reload()
      await reloadAux()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定时分析任务操作失败')
    }
  }

  async function executeRun(runSeq: number, withFailure: boolean) {
    if (!selectedId) {
      setErrorMessage('请先在列表中选择一条已启用任务')
      return
    }
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${selectedId}/runs`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            run_seq: runSeq,
            ...(withFailure ? { fail_at: failAt } : {}),
          },
        }),
      })
      const payload = await response.json()
      if (!payload.ok) setErrorMessage(payload.message)
      else setNotice(payload.message)
      await reload()
      await reloadAux()
      await loadRuns(selectedId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '执行运行失败')
    }
  }

  const ruleStandards = preview?.rules?.standards ?? {}
  const ruleReasons: string[] = preview?.rules?.reasons ?? []
  const ruleWarnings: string[] = preview?.rules?.warnings ?? []

  return (
    <section className="page" data-module="schedule">
      <header className="page-head">
        <div>
          <h2>定时分析任务管理</h2>
          <p className="page-desc">
            调度窗口冲突预演：列出未来运行、冲突任务与顺延原因，确认后才能启用；失败后从失败阶段续跑。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出定时分析任务清单
          </button>
        </div>
      </header>

      {rules && (
        <div className="rule-strip">
          <span>执行窗口 {rules.执行窗口分钟[0]}–{rules.执行窗口分钟[1]} 分钟</span>
          <span>冲突阈值 {rules.冲突阈值分钟[0]}–{rules.冲突阈值分钟[1]} 分钟</span>
          <span>优先级 {rules.优先级.join(' > ')}（同级按任务编号升序）</span>
          <span>预演 {rules.预演次数} 次 · 最大顺延 {rules.允许最大顺延分钟} 分钟</span>
          <span>{rules.时区}</span>
        </div>
      )}

      <div className="panel-grid">
        <article className="panel">
          <h3>登记 / 预演参数</h3>
          <div className="form-grid">
            {(Object.keys(EMPTY_FORM) as Array<keyof FormState>).map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                {field === '分析类型' && rules ? (
                  <select value={form[field]} onChange={(event) => setForm((p) => ({ ...p, [field]: event.target.value }))}>
                    {rules.分析类型.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                ) : field === '优先级' && rules ? (
                  <select value={form[field]} onChange={(event) => setForm((p) => ({ ...p, [field]: event.target.value }))}>
                    {rules.优先级.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                ) : field === '通知渠道' && rules ? (
                  <select value={form[field]} onChange={(event) => setForm((p) => ({ ...p, [field]: event.target.value }))}>
                    {rules.通知渠道.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                ) : (
                  <input
                    value={form[field]}
                    onChange={(event) => setForm((p) => ({ ...p, [field]: event.target.value }))}
                    placeholder={field === '调度表达式' ? '分 时 日 月 周，例如 50 23 * * *' : field}
                  />
                )}
              </label>
            ))}
          </div>
          <div className="page-actions">
            <button className="btn primary" type="button" onClick={() => void previewForm('form')}>预演窗口冲突</button>
            <button className="btn" type="button" onClick={() => void saveForm()}>登记为草稿</button>
            <button className="btn" type="button" onClick={() => void confirmPreview()} disabled={!selectedId}>
              确认预演
            </button>
          </div>
        </article>

        <article className="panel">
          <h3>调度队列（已启用任务）</h3>
          {queue.length === 0 && <p className="empty-state">队列为空：任务需先确认预演再启用。</p>}
          {queue.map((item) => (
            <div key={item.任务编号 + item.计划开始} className="queue-item">
              <div>
                <strong>{item.任务编号}</strong> {item.任务名称}
                <span className="badge">{item.队列状态}</span>
              </div>
              <div className="muted">
                计划 {item.计划开始} → {item.计划结束}
                {item.顺延分钟 > 0 && <span className="warn">（顺延 {item.顺延分钟} 分钟）</span>}
              </div>
              {item.顺延分钟 > 0 && <div className="reason">{item.顺延原因}</div>}
            </div>
          ))}
        </article>
      </div>

      {(ruleReasons.length > 0 || ruleWarnings.length > 0) && (
        <div className="rule-box">
          {ruleReasons.length > 0 && (
            <div>
              <strong>规则校验未通过：</strong>
              <ul>{ruleReasons.map((reason) => <li key={reason} className="error-text">{reason}</li>)}</ul>
            </div>
          )}
          {ruleWarnings.length > 0 && (
            <div>
              <strong>提示：</strong>
              <ul>{ruleWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {preview && (
        <section className="preview">
          <h3>预演结果 · {previewSource}</h3>
          <div className="preview-summary">
            <strong>{preview.ok ? preview.summary : preview.message}</strong>
            <span className="muted">
              任务 {preview.任务编号} · {preview.分析类型} → {preview.输出目标} · 通知 {preview.通知渠道}
            </span>
            <span className="muted">预演起点 {preview.预演起点} · 指纹 {preview.fingerprint}</span>
            {preview.message && !preview.ok && <span className="warn">{preview.message}</span>}
            {(preview as PreviewRecord & { 试算?: boolean }).试算 && <span className="warn">当前为试算结果，保存并重新预演后方可确认</span>}
          </div>

          {preview.ok && (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>序号</th><th>原定开始</th><th>计划开始</th><th>计划结束</th>
                    <th>顺延分钟</th><th>冲突任务</th><th>顺延原因</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.runs.map((run) => (
                    <tr key={run.序号}>
                      <td>{run.序号}</td>
                      <td>{run.原定开始}</td>
                      <td>
                        {run.计划开始}
                        {run.跨午夜 && <span className="badge">跨午夜</span>}
                      </td>
                      <td>{run.计划结束}</td>
                      <td>{run.顺延分钟 > 0 ? `+${run.顺延分钟}` : '—'}</td>
                      <td>
                        {run.冲突任务.length === 0 ? '—' : run.冲突任务.map((hit) => (
                          <div key={`${hit.任务编号}-${hit.窗口}`}>
                            {hit.任务编号} {hit.任务名称}（{hit.优先级}，重叠 {hit.重叠分钟} 分钟）
                          </div>
                        ))}
                      </td>
                      <td className={run.顺延分钟 > 0 ? 'warn' : 'muted'}>{run.顺延原因}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {preview.shifted_tasks.length > 0 && (
                <>
                  <h4>受影响的已排任务</h4>
                  <table className="data-table">
                    <thead><tr><th>任务编号</th><th>任务名称</th><th>原定开始</th><th>顺延后开始</th><th>顺延分钟</th></tr></thead>
                    <tbody>
                      {preview.shifted_tasks.map((item) => (
                        <tr key={item.任务编号 + item.原定开始}>
                          <td>{item.任务编号}</td><td>{item.任务名称}</td>
                          <td>{item.原定开始}</td><td>{item.顺延后开始}</td><td>+{item.顺延分钟}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}

              <h4>受影响分析报告</h4>
              <table className="data-table">
                <thead><tr><th>报告编号</th><th>报告标题</th><th>原定开始</th><th>计划生成时间</th><th>顺延分钟</th><th>顺延原因</th></tr></thead>
                <tbody>
                  {preview.affected_reports.map((item) => (
                    <tr key={item.报告编号}>
                      <td>{item.报告编号}</td><td>{item.报告标题}</td><td>{item.原定开始}</td>
                      <td>{item.计划生成时间}</td><td>{item.顺延分钟 > 0 ? `+${item.顺延分钟}` : '—'}</td>
                      <td className="muted">{item.顺延原因}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                {preview.confirmed ? '预演已确认，可执行「启用配置」进入调度队列。' : '预演尚未确认：确认后才会进入调度队列。'}
              </p>
            </>
          )}

          {ruleStandards && Object.keys(ruleStandards).length > 0 && (
            <details className="standards">
              <summary>本次校验采用的规则口径</summary>
              <ul>{Object.entries(ruleStandards).map(([key, value]) => <li key={key}>{key}：{value}</li>)}</ul>
            </details>
          )}
        </section>
      )}

      {selectedId && (
        <section className="preview">
          <h3>运行与续跑 · 任务 {selectedId}</h3>
          <div className="page-actions">
            <label className="filter-item">
              <span>失败阶段（复现用）</span>
              <select value={failAt} onChange={(event) => setFailAt(event.target.value)}>
                {SCHEDULE_STAGES.map((stage) => <option key={stage} value={stage}>{stage}</option>)}
              </select>
            </label>
            <button className="btn" type="button" onClick={() => void executeRun(1, true)}>执行并注入失败</button>
            <button className="btn primary" type="button" onClick={() => void executeRun(1, false)}>执行 / 从失败处继续</button>
            <button className="btn ghost" type="button" onClick={() => void loadRuns(selectedId)}>刷新运行记录</button>
          </div>
          {runs.length === 0 && <p className="empty-state">尚无运行记录。</p>}
          {runs.map((run) => (
            <div key={run.id} className="run-card">
              <div>
                <strong>第 {run.run_seq} 次运行</strong>
                <span className={`badge ${run.状态 === '失败' ? 'warn' : ''}`}>{run.状态}</span>
                {run.续跑 && <span className="badge">续跑 · 第 {run.尝试次数} 次尝试</span>}
                <span className="muted">计划 {run.计划开始} → {run.计划结束}</span>
              </div>
              <div className="stage-row">
                {SCHEDULE_STAGES.map((stage) => (
                  <span key={stage} className={`stage stage-${run.阶段?.[stage] ?? '待执行'}`}>
                    {stage}：{run.阶段?.[stage] ?? '待执行'}
                  </span>
                ))}
              </div>
              {run.异常原因 && <div className="error-text">{run.异常原因}</div>}
              {run.事件.map((event, index) => (
                <div key={`${event.时间}-${index}`} className="muted">
                  {event.时间} · {event.说明}（从「{event.从阶段}」开始）
                </div>
              ))}
            </div>
          ))}
        </section>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column === 'status' ? '状态' : column}</th>)}
            <th>可执行动作</th>
            <th>预演</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selectedId === Number(row.id) ? 'selected-row' : ''}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void runPreviewOnRow(row)}>预演</button>
                <button className="link" type="button" onClick={() => { setSelectedId(Number(row.id)); void loadRuns(Number(row.id)) }}>
                  运行记录
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无定时分析任务数据，可先登记定时任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条定时分析任务记录</span>
        <span>{notice}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
