/*
 * Add, edit and delete lighting modes.
 *
 * These are the same entries the voice parser matches against, so a mode created here
 * is speakable immediately — the server reads the live list on every command rather
 * than caching it at startup.
 */

import { useState } from 'react'
import { api } from '../api'
import type { Mode } from '../api'
import { SectionHead } from './ui'

export function ModesManager({
  modes, onChange, onError,
}: {
  modes: Mode[]
  onChange: () => void
  onError: (message: string) => void
}) {
  const [name, setName] = useState('')
  const [lux, setLux] = useState(400)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<{ name: string; target_lux: number }>({
    name: '', target_lux: 0,
  })

  const guard = (promise: Promise<unknown>) =>
    promise.then(onChange).catch((error) => onError(String(error.message ?? error)))

  const add = () => {
    if (!name.trim()) return
    guard(api.createMode(name.trim(), lux)).then(() => {
      setName('')
      setLux(400)
    })
  }

  return (
    <section className="glass rise">
      <SectionHead
        eyebrow="Modes"
        title={<>Named <span className="accent-serif">scenes</span></>}
        right={<span style={{ fontSize: 12, color: 'var(--text-faint)' }}>speakable the moment they're saved</span>}
      />

      <div className="stack" style={{ gap: 8, marginBottom: 18 }}>
        {modes.map((mode) =>
          editing === mode.id ? (
            <div key={mode.id} className="row" style={{ gap: 8 }}>
              <input
                type="text"
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                style={{ flex: 2 }}
              />
              <input
                type="number"
                value={draft.target_lux}
                onChange={(event) =>
                  setDraft({ ...draft, target_lux: Number(event.target.value) })}
                style={{ flex: 1 }}
              />
              <button
                className="primary"
                onClick={() =>
                  guard(api.updateMode(mode.id, draft)).then(() => setEditing(null))}
              >
                Save
              </button>
              <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
            </div>
          ) : (
            <div
              key={mode.id}
              className="inset between"
              style={{ padding: '10px 10px 10px 16px', borderRadius: 16 }}
            >
              <div className="row" style={{ gap: 11 }}>
                {/* A swatch whose opacity tracks the target, so the list reads as a
                    brightness ramp at a glance. */}
                <span
                  style={{
                    width: 9, height: 9, borderRadius: '50%', background: 'var(--amber)',
                    opacity: 0.25 + 0.75 * Math.min(1, mode.target_lux / 800),
                    boxShadow: '0 0 8px var(--amber-glow)',
                  }}
                />
                <span style={{ fontWeight: 500 }}>{mode.name}</span>
                <span className="metric" style={{ fontSize: 12, color: 'var(--text-faint)' }}>
                  {mode.target_lux} lx
                </span>
              </div>
              <div className="row" style={{ gap: 6 }}>
                <button
                  className="ghost"
                  onClick={() => {
                    setEditing(mode.id)
                    setDraft({ name: mode.name, target_lux: mode.target_lux })
                  }}
                >
                  Edit
                </button>
                <button
                  className="ghost danger"
                  onClick={() => guard(api.deleteMode(mode.id))}
                >
                  Delete
                </button>
              </div>
            </div>
          ),
        )}
      </div>

      <div className="row" style={{ gap: 8 }}>
        <input
          type="text"
          placeholder="New mode name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && add()}
          style={{ flex: 2 }}
        />
        <input
          type="number"
          value={lux}
          onChange={(event) => setLux(Number(event.target.value))}
          style={{ flex: 1 }}
          title="Target lux"
        />
        <button className="primary" onClick={add} disabled={!name.trim()}>Add</button>
      </div>
    </section>
  )
}
