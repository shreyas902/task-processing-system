import { useState } from 'react'
import { api } from '../api.js'

/** Prompts for a code task's input variables, then runs it. */
export default function RunTaskDialog({ task, onClose, onStarted }) {
  const variables = task.default_payload.variables || []
  const [values, setValues] = useState(() =>
    Object.fromEntries(variables.map((v) => [v.name, v.default == null ? '' : String(v.default)])))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setBusy(true)
    try {
      const inputs = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''))
      onStarted(await api.runTask(task.name, { payload: { inputs } }))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="drawer-backdrop center" onClick={onClose}>
      <form className="card dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="row between">
          <h3>Run “{task.name}”</h3>
          <button type="button" className="link" onClick={onClose}>✕</button>
        </div>
        {task.description && <p className="muted small">{task.description}</p>}
        {variables.map((v) => (
          <label key={v.name}>
            <span><code>{v.name}</code> <span className="muted">({v.type})</span>{v.required && <span className="error"> *</span>}</span>
            {v.type === 'boolean' ? (
              <select value={values[v.name]} onChange={(e) => setValues({ ...values, [v.name]: e.target.value })}>
                <option value="">(not set)</option><option value="true">true</option><option value="false">false</option>
              </select>
            ) : (
              <input type={v.type === 'number' ? 'number' : 'text'} step="any" value={values[v.name]}
                     required={v.required} autoFocus={v === variables[0]}
                     onChange={(e) => setValues({ ...values, [v.name]: e.target.value })} />
            )}
            {v.description && <span className="muted small">{v.description}</span>}
          </label>
        ))}
        <button type="submit" disabled={busy}>▶ Run</button>
        {error && <p className="error small">{error}</p>}
      </form>
    </div>
  )
}

/**
 * Shared "run a task" flow: tasks with input variables open the inputs dialog first,
 * others run immediately. Returns [run(task), dialogElement].
 */
export function useRunTask(onStarted, onError) {
  const [prompting, setPrompting] = useState(null)
  const run = async (task) => {
    if (task.handler === 'run_code' && task.default_payload?.variables?.length) {
      setPrompting(task)
      return
    }
    try {
      onStarted(task, await api.runTask(task.name, {}))
    } catch (e) {
      onError?.(e)
    }
  }
  const dialog = prompting && (
    <RunTaskDialog task={prompting} onClose={() => setPrompting(null)}
                   onStarted={(job) => { const t = prompting; setPrompting(null); onStarted(t, job) }} />
  )
  return [run, dialog]
}
