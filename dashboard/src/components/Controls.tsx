/*
 * Manual override: put the room where you want it without speaking to it.
 *
 * Every control here goes through the same state machine the voice pipeline uses, so
 * a dashboard override sets MANUAL and starts the same revert timer — the two input
 * paths cannot disagree about what mode the system is in.
 */

import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Mode, SystemState } from '../api'

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

  return (
    <div className="card">
      <div className="between" style={{ marginBottom: 16 }}>
        <div className="row" style={{ gap: 10 }}>
          <span className="label">Manual override</span>
          {/* Ramps the panel 0-100-0. If nothing visibly happens, PWM is
              reaching the pin but the wiring cannot act on it. */}
          <button
            className="ghost"
            style={{ fontSize: 11, padding: '4px 9px' }}
            disabled={!state?.device_connected}
            title="Ramp the panel up and down to confirm it physically responds"
            onClick={() => api.sweepPanel().catch((e) =>
              onError(String(e.message ?? e)))}
          >
            Test panel
          </button>
        </div>
        <button
          className={state?.mode === 'AUTO' ? 'primary' : 'ghost'}
          onClick={() => send({ auto: true })}
          disabled={state?.mode === 'AUTO'}
        >
          {state?.mode === 'AUTO' ? 'Auto active' : 'Return to auto'}
        </button>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div className="between" style={{ marginBottom: 10 }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>Brightness</span>
          <span className="metric" style={{ fontSize: 17, color: 'var(--amber)' }}>
            {slider}
            <span className="unit">%</span>
          </span>
        </div>
        <input
          type="range"
          min={0}
          max={100}
          value={slider}
          onChange={(event) => setSlider(Number(event.target.value))}
          onPointerDown={() => setDragging(true)}
          // Commit on release rather than on every change event, so dragging across
          // the track does not fire a hundred commands at the ESP32.
          onPointerUp={() => {
            setDragging(false)
            send({ brightness: slider / 100 })
          }}
        />
      </div>

      <div className="label" style={{ marginBottom: 10 }}>Jump to mode</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {modes.map((mode) => {
          const active = state?.active_mode_id === mode.id
          return (
            <button
              key={mode.id}
              className={active ? 'primary' : ''}
              onClick={() => send({ mode_id: mode.id })}
              title={`${mode.target_lux} lux target`}
            >
              {mode.name}
              <span style={{ opacity: 0.6, marginLeft: 7, fontSize: 11 }}>
                {mode.target_lux}lx
              </span>
            </button>
          )
        })}
        {modes.length === 0 && (
          <span style={{ fontSize: 12.5, color: 'var(--text-faint)' }}>
            No modes defined yet — add one below.
          </span>
        )}
      </div>
    </div>
  )
}
