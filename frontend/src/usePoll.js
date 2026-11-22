import { useEffect, useRef, useState } from 'react'

/** Run `load` now and every `ms` milliseconds while mounted; re-runs when deps change. */
export function usePoll(load, deps = [], ms = 2000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const loader = useRef(load)
  loader.current = load

  useEffect(() => {
    let alive = true
    const tick = () => loader.current()
      .then((d) => { if (alive) { setData(d); setError('') } })
      .catch((e) => alive && setError(e.message))
    tick()
    const t = setInterval(tick, ms)
    return () => { alive = false; clearInterval(t) }
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps

  return [data, error]
}
