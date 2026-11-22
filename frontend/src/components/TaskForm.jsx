import { useEffect, useState } from 'react'
import { api } from '../api.js'
import VariablesEditor, { toApiVariables, fromApiVariables } from './VariablesEditor.jsx'

const UNITS = { seconds: 1, minutes: 60, hours: 3600 }

const KINDS = {
  python: { label: 'Python script', handler: 'run_code' },
  shell: { label: 'Shell command', handler: 'run_code' },
  webhook: { label: 'Webhook (HTTP request)', handler: 'http_request' },
  builtin: { label: 'Built-in handler', handler: null },
}

const EXAMPLE_VARS = [
  { name: 'city', type: 'string', default: 'Pune', required: true, description: 'City to report on' },
  { name: 'days', type: 'number', default: '7', required: false, description: 'How many days' },
]

const TEMPLATES = {
  python: `# Your variables are in the \`inputs\` dict, e.g. inputs["city"].
# Everything you print() is captured. Set \`result\` to return structured data.
city = inputs["city"]
days = inputs["days"]

print(f"Generating a {days}-day report for {city}...")
result = {"city": city, "days": days, "status": "ok"}
`,
  shell: `# Your variables are environment variables: $city, $days
echo "Generating a $days-day report for $city"
date
echo "Working directory: $(pwd)"
`,
}

const EMPTY = {
  name: '', description: '', kind: 'python', handler: 'fibonacci', payload: '{}',
  code: TEMPLATES.python, variables: EXAMPLE_VARS,
  priority: 'normal', max_retries: 0, timeout_seconds: 60,
  recurring: false, every: 5, unit: 'minutes', enabled: true,
  method: 'POST', url: 'https://httpbin.org/post', headers: '{}', body: '{"hello": "world"}', expected: '',
}

function splitInterval(s) {
  for (const unit of ['hours', 'minutes']) if (s % UNITS[unit] === 0) return { every: s / UNITS[unit], unit }
  return { every: s, unit: 'seconds' }
}

function fromTask(t) {
  const p = t.default_payload || {}
  const kind = t.handler === 'run_code' ? p.language : t.handler === 'http_request' ? 'webhook' : 'builtin'
  return {
    ...EMPTY, name: t.name, description: t.description, kind, handler: t.handler,
    payload: JSON.stringify(p, null, 2), priority: t.priority, max_retries: t.max_retries,
    timeout_seconds: t.timeout_s, enabled: t.enabled, recurring: t.interval_s != null,
    ...(t.interval_s != null ? splitInterval(t.interval_s) : {}),
    ...(kind === 'python' || kind === 'shell' ? { code: p.code || '', variables: fromApiVariables(p.variables) } : {}),
    ...(kind === 'webhook' ? {
      method: p.method || 'GET', url: p.url || '', headers: JSON.stringify(p.headers || {}),
      body: p.body != null ? JSON.stringify(p.body) : '', expected: (p.expected_status || []).join(', '),
    } : {}),
  }
}

function parseJson(text, field) {
  try { return text.trim() ? JSON.parse(text) : undefined } catch { throw new Error(`${field} must be valid JSON`) }
}

/** Textarea that inserts spaces on Tab instead of moving focus. */
function CodeEditor({ value, onChange }) {
  const onKeyDown = (e) => {
    if (e.key !== 'Tab') return
    e.preventDefault()
    const el = e.target, { selectionStart: s, selectionEnd: end } = el
    onChange(value.slice(0, s) + '    ' + value.slice(end))
    requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = s + 4 })
  }
  return (
    <textarea className="code" rows={12} spellCheck={false} value={value}
              onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown} />
  )
}

