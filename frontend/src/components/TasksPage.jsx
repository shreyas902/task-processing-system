import { useState } from 'react'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'
import JobsTable, { ago } from './JobsTable.jsx'
import RunTaskDialog from './RunTaskDialog.jsx'
import TaskForm from './TaskForm.jsx'

/** First meaningful line of a script (skips blank lines and comments) for the task list. */
const firstLine = (code = '') =>
  code.split('\n').map((l) => l.trim()).find((l) => l && !l.startsWith('#')) || code.split('\n')[0]

const fmtInterval = (s) =>
  s % 3600 === 0 ? `${s / 3600}h` : s % 60 === 0 ? `${s / 60}m` : `${s}s`

export default function TasksPage({ onSelectJob, selectedJob }) {
  const [refresh, setRefresh] = useState(0)
  const [tasks, error] = usePoll(api.tasks, [refresh], 3000)
  const [editing, setEditing] = useState(null)
  const [viewing, setViewing] = useState(null)
  const [flash, setFlash] = useState(null)
  const [prompting, setPrompting] = useState(null)  // code task waiting for its input values
  const reload = () => setRefresh((r) => r + 1)

  const act = async (fn, ok) => {
    setFlash(null)
    try {
      await fn()
      if (ok) setFlash({ ok: true, text: ok })
      reload()
    } catch (e) {
      setFlash({ ok: false, text: e.message })
    }
  }

  const started = (t, job) => {
    setFlash({ ok: true, text: `Started “${t.name}”` })
    setViewing(t.name)
    onSelectJob(job.id)
    reload()
  }

  const run = (t) => {
    if (t.handler === 'run_code' && t.default_payload.variables?.length) {
      setPrompting(t)  // ask for input variables first
      return
    }
    act(async () => started(t, await api.runTask(t.name, {})))
  }

  const remove = (t) => {
    if (!confirm(`Delete task “${t.name}”? Its past runs stay in the job history.`)) return
    act(async () => {
      const r = await api.deleteTask(t.name)
      if (!r.ok) throw new Error(`Delete failed (${r.status})`)
      if (editing?.name === t.name) setEditing(null)
      if (viewing === t.name) setViewing(null)
    }, `Deleted “${t.name}”`)
  }

  return (
    <div className="grid">
      <div>
        <TaskForm
          editing={editing}
          onCancel={() => setEditing(null)}
          onSaved={(t) => { setFlash({ ok: true, text: `Saved “${t.name}”` }); setEditing(null); setViewing(t.name); reload() }}
        />
      </div>

      <div>
        <section className="card">
          <div className="row between">
            <h3>Tasks {tasks && <span className="muted small">({tasks.length})</span>}</h3>
            {flash && <span className={flash.ok ? 'ok-text small' : 'error small'}>{flash.text}</span>}
          </div>
          {error && <p className="error">{error}</p>}
          {tasks && tasks.length === 0 && (
            <p className="muted">No tasks yet. Create one on the left: for example, a webhook that runs every 5 minutes.</p>
          )}
          {tasks && tasks.length > 0 && (
            <table>
              <thead>
                <tr><th>Task</th><th>Handler</th><th>Schedule</th><th>Runs</th><th>Last run</th><th /></tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id} className={viewing === t.name ? 'selected' : ''}>
                    <td>
                      <a className="clickable" onClick={() => setViewing(t.name)}><b>{t.name}</b></a>
                      {t.description && <div className="muted small wrap">{t.description}</div>}
                    </td>
                    <td>
                      {t.handler === 'run_code' ? (
                        <>
                          <span className={`pill lang-${t.default_payload.language}`}>{t.default_payload.language}</span>
                          <div className="muted small wrap mono">{firstLine(t.default_payload.code)}</div>
                          {t.default_payload.variables?.length > 0 && (
                            <div className="muted small wrap">inputs: {t.default_payload.variables.map((v) => v.name).join(', ')}</div>
                          )}
                        </>
                      ) : <code>{t.handler}</code>}
                      {t.handler === 'http_request' && (
                        <div className="muted small wrap">{t.default_payload.method} {t.default_payload.url}</div>
                      )}
                    </td>
                    <td>
                      {t.interval_s ? (
                        <>
                          <span className={`pill ${t.enabled ? 'ok' : ''}`}>every {fmtInterval(t.interval_s)}</span>
                          <div className="muted small">{t.enabled ? `next ${ago(t.next_run_at)}` : 'paused'}</div>
                        </>
                      ) : <span className="pill">on demand</span>}
                      {!t.enabled && !t.interval_s && <div className="muted small">disabled</div>}
                    </td>
                    <td className="small">
                      {t.run_count}
                      {(t.stats.succeeded || t.stats.dead) ? (
                        <div><span className="ok-text">✓{t.stats.succeeded || 0}</span> <span className="error">✗{t.stats.dead || 0}</span></div>
                      ) : null}
                    </td>
                    <td className="muted small">{ago(t.last_run_at)}</td>
                    <td className="actions">
                      <button onClick={() => run(t)} disabled={!t.enabled} title={t.enabled ? 'Run now' : 'Enable the task to run it'}>▶ Run</button>
                      <button className="tab" onClick={() => setEditing(t)}>Edit</button>
                      {t.interval_s != null && (
                        <button className="tab" onClick={() => act(() => api.updateTask(t.name, { enabled: !t.enabled }),
                          t.enabled ? `Paused “${t.name}”` : `Resumed “${t.name}”`)}>
                          {t.enabled ? 'Pause' : 'Resume'}
                        </button>
                      )}
                      <button className="tab danger" onClick={() => remove(t)}>Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {prompting && (
          <RunTaskDialog task={prompting} onClose={() => setPrompting(null)}
                         onStarted={(job) => { const t = prompting; setPrompting(null); started(t, job) }} />
        )}

        {viewing && (
          <JobsTable task={viewing} title={`Runs of “${viewing}”`} refreshKey={refresh}
                     onSelect={onSelectJob} selected={selectedJob} />
        )}
      </div>
    </div>
  )
}
