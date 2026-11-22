import { useState } from 'react'
import { api } from './api.js'
import { usePoll } from './usePoll.js'
import JobDetail from './components/JobDetail.jsx'
import JobsTable from './components/JobsTable.jsx'
import StatsPanel from './components/StatsPanel.jsx'
import SubmitForm from './components/SubmitForm.jsx'
import Workers from './components/Workers.jsx'

export default function App() {
  const [health, healthErr] = usePoll(api.health, [], 5000)
  const [stats] = usePoll(api.stats, [], 2000)
  const [selected, setSelected] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)

  return (
    <div className="app">
      <header className="topbar">
        <h1>⚙ Distributed Task Processing</h1>
        <div className="row">
          <span className={`pill ${health ? 'ok' : 'bad'}`}>{health ? 'API ok' : healthErr || 'connecting…'}</span>
          {health && <span className="pill">broker: {health.broker}</span>}
        </div>
      </header>

      <StatsPanel stats={stats} />

      <div className="grid">
        <div>
          <SubmitForm onSubmitted={refresh} />
          <Workers />
        </div>
        <JobsTable refreshKey={refreshKey} onSelect={setSelected} selected={selected} />
      </div>

      {selected && <JobDetail id={selected} onClose={() => setSelected(null)} onChanged={refresh} />}
    </div>
  )
}
