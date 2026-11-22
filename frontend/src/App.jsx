import { useState } from 'react'
import { api } from './api.js'
import { usePoll } from './usePoll.js'
import JobDetail from './components/JobDetail.jsx'
import JobsTable from './components/JobsTable.jsx'
import StatsPanel from './components/StatsPanel.jsx'
import SubmitForm from './components/SubmitForm.jsx'
import TasksPage from './components/TasksPage.jsx'
import Workers from './components/Workers.jsx'

const PAGES = { dashboard: 'Dashboard', tasks: 'Tasks' }
const initialPage = () => (location.hash === '#tasks' ? 'tasks' : 'dashboard')

export default function App() {
  const [health, healthErr] = usePoll(api.health, [], 5000)
  const [stats] = usePoll(api.stats, [], 2000)
  const [page, setPage] = useState(initialPage)
  const [selected, setSelected] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)

  const go = (p) => { setPage(p); history.replaceState(null, '', p === 'tasks' ? '#tasks' : location.pathname) }

  return (
    <div className="app">
      <header className="topbar">
        <div className="row">
          <h1>⚙ Distributed Task Processing</h1>
          <nav className="row nav">
            {Object.entries(PAGES).map(([key, label]) => (
              <button key={key} className={page === key ? 'tab active' : 'tab'} onClick={() => go(key)}>{label}</button>
            ))}
          </nav>
        </div>
        <div className="row">
          <span className={`pill ${health ? 'ok' : 'bad'}`}>{health ? 'API ok' : healthErr || 'connecting…'}</span>
          {health && <span className="pill">broker: {health.broker}</span>}
        </div>
      </header>

      <StatsPanel stats={stats} />

      {page === 'dashboard' ? (
        <div className="grid">
          <div>
            <SubmitForm onSubmitted={refresh} />
            <Workers />
          </div>
          <JobsTable refreshKey={refreshKey} onSelect={setSelected} selected={selected} />
        </div>
      ) : (
        <TasksPage onSelectJob={setSelected} selectedJob={selected} />
      )}

      {selected && <JobDetail id={selected} onClose={() => setSelected(null)} onChanged={refresh} />}
    </div>
  )
}
