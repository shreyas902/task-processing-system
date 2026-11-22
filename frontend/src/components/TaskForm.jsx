import { useEffect, useState } from 'react'
import { api } from '../api.js'

const UNITS = { seconds: 1, minutes: 60, hours: 3600 }

const EMPTY = {
  name: '', description: '', handler: 'http_request', payload: '{}', priority: 'normal', max_retries: 3,
  timeout_seconds: 60, recurring: false, every: 5, unit: 'minutes', enabled: true,
  // structured fields for the http_request (webhook) handler
  method: 'POST', url: 'https://httpbin.org/post', headers: '{}', body: '{"hello": "world"}', expected: '',
}

/** Split a stored interval back into the largest whole unit (300s -> 5 minutes). */
function splitInterval(s) {
  for (const unit of ['hours', 'minutes']) if (s % UNITS[unit] === 0) return { every: s / UNITS[unit], unit }
  return { every: s, unit: 'seconds' }
}

function fromTask(t) {
  const p = t.default_payload || {}
  return {
    ...EMPTY, name: t.name, description: t.description, handler: t.handler,
    payload: JSON.stringify(p, null, 2), priority: t.priority, max_retries: t.max_retries,
    timeout_seconds: t.timeout_s, enabled: t.enabled, recurring: t.interval_s != null,
    ...(t.interval_s != null ? splitInterval(t.interval_s) : {}),
    ...(t.handler === 'http_request' ? {
      method: p.method || 'GET', url: p.url || '', headers: JSON.stringify(p.headers || {}),
      body: p.body != null ? JSON.stringify(p.body) : '', expected: (p.expected_status || []).join(', '),
    } : {}),
  }
}

function parseJson(text, field) {
  try { return text.trim() ? JSON.parse(text) : undefined } catch { throw new Error(`${field} must be valid JSON`) }
}

export default function TaskForm({ editing, onSaved, onCancel }) {
  const [types, setTypes] = useState([])
  const [f, setF] = useState(EMPTY)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.taskTypes().then(setTypes).catch(() => {}) }, [])
  useEffect(() => { setF(editing ? fromTask(editing) : EMPTY); setError('') }, [editing])

  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const pickHandler = (e) => {
    const t = types.find((x) => x.type === e.target.value)
    setF({ ...f, handler: e.target.value, payload: JSON.stringify(t?.example ?? {}, null, 2) })
  }
  const isWebhook = f.handler === 'http_request'

  const buildPayload = () => {
    if (!isWebhook) return parseJson(f.payload, 'Default payload') ?? {}
    const expected = f.expected.split(',').map((x) => x.trim()).filter(Boolean).map(Number)
    if (expected.some((n) => !Number.isInteger(n))) throw new Error('Expected status codes must be numbers, e.g. 200, 201')
    const payload = { method: f.method, url: f.url, headers: parseJson(f.headers, 'Headers') ?? {} }
    const body = parseJson(f.body, 'Body')
    if (body !== undefined && !['GET', 'HEAD'].includes(f.method)) payload.body = body
    if (expected.length) payload.expected_status = expected
    return payload
  }

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setBusy(true)
    try {
      const body = {
        description: f.description, handler: f.handler, default_payload: buildPayload(), priority: f.priority,
        max_retries: +f.max_retries, timeout_seconds: +f.timeout_seconds, enabled: f.enabled,
        interval_seconds: f.recurring ? Math.round(+f.every * UNITS[f.unit]) : null,
      }
      const saved = editing ? await api.updateTask(editing.name, body) : await api.createTask({ name: f.name, ...body })
      onSaved(saved)
      if (!editing) setF(EMPTY)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const desc = types.find((t) => t.type === f.handler)?.description

  return (
    <form className="card form" onSubmit={submit}>
      <div className="row between">
        <h3>{editing ? `Edit task “${editing.name}”` : 'Create task'}</h3>
        {editing && <button type="button" className="link" onClick={onCancel}>cancel</button>}
      </div>
      {!editing && (
        <label>Name
          <input required value={f.name} onChange={set('name')} placeholder="e.g. ping-payment-service"
                 pattern="[a-z0-9][a-z0-9_\-]{1,63}" title="2-64 chars: lowercase letters, digits, - and _" />
        </label>
      )}
      <label>Description<input value={f.description} onChange={set('description')} placeholder="What does this task do?" /></label>
      <label>Handler
        <select value={f.handler} onChange={pickHandler}>
          {types.map((t) => <option key={t.type} value={t.type}>{t.type === 'http_request' ? 'http_request (webhook)' : t.type}</option>)}
        </select>
      </label>
      {desc && <p className="muted small">{desc}</p>}

      {isWebhook ? (
        <fieldset>
          <legend>Webhook</legend>
          <div className="method-url">
            <select value={f.method} onChange={set('method')}>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'].map((m) => <option key={m}>{m}</option>)}
            </select>
            <input required value={f.url} onChange={set('url')} placeholder="https://example.com/hook" />
          </div>
          <label>Headers (JSON)<textarea rows={2} value={f.headers} onChange={set('headers')} /></label>
          {!['GET', 'HEAD'].includes(f.method) && (
            <label>JSON body<textarea rows={3} value={f.body} onChange={set('body')} /></label>
          )}
          <label>Expected status codes<input value={f.expected} onChange={set('expected')} placeholder="any 2xx (or e.g. 200, 201)" /></label>
          <p className="muted small">Private/internal addresses are blocked (SSRF protection). 5xx, 429 and network errors are retried.</p>
        </fieldset>
      ) : (
        <label>Default payload (JSON)<textarea rows={4} value={f.payload} onChange={set('payload')} /></label>
      )}

      <div className="two">
        <label>Priority
          <select value={f.priority} onChange={set('priority')}>
            <option>high</option><option>normal</option><option>low</option>
          </select>
        </label>
        <label>Max retries<input type="number" min="0" max="20" value={f.max_retries} onChange={set('max_retries')} /></label>
        <label>Timeout (s)<input type="number" min="1" max="3600" value={f.timeout_seconds} onChange={set('timeout_seconds')} /></label>
      </div>

      <fieldset>
        <legend>Schedule</legend>
        <label className="check"><input type="radio" checked={!f.recurring} onChange={() => setF({ ...f, recurring: false })} /> Run on demand only</label>
        <label className="check"><input type="radio" checked={f.recurring} onChange={() => setF({ ...f, recurring: true })} /> Repeat automatically</label>
        {f.recurring && (
          <div className="row">
            <span className="small muted">every</span>
            <input type="number" min="1" className="narrow" value={f.every} onChange={set('every')} />
            <select className="narrow" value={f.unit} onChange={set('unit')}>
              {Object.keys(UNITS).map((u) => <option key={u}>{u}</option>)}
            </select>
          </div>
        )}
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /> Enabled</label>
      </fieldset>

      <button type="submit" disabled={busy}>{editing ? 'Save changes' : 'Create task'}</button>
      {error && <p className="error small">{error}</p>}
    </form>
  )
}
