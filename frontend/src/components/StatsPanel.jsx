const ORDER = ['scheduled', 'queued', 'running', 'succeeded', 'dead', 'cancelled']

export default function StatsPanel({ stats }) {
  if (!stats) return <div className="card muted">Loading stats…</div>
  const b = stats.broker || {}
  const depth = b.name === 'redis'
    ? `high ${b.high ?? 0} · normal ${b.normal ?? 0} · low ${b.low ?? 0} · in-flight ${b.inflight ?? 0}`
    : b.name === 'rabbitmq' ? `ready ${b.ready ?? 0} · consumers ${b.consumers ?? 0}` : b.error

  return (
    <section>
      <div className="stats">
        {ORDER.map((s) => (
          <div key={s} className={`stat st-${s}`}>
            <div className="stat-value">{stats.by_status[s]}</div>
            <div className="stat-label">{s === 'dead' ? 'dead-letter' : s}</div>
          </div>
        ))}
        <div className="stat">
          <div className="stat-value">{stats.succeeded_last_minute}</div>
          <div className="stat-label">done / last min</div>
        </div>
        <div className="stat">
          <div className="stat-value">{stats.avg_duration_ms_15m != null ? `${Math.round(stats.avg_duration_ms_15m)}ms` : '–'}</div>
          <div className="stat-label">avg duration (15m)</div>
        </div>
      </div>
      <div className="card row between">
        <span className="small"><b>Broker queue ({b.name}):</b> <span className="muted">{depth}</span></span>
        <Throughput points={stats.throughput} />
      </div>
    </section>
  )
}

/** Tiny stacked bar chart: completions per 10s bucket over the last 5 minutes. */
function Throughput({ points }) {
  const now = Math.floor(Date.now() / 10000) * 10000
  const buckets = Array.from({ length: 30 }, (_, i) => ({ t: now - (29 - i) * 10000, ok: 0, dead: 0 }))
  for (const p of points) {
    const b = buckets.find((x) => x.t === new Date(p.t).getTime())
    if (b) b[p.status === 'succeeded' ? 'ok' : 'dead'] += p.count
  }
  const max = Math.max(1, ...buckets.map((b) => b.ok + b.dead))
  const W = 300, H = 44, bw = W / buckets.length
  return (
    <div className="row small muted">
      <span>throughput (5 min)</span>
      <svg width={W} height={H} role="img" aria-label="Jobs finished per 10 seconds">
        {buckets.map((b, i) => {
          const hOk = (b.ok / max) * H, hDead = (b.dead / max) * H
          return (
            <g key={b.t}>
              <title>{`${new Date(b.t).toLocaleTimeString()}: ${b.ok} succeeded, ${b.dead} dead`}</title>
              <rect x={i * bw + 1} y={H - hOk} width={bw - 2} height={hOk} fill="var(--ok)" />
              <rect x={i * bw + 1} y={H - hOk - hDead} width={bw - 2} height={hDead} fill="var(--bad)" />
            </g>
          )
        })}
      </svg>
    </div>
  )
}
