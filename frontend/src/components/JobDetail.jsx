import { useState } from 'react'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

export default function JobDetail({ id, onClose, onChanged }) {
  const [tick, setTick] = useState(0)
  const [job, error] = usePoll(() => api.job(id), [id, tick], 1500)
  const [actionErr, setActionErr] = useState('')

  const act = async (fn) => {
    setActionErr('')
    try { await fn(id); setTick((t) => t + 1); onChanged() } catch (e) { setActionErr(e.message) }
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Job <code>{id.slice(0, 8)}</code></h3>
          <button className="link" onClick={onClose}>close ✕</button>
        </div>
        {error && <p className="error">{error}</p>}
        {job && (
          <>
            <div className="row">
              <span className={`pill st-${job.status}`}>{job.status}</span>
              <span className={`pill pr-${job.priority}`}>{job.priority}</span>
              <span className="muted small">{job.type} · attempt {job.attempts}/{job.max_retries + 1} · timeout {job.timeout_s}s</span>
            </div>
            <div className="row">
              {['scheduled', 'queued'].includes(job.status) && <button onClick={() => act(api.cancel)}>Cancel</button>}
              {['dead', 'cancelled'].includes(job.status) && <button onClick={() => act(api.retry)}>Retry</button>}
            </div>
            {actionErr && <p className="error small">{actionErr}</p>}
            <h4>Payload</h4><pre>{JSON.stringify(job.payload, null, 2)}</pre>
            {job.result != null && (<><h4>Result</h4><pre>{JSON.stringify(job.result, null, 2)}</pre></>)}
            {job.error && (<><h4>Last error</h4><pre className="error">{job.error}</pre></>)}
            <h4>Execution history</h4>
            <ol className="timeline">
              {job.events.map((e, i) => (
                <li key={i} className={`ev-${e.event}`}>
                  <b>{e.event}</b>{e.attempt > 0 && <span className="muted"> · attempt {e.attempt}</span>}
                  <div className="muted small">{new Date(e.ts).toLocaleTimeString()} {e.worker_id && `· ${e.worker_id}`}</div>
                  {e.message && <div className="small">{e.message}</div>}
                </li>
              ))}
            </ol>
            {job.idempotency_key && <p className="muted small">idempotency key: <code>{job.idempotency_key}</code></p>}
            <p className="muted small">full id: <code>{job.id}</code></p>
          </>
        )}
      </aside>
    </div>
  )
}
