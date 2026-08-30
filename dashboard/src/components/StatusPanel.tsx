/*
 * The top row: what the system is doing right now.
 *
 * Four tiles, ordered by how often someone looks at them — mode first (it explains
 * everything else), then the panel output it produced, then the two sensor readings
 * that drove it.
 */

import type { SystemState } from '../api'

function Tile({
  label, value, unit, accent, sub,
}: {
  label: string
  value: string
  unit?: string
  accent?: string
  sub?: string
}) {
  return (
    <div className="card" style={{ padding: '16px 18px' }}>
      <div className="label">{label}</div>
      <div
        className="metric"
        style={{ fontSize: 34, marginTop: 10, color: accent ?? 'var(--text)' }}
      >
        {value}
        {unit && <span className="unit">{unit}</span>}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-faint)', marginTop: 7, minHeight: 16 }}>
        {sub ?? ''}
      </div>
    </div>
  )
}

function formatCountdown(seconds: number | null) {
  if (seconds == null) return undefined
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.floor(seconds % 60)
  return `reverts to AUTO in ${minutes}:${String(remainder).padStart(2, '0')}`
}

export function StatusPanel({ state }: { state: SystemState | null }) {
  const manual = state?.mode === 'MANUAL'

  // With no device attached the server still holds the last values it was sent, but
  // they describe a moment that has passed. Showing them unlabelled would present
  // stale data as a live reading, so the sensor tiles go blank instead.
  const live = !!state?.readings_live

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
        gap: 'var(--gap)',
      }}
    >
      <Tile
        label="Mode"
        value={state?.mode ?? '—'}
        accent={manual ? 'var(--violet)' : 'var(--teal)'}
        sub={
          manual
            ? state?.active_mode_name
              ? `${state.active_mode_name} · ${formatCountdown(state.manual_seconds_remaining) ?? ''}`
              : formatCountdown(state?.manual_seconds_remaining ?? null)
            : 'following the model'
        }
      />
      <Tile
        label="Panel output"
        value={live && state ? (state.brightness * 100).toFixed(0) : '—'}
        unit={live ? '%' : undefined}
        accent={live ? 'var(--amber)' : 'var(--text-faint)'}
        sub={
          // Rated wattage is a config value; tolerate a server that has not sent it.
          !live
            ? 'no device'
            : state?.panel_rated_watts
              ? `${(state.brightness * state.panel_rated_watts).toFixed(1)} W of ${state.panel_rated_watts.toFixed(0)} W`
              : undefined
        }
      />
      <Tile
        label="Indoor"
        value={live && state ? Math.round(state.indoor_lux).toLocaleString() : '—'}
        unit={live ? 'lx' : undefined}
        sub={live ? 'room sensor' : state?.sensor_error ?? 'no reading'}
      />
      <Tile
        label="Outdoor"
        value={live && state ? Math.round(state.outdoor_lux).toLocaleString() : '—'}
        unit={live ? 'lx' : undefined}
        sub={live ? 'reference sensor' : state?.sensor_error ?? 'no reading'}
      />
    </div>
  )
}
