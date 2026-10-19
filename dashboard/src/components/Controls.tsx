/*
 * Manual override: put the room where you want it without speaking to it.
 *
 * Every control here goes through the same state machine the voice pipeline uses, so
 * a dashboard override sets MANUAL and starts the same revert timer — the two input
 * paths cannot disagree about what mode the system is in.
 */

import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { api } from '../api'
import type { Mode, SystemState } from '../api'
import { SectionHead } from './ui'

export function Controls({
  state, modes, onError,
}: {
  state: SystemState | null
  modes: Mode[]
  onError: (message: string) => void
}) {
  const [slider, setSlider] = useState(0)
  const [dragging, setDragging] = useState(false)

  // Track the server while the user is not actively dragging, so the slider reflects
  // AUTO's decisions but never fights the thumb under the cursor.
  useEffect(() => {
    if (!dragging && state) setSlider(Math.round(state.brightness * 100))
  }, [state, dragging])

  const send = (payload: Parameters<typeof api.override>[0]) =>
    api.override(payload).catch((error) => onError(String(error.message ?? error)))

  const auto = state?.mode === 'AUTO'
  const maxLux = Math.max(500, ...modes.map((mode) => mode.target_lux))

  return (
    <section className="glass rise" style={{ display: 'flex', flexDirection: 'column' }}>
      <SectionHead
        eyebrow="Control"
        title={<>Set the <span className="accent-serif">room</span></>}
        right={
          /* Ramps the panel 0-100-0. If nothing visibly happens, PWM is reaching the
             pin but the wiring cannot act on it. */
          <button
            className="ghost"
            style={{ fontSize: 12, padding: '7px 13px' }}
            disabled={!state?.device_connected}
            title="Ramp the panel up and down to confirm it physically responds"
            onClick={() => api.sweepPanel().catch((e) => onError(String(e.message ?? e)))}
          >
            Test panel
          </button>
        }
      />

      {/* AUTO / MANUAL as one switch. */}
      <div className="inset between" style={{ padding: '12px 14px 12px 18px', marginBottom: 22 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{auto ? 'AUTO is driving' : 'Manual hold'}</div>
          <div style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 2 }}>
            {auto ? 'the model sets the level every second' : 'tap to hand control back to the model'}
          </div>
        </div>
        <button
          className={auto ? 'primary' : ''}
          onClick={() => send({ auto: true })}
          disabled={auto}
          style={{ minWidth: 112 }}
        >
          {auto ? 'Auto on' : 'Return to auto'}
        </button>
      </div>

      <div className="between" style={{ marginBottom: 12 }}>
        <span className="eyebrow">Brightness</span>
        <span className="metric" style={{ fontSize: 30, color: 'var(--amber)' }}>
          {slider}<span className="unit">%</span>
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        value={slider}
        aria-label="Panel brightness"
        style={{ '--fill': `${slider}%` } as CSSProperties}
        onChange={(event) => setSlider(Number(event.target.value))}
        onPointerDown={() => setDragging(true)}
        // Commit on release rather than on every change event, so dragging across
        // the track does not fire a hundred commands at the ESP32.
        onPointerUp={() => {
          setDragging(false)
          send({ brightness: slider / 100 })
        }}
      />
      <div className="between" style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 8, marginBottom: 24 }}>
        <span>off</span><span>release to apply</span><span>full</span>
      </div>

      <div className="eyebrow" style={{ marginBottom: 10 }}>Modes</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(118px, 1fr))', gap: 8 }}>
        {modes.map((mode) => {
          const active = state?.active_mode_id === mode.id
          const strength = Math.min(1, mode.target_lux / maxLux)
          return (
            <button
              key={mode.id}
              onClick={() => send({ mode_id: mode.id })}
              title={`${mode.target_lux} lux target`}
              style={{
                borderRadius: 16, padding: '12px 14px', textAlign: 'left',
                display: 'flex', flexDirection: 'column', gap: 6,
                borderColor: active ? 'rgba(255,184,77,0.65)' : undefined,
                background: active ? 'linear-gradient(160deg, rgba(255,184,77,0.22), rgba(255,184,77,0.05))' : undefined,
              }}
            >
              <span className="row" style={{ gap: 8 }}>
                {/* Swatch brightness tracks the mode's target. */}
                <span style={{
                  width: 10, height: 10, borderRadius: '50%', background: 'var(--amber)',
                  opacity: 0.2 + 0.8 * strength, boxShadow: `0 0 ${4 + strength * 10}px var(--amber)`,
                }} />
                <span style={{ fontWeight: 700 }}>{mode.name}</span>
              </span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-faint)', fontWeight: 400 }}>
                {mode.target_lux} lx
              </span>
            </button>
          )
        })}
      </div>
      {modes.length === 0 && (
        <span style={{ fontSize: 12.5, color: 'var(--text-faint)' }}>
          No modes defined yet — add one in Setup.
        </span>
      )}
    </section>
  )
}
