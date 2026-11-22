const TYPES = ['string', 'number', 'boolean']

/** Form rows (defaults kept as strings for inputs) <-> API variables (defaults typed or null). */
export const fromApiVariables = (vars = []) =>
  vars.map((v) => ({ ...v, default: v.default == null ? '' : String(v.default) }))

export const toApiVariables = (rows) =>
  rows.map((v) => ({
    name: v.name.trim(), type: v.type, required: !!v.required, description: v.description || '',
    default: v.default === '' ? null : v.default,
  }))

export default function VariablesEditor({ language, variables, onChange }) {
  const update = (i, key, value) => onChange(variables.map((v, j) => (j === i ? { ...v, [key]: value } : v)))
  const add = () => onChange([...variables, { name: '', type: 'string', default: '', required: false, description: '' }])
  const remove = (i) => onChange(variables.filter((_, j) => j !== i))
  const example = variables.find((v) => v.name)?.name || 'name'

  return (
    <fieldset>
      <legend>Input variables</legend>
      <p className="muted small">
        Asked for each time the task runs. Use them as{' '}
        {language === 'python' ? <code>{`inputs["${example}"]`}</code> : <code>{`$${example}`}</code>}.
        Values are passed as data, never pasted into the code, so they can't inject commands.
      </p>
      {variables.length > 0 && (
        <div className="vars">
          <div className="vars-head small muted"><span>Name</span><span>Type</span><span>Default</span><span>Req.</span><span /></div>
          {variables.map((v, i) => (
            <div key={i} className="var-row">
              <input value={v.name} placeholder="name" pattern="[a-z_][a-z0-9_]{0,31}"
                     title="lowercase letters, digits and _" onChange={(e) => update(i, 'name', e.target.value)} />
              <select value={v.type} onChange={(e) => update(i, 'type', e.target.value)}>
                {TYPES.map((t) => <option key={t}>{t}</option>)}
              </select>
              {v.type === 'boolean' ? (
                <select value={v.default} onChange={(e) => update(i, 'default', e.target.value)}>
                  <option value="">(none)</option><option value="true">true</option><option value="false">false</option>
                </select>
              ) : (
                <input value={v.default} placeholder="(none)" type={v.type === 'number' ? 'number' : 'text'}
                       step="any" onChange={(e) => update(i, 'default', e.target.value)} />
              )}
              <input type="checkbox" checked={v.required} title="required"
                     onChange={(e) => update(i, 'required', e.target.checked)} />
              <button type="button" className="link danger-text" title="remove" onClick={() => remove(i)}>✕</button>
              <input className="var-desc" value={v.description} placeholder="description (shown when running)"
                     onChange={(e) => update(i, 'description', e.target.value)} />
            </div>
          ))}
        </div>
      )}
      <button type="button" className="tab" onClick={add}>+ Add variable</button>
    </fieldset>
  )
}