export default function TaskForm({ editing, onSaved, onCancel }) {
  const [types, setTypes] = useState([])
  const [f, setF] = useState(EMPTY)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.taskTypes().then(setTypes).catch(() => {}) }, [])
  useEffect(() => { setF(editing ? fromTask(editing) : EMPTY); setError('') }, [editing])

  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const isCode = f.kind === 'python' || f.kind === 'shell'
  const builtins = types.filter((t) => !['run_code', 'http_request'].includes(t.type))

  const pickKind = (kind) => {
    const next = { ...f, kind }
    if ((kind === 'python' || kind === 'shell') && !editing) {
      // Swap in the template only if the user hasn't written their own code yet.
      if (!f.code.trim() || Object.values(TEMPLATES).includes(f.code)) next.code = TEMPLATES[kind]
      next.max_retries = 0  // scripts usually shouldn't re-run automatically
    }
    if (kind === 'builtin' && !builtins.some((t) => t.type === f.handler) && builtins[0]) {
      next.handler = builtins[0].type
      next.payload = JSON.stringify(builtins[0].example, null, 2)
    }
    setF(next)
  }
  const pickHandler = (e) => {
    const t = types.find((x) => x.type === e.target.value)
    setF({ ...f, handler: e.target.value, payload: JSON.stringify(t?.example ?? {}, null, 2) })
  }

  const build = () => {
    if (isCode) {
      if (!f.code.trim()) throw new Error('Write the code to run')
      return { handler: 'run_code', payload: { language: f.kind, code: f.code, variables: toApiVariables(f.variables) } }
    }
    if (f.kind === 'webhook') {
      const expected = f.expected.split(',').map((x) => x.trim()).filter(Boolean).map(Number)
      if (expected.some((n) => !Number.isInteger(n))) throw new Error('Expected status codes must be numbers, e.g. 200, 201')
      const payload = { method: f.method, url: f.url, headers: parseJson(f.headers, 'Headers') ?? {} }
      const body = parseJson(f.body, 'Body')
      if (body !== undefined && !['GET', 'HEAD'].includes(f.method)) payload.body = body
      if (expected.length) payload.expected_status = expected
      return { handler: 'http_request', payload }
    }
    return { handler: f.handler, payload: parseJson(f.payload, 'Default payload') ?? {} }
  }

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setBusy(true)
    try {
      const { handler, payload } = build()
      const body = {
        description: f.description, handler, default_payload: payload, priority: f.priority,
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

  return (
    <form className="card form" onSubmit={submit}>
      <div className="row between">
        <h3>{editing ? `Edit task “${editing.name}”` : 'Create task'}</h3>
        {editing && <button type="button" className="link" onClick={onCancel}>cancel</button>}
      </div>
      {!editing && (
        <label>Name
          <input required value={f.name} onChange={set('name')} placeholder="e.g. daily-city-report"
                 pattern="[a-z0-9][a-z0-9_\-]{1,63}" title="2-64 chars: lowercase letters, digits, - and _" />
        </label>
      )}
      <label>Description
        <textarea rows={2} value={f.description} onChange={set('description')} maxLength={500}
                  placeholder="What does this task do? Who should run it and when?" />
      </label>

      <label>What to run</label>
      <div className="kinds">
        {Object.entries(KINDS).map(([k, v]) => (
          <button type="button" key={k} className={f.kind === k ? 'tab active' : 'tab'} onClick={() => pickKind(k)}>{v.label}</button>
        ))}
      </div>

      {isCode && (
        <>
          <label>{f.kind === 'python' ? 'Python code' : 'Shell commands (bash)'}</label>
          <CodeEditor value={f.code} onChange={(code) => setF({ ...f, code })} />
          <p className="muted small">
            Runs in an isolated sandbox: no network, no access to the database or secrets, 256 MB memory,
            output capped at 64 KB. A non-zero exit code marks the run as failed.
          </p>
          <VariablesEditor language={f.kind} variables={f.variables}
                           onChange={(variables) => setF({ ...f, variables })} />
        </>
      )}

      {f.kind === 'webhook' && (
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
      )}

      {f.kind === 'builtin' && (
        <>
          <label>Handler
            <select value={f.handler} onChange={pickHandler}>
              {builtins.map((t) => <option key={t.type}>{t.type}</option>)}
            </select>
          </label>
          <p className="muted small">{types.find((t) => t.type === f.handler)?.description}</p>
          <label>Default payload (JSON)<textarea rows={4} value={f.payload} onChange={set('payload')} /></label>
        </>
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
          <>
            <div className="row">
              <span className="small muted">every</span>
              <input type="number" min="1" className="narrow" value={f.every} onChange={set('every')} />
              <select className="narrow" value={f.unit} onChange={set('unit')}>
                {Object.keys(UNITS).map((u) => <option key={u}>{u}</option>)}
              </select>
            </div>
            {isCode && <p className="muted small">Scheduled runs use the variables' default values.</p>}
          </>
        )}
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /> Enabled</label>
      </fieldset>

      <button type="submit" disabled={busy}>{editing ? 'Save changes' : 'Create task'}</button>
      {error && <p className="error small">{error}</p>}
    </form>
  )
}
