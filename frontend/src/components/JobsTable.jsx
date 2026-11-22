import { useState } from 'react'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

const FILTERS = ['', 'ready', 'scheduled', 'queued', 'running', 'succeeded', 'dead', 'cancelled']
const PAGE = 20

export const ago = (iso) => {
  if (!iso) return '–'
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  const fmt = (n) => (n < 60 ? `${n}s` : n < 3600 ? `${Math.floor(n / 60)}m` : `${Math.floor(n / 3600)}h`)
  return s < -1 ? `in ${fmt(-s)}` : `${fmt(Math.max(s, 0))} ago`
}

/** Handler label: python / shell for code tasks, otherwise the handler name. */
const kindOf = (handler, payload) => (handler === 'run_code' ? payload?.language || 'code' : handler)

/**
 * Jobs list. With `showReadyTasks`, task definitions that were created but have never run
 * are listed as "ready" rows at the top, so a newly created task is visible here right away.
 * `task` fixes the list to one task (used on the Tasks page).
 */
export default function JobsTable({ refreshKey, onSelect, selected, task = null, title = 'Jobs',
                                    showReadyTasks = false, onRunTask }) {
  const [status, setStatus] = useState('')
  const [taskFilter, setTaskFilter] = useState('')
  const [page, setPage] = useState(0)
  const effectiveTask = task || taskFilter || null
  const onlyReady = status === 'ready'

  const [data, error] = usePoll(
    () => (onlyReady ? Promise.resolve({ items: [], total: 0 })
      : api.jobs({ status, task: effectiveTask, limit: PAGE, offset: page * PAGE })),
    [status, effectiveTask, page, refreshKey],
  )
  // Task definitions: for the filter dropdown and the never-run "ready" rows.
  const [tasks] = usePoll(() => (showReadyTasks ? api.tasks() : Promise.resolve([])), [showReadyTasks, refreshKey], 3000)

  const ready = (tasks || []).filter((t) => t.run_count === 0 && (!effectiveTask || t.name === effectiveTask))
  const showReady = showReadyTasks && page === 0 && (status === '' || onlyReady)
  const readyRows = showReady ? ready : []
  const total = (data?.total ?? 0) + readyRows.length
  const filters = showReadyTasks ? FILTERS : FILTERS.filter((f) => f !== 'ready')
  const change = (fn) => { fn(); setPage(0) }

  return (
    <section className="card">
      <div className="row between">
        <h3>{title} {data && <span className="muted small">({total})</span>}</h3>
        <div className="row">
          {showReadyTasks && !task && (
            <select className="task-filter" value={taskFilter} onChange={(e) => change(() => setTaskFilter(e.target.value))}
                    aria-label="Filter by task">
              <option value="">all tasks</option>
              {(tasks || []).map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
            </select>
          )}
          {filters.map((f) => (
            <button key={f} className={f === status ? 'tab active' : 'tab'} onClick={() => change(() => setStatus(f))}>
              {f === '' ? 'all' : f === 'dead' ? 'dead-letter' : f}
              {f === 'ready' && ready.length > 0 && <span className="count">{ready.length}</span>}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr><th>ID</th><th>Task</th><th>Type</th><th>Priority</th><th>Status</th><th>Attempts</th><th>Created</th><th>Worker</th></tr>
        </thead>
        <tbody>
          {readyRows.map((t) => (
            <tr key={`task-${t.id}`} className="ready-row">
              <td className="muted">—</td>
              <td><b>{t.name}</b>{t.description && <div className="muted small wrap">{t.description}</div>}</td>
              <td>{kindOf(t.handler, t.default_payload)}</td>
              <td><span className={`pill pr-${t.priority}`}>{t.priority}</span></td>
              <td>
                <span className="pill st-ready">ready</span>
                <span className="muted small"> {t.enabled ? (t.interval_s ? `first run ${ago(t.next_run_at)}` : 'never run') : 'disabled'}</span>
              </td>
              <td>0/{t.max_retries + 1}</td>
              <td className="muted small">{ago(t.created_at)}</td>
              <td>
                {onRunTask && t.enabled && (
                  <button className="small-btn" onClick={() => onRunTask(t)}>▶ Run</button>
                )}
              </td>
            </tr>
          ))}
          {data?.items.map((j) => (
            <tr key={j.id} onClick={() => onSelect(j.id)} className={j.id === selected ? 'selected clickable' : 'clickable'}>
              <td><code>{j.id.slice(0, 8)}</code></td>
              <td>{j.task_name ? <b>{j.task_name}</b> : <span className="muted small">ad-hoc</span>}</td>
              <td>{kindOf(j.type, j.payload)}</td>
              <td><span className={`pill pr-${j.priority}`}>{j.priority}</span></td>
              <td>
                <span className={`pill st-${j.status}`}>{j.status}</span>
                {j.status === 'scheduled' && <span className="muted small"> {ago(j.run_at)}</span>}
              </td>
              <td>{j.attempts}/{j.max_retries + 1}</td>
              <td className="muted small">{ago(j.created_at)}</td>
              <td className="muted small">{j.worker_id || '–'}</td>
            </tr>
          ))}
          {data && total === 0 && (
            <tr><td colSpan={8} className="muted">{onlyReady ? 'Every task has run at least once.' : 'No jobs yet.'}</td></tr>
          )}
        </tbody>
      </table>
      {!onlyReady && data && data.total > PAGE && (
        <div className="row">
          <button className="tab" disabled={page === 0} onClick={() => setPage(page - 1)}>← prev</button>
          <span className="muted small">page {page + 1} / {Math.ceil(data.total / PAGE)}</span>
          <button className="tab" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}>next →</button>
        </div>
      )}
    </section>
  )
}
