import { useState } from 'react'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

const FILTERS = ['', 'scheduled', 'queued', 'running', 'succeeded', 'dead', 'cancelled']
const PAGE = 20

export const ago = (iso) => {
  if (!iso) return '–'
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < -1) return `in ${-s}s`
  return s < 60 ? `${Math.max(s, 0)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`
}

export default function JobsTable({ refreshKey, onSelect, selected }) {
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  const [data, error] = usePoll(() => api.jobs({ status, limit: PAGE, offset: page * PAGE }), [status, page, refreshKey])

  return (
    <section className="card">
      <div className="row between">
        <h3>Jobs {data && <span className="muted small">({data.total})</span>}</h3>
        <div className="row">
          {FILTERS.map((f) => (
            <button key={f} className={f === status ? 'tab active' : 'tab'} onClick={() => { setStatus(f); setPage(0) }}>
              {f === '' ? 'all' : f === 'dead' ? 'dead-letter' : f}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr><th>ID</th><th>Type</th><th>Priority</th><th>Status</th><th>Attempts</th><th>Created</th><th>Worker</th></tr>
        </thead>
        <tbody>
          {data?.items.map((j) => (
            <tr key={j.id} onClick={() => onSelect(j.id)} className={j.id === selected ? 'selected clickable' : 'clickable'}>
              <td><code>{j.id.slice(0, 8)}</code></td>
              <td>{j.type}</td>
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
        </tbody>
      </table>
      {data && data.total > PAGE && (
        <div className="row">
          <button className="tab" disabled={page === 0} onClick={() => setPage(page - 1)}>← prev</button>
          <span className="muted small">page {page + 1} / {Math.ceil(data.total / PAGE)}</span>
          <button className="tab" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}>next →</button>
        </div>
      )}
    </section>
  )
}
