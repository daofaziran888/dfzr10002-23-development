import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/report'
const columns = [
  "报告编号",
  "报告标题",
  "关联数据集",
  "分析周期",
  "报告模板",
  "生成时间",
  "生成人",
  "报告状态",
  "计划生成时间",
  "顺延分钟",
  "预演状态",
  "队列状态"
]
const actions = [
  "生成报告",
  "归档报告",
  "删除报告"
]
const statuses = [
  "草稿",
  "生成中",
  "已生成",
  "已归档"
]
const stats = [
  {
    "label": "分析报告总数",
    "value": 0
  },
  {
    "label": "已生成报告",
    "value": 0
  },
  {
    "label": "已归档报告",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ConflictHit = { 任务编号?: string; 任务名称?: string; 窗口?: string; 重叠分钟?: number }

export default function ReportView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('分析报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析报告管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('分析报告管理动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析报告管理操作失败')
    }
  }

  return (
    <section className="page" data-module="report">
      <header className="page-head">
        <div>
          <h2>分析报告管理管理</h2>
          <p className="page-desc">
            维护分析报告的模板配置、周期生成与分发管理。定时任务的预演结果在这里同步：
            「计划生成时间」「顺延分钟」与「预演状态」直接取自调度预演，实际运行延迟后队列同步顺延。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分析报告登记入口尚未接入审批流')}>
            登记分析报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析报告管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

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
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => {
                const value = row[column] ?? '—'
                if (column === "计划生成时间" && row["顺延分钟"]) {
                  return (
                    <td key={column} title={String(row["顺延原因"] ?? '')}>
                      {value}
                      {Number(row["顺延分钟"]) > 0 && <span className="badge warn">+{row["顺延分钟"]}</span>}
                    </td>
                  )
                }
                return <td key={column} title={column === "预演状态" || column === "队列状态" ? String(row["调度预演"] ?? '') : undefined}>{value}</td>
              })}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析报告管理数据，可先登记分析报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="preview">
        <h3>来自定时任务的报告（预演同步）</h3>
        <p className="muted">
          下列报告的计划生成时间、冲突任务与顺延原因与调度预演逐字一致；任务实际运行延迟后，
          报告队列的「计划生成时间」同步顺延，预演指纹保持不变以便核对。
        </p>
        <table className="data-table">
          <thead>
            <tr>
              <th>报告编号</th><th>来源任务</th><th>计划生成时间</th><th>顺延分钟</th>
              <th>冲突任务</th><th>顺延原因</th><th>预演口径</th><th>预演状态</th>
            </tr>
          </thead>
          <tbody>
            {rows.filter((row) => row["来源任务"]).map((row) => (
              <tr key={`sync-${String(row.id)}`}>
                <td>{row["报告编号"]}</td>
                <td>{row["来源任务"]}</td>
                <td>
                  {row["计划生成时间"] ?? '—'}
                  {row["队列顺延"] ? <div className="muted">{row["队列顺延"]}</div> : null}
                </td>
                <td>{Number(row["顺延分钟"] ?? 0) > 0 ? `+${row["顺延分钟"]}` : '—'}</td>
                <td>
                  {Array.isArray(row["冲突任务"]) && (row["冲突任务"] as ConflictHit[]).length > 0
                    ? (row["冲突任务"] as ConflictHit[]).map((hit, index) => (
                        <div key={`${hit.任务编号}-${index}`}>
                          {hit.任务编号} {hit.任务名称}（{hit.窗口}，重叠 {hit.重叠分钟} 分钟）
                        </div>
                      ))
                    : '—'}
                </td>
                <td className="muted">{String(row["顺延原因"] ?? '—')}</td>
                <td className="muted">{String(row["预演口径"] ?? '—')}</td>
                <td>{String(row["预演状态"] ?? '—')}</td>
              </tr>
            ))}
            {rows.filter((row) => row["来源任务"]).length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无关联定时任务的报告</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条分析报告管理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
