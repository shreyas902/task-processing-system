const BASE = import.meta.env.VITE_API_URL || ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* not JSON */ }
    if (Array.isArray(detail)) detail = detail.map((d) => `${d.loc?.slice(1).join('.')}: ${d.msg}`).join('; ')
    throw new Error(detail)
  }
  return res.json()
}

const post = (path, body) => request(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
})

export const api = {
  health: () => request('/api/health'),
  taskTypes: () => request('/api/task-types'),
  stats: () => request('/api/stats'),
  workers: () => request('/api/workers'),
  jobs: ({ status, type, task, limit = 20, offset = 0 }) => {
    const q = new URLSearchParams({ limit, offset })
    if (status) q.set('status', status)
    if (type) q.set('type', type)
    if (task) q.set('task', task)
    return request(`/api/jobs?${q}`)
  },
  job: (id) => request(`/api/jobs/${id}`),
  create: (body) => post('/api/jobs', body),
  bulk: (body) => post('/api/jobs/bulk', body),
  cancel: (id) => post(`/api/jobs/${id}/cancel`),
  retry: (id) => post(`/api/jobs/${id}/retry`),
  tasks: () => request('/api/tasks'),
  createTask: (body) => post('/api/tasks', body),
  updateTask: (name, body) => request(`/api/tasks/${encodeURIComponent(name)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  deleteTask: (name) => fetch(`${BASE}/api/tasks/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  runTask: (name, body) => post(`/api/tasks/${encodeURIComponent(name)}/run`, body),
}

