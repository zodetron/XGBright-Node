/*
 * Application shell and layout.
 *
 * Two columns on a wide screen: the live picture on the left (status, chart, voice),
 * configuration on the right. The split follows how the two halves are used — the left
 * is watched, the right is occasionally edited — so the thing that changes every second
 * never sits next to a text field someone is typing into.
 */

import { useCallback, useEffect, useState } from 'react'
import { api, useLiveData } from './api'
import type { Mode } from './api'
import { Controls } from './components/Controls'
import { EnergyChart } from './components/EnergyChart'
import { ModesManager } from './components/ModesManager'
import { SettingsPanel } from './components/SettingsPanel'
import { StatusPanel } from './components/StatusPanel'
import { VoicePanel } from './components/VoicePanel'

function ConnectionDot({ live, label }: { live: boolean; label: string }) {
  return (
    <div className="row" style={{ gap: 7 }}>
      <span
        style={{
          width: 7, height: 7, borderRadius: '50%',
          background: live ? 'var(--green)' : 'var(--text-faint)',
          boxShadow: live ? '0 0 9px var(--green)' : 'none',
          animation: live ? 'pulse 2.4s ease-in-out infinite' : 'none',
        }}
      />
      <span style={{ fontSize: 11.5, color: live ? 'var(--text-dim)' : 'var(--text-faint)' }}>
        {label}
      </span>
    </div>
  )
}

export default function App() {
  const { state, points, connected, lastVoice } = useLiveData()
  const [modes, setModes] = useState<Mode[]>([])
  const [error, setError] = useState<string | null>(null)

  const loadModes = useCallback(() => {
    api.modes().then(setModes).catch((cause) => setError(String(cause)))
  }, [])

  useEffect(loadModes, [loadModes])

  // Errors are transient notices, not state to be dismissed by hand.
  useEffect(() => {
    if (!error) return
    const timer = setTimeout(() => setError(null), 5000)
    return () => clearTimeout(timer)
  }, [error])

  return (
    <div style={{ maxWidth: 1440, margin: '0 auto', padding: '30px 26px 60px' }}>
      <header className="between" style={{ marginBottom: 26, flexWrap: 'wrap', gap: 14 }}>
        <div>
          <h1
            style={{
              margin: 0, fontSize: 21, fontWeight: 600, letterSpacing: '-0.015em',
            }}
          >
            Adaptive Lighting
          </h1>
          <div style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 3 }}>
            {state?.model.loaded
              ? `${state.model.model} · MAE ${state.model.metrics.mae?.toFixed(3)}`
              : 'model not loaded — AUTO is using the fallback heuristic'}
          </div>
        </div>
        <div className="row" style={{ gap: 18 }}>
          <ConnectionDot live={connected} label={connected ? 'Server live' : 'Reconnecting…'} />
          <ConnectionDot
            live={!!state?.device_connected}
            label={state?.device_connected ? 'ESP32 connected' : 'ESP32 offline'}
          />
        </div>
      </header>

      {error && (
        <div
          className="card"
          style={{
            marginBottom: 'var(--gap)', padding: '11px 15px',
            borderColor: 'var(--rose)', color: 'var(--rose)', fontSize: 12.5,
          }}
        >
          {error}
        </div>
      )}

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.75fr) minmax(320px, 1fr)',
          gap: 'var(--gap)',
          alignItems: 'start',
        }}
      >
        <div className="stack" style={{ gap: 'var(--gap)' }}>
          <StatusPanel state={state} />
          <EnergyChart points={points} state={state} />
          <VoicePanel lastVoice={lastVoice} state={state} onError={setError} />
        </div>

        <div className="stack" style={{ gap: 'var(--gap)' }}>
          <Controls state={state} modes={modes} onError={setError} />
          <ModesManager modes={modes} onChange={loadModes} onError={setError} />
          <SettingsPanel onError={setError} />
        </div>
      </div>
    </div>
  )
}
