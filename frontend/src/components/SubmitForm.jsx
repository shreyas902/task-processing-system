import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function SubmitForm({ onSubmitted }) {
  const [types, setTypes] = useState([])
  const [form, setForm] = useState({
    type: 'sleep', payload: '{"seconds": 3}', priority: 'normal', max_retries: 3,
    timeout_seconds: 60, delay_seconds: 0, count: 1, idempotency_key: '',
  })
  const [msg, setMsg] = useState(null)

  useEffect(() => { api.taskTypes().then(setTypes).catch(() => {}) }, [])
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const pickType = (e) => {
    const t = types.find((x) => x.type === e.target.value)
    setForm({ ...form, type: e.target.value, payload: JSON.stringify(t?.example ?? {}) })
  }

  const submit = async (e) => {
    e.preventDefault()
    setMsg(null)
    try {
      const body = {
        type: form.type, payload: JSON.parse(form.payload || '{}'), priority: form.priority,
        max_retries: +form.max_retries, timeout_seconds: +form.timeout_seconds, delay_seconds: +form.delay_seconds,
        idempotency_key: form.idempotency_key || null,
      }
      if (+form.count > 1) {
        const r = await api.bulk({ ...body, count: +form.count })
        setMsg({ ok: true, text: `Submitted ${r.created} jobs` })
      } else {
        const job = await api.create(body)
        setMsg({ ok: true, text: `Job ${job.id.slice(0, 8)} → ${job.status}` })
      }
      onSubmitted()
    } catch (err) {
      setMsg({ ok: false, text: err instanceof SyntaxError ? 'Payload must be valid JSON' : err.message })
    }
  }

  const desc = types.find((t) => t.type === form.type)?.description

  return (
    <form className="card form" onSubmit={submit}>
      <h3>Submit job</h3>
      <label>Type
        <select value={form.type} onChange={pickType}>
          {types.map((t) => <option key={t.type}>{t.type}</option>)}
        </select>
      </label>
      {desc && <p className="muted small">{desc}</p>}
      <label>Payload (JSON)<textarea rows={2} value={form.payload} onChange={set('payload')} /></label>
      <div className="two">
        <label>Priority
          <select value={form.priority} onChange={set('priority')}>
            <option>high</option><option>normal</option><option>low</option>
          </select>
        </label>
        <label>Max retries<input type="number" min="0" max="20" value={form.max_retries} onChange={set('max_retries')} /></label>
        <label>Timeout (s)<input type="number" min="1" value={form.timeout_seconds} onChange={set('timeout_seconds')} /></label>
        <label>Delay (s)<input type="number" min="0" value={form.delay_seconds} onChange={set('delay_seconds')} /></label>
        <label>Count<input type="number" min="1" max="1000" value={form.count} onChange={set('count')} /></label>
        <label>Idempotency key<input value={form.idempotency_key} onChange={set('idempotency_key')} placeholder="optional" /></label>
      </div>
      <button type="submit">Submit</button>
      {msg && <p className={msg.ok ? 'ok-text small' : 'error small'}>{msg.text}</p>}
    </form>
  )
}
