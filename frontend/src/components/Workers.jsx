import { api } from '../api.js'
import { usePoll } from '../usePoll.js'
import { ago } from './JobsTable.jsx'

export default function Workers() {
  const [workers] = usePoll(api.workers, [], 3000)
  const shown = (workers || []).filter((w) => w.alive || Date.now() - new Date(w.last_heartbeat) < 10 * 60e3)
  return (
    <section className="card">
      <h3>Workers <span className="muted small">({shown.filter((w) => w.alive).length} alive)</span></h3>
      {shown.length === 0 && <p className="muted small">No workers yet. Start some with <code>docker compose up --scale worker=3</code>.</p>}
      {shown.map((w) => (
        <div key={w.id} className="worker">
          <div className="row between">
            <code className="small">{w.id}</code>
            <span className={`pill ${w.alive ? 'ok' : 'bad'}`}>{w.alive ? 'alive' : 'gone'}</span>
          </div>
          <div className="slots" title={`${w.active}/${w.concurrency} slots busy`}>
            {Array.from({ length: w.concurrency }, (_, i) => <i key={i} className={i < w.active ? 'busy' : ''} />)}
          </div>
          <div className="muted small">processed {w.processed} · failed {w.failed} · heartbeat {ago(w.last_heartbeat)}</div>
        </div>
      ))}
    </section>
  )
}
