/*
 * One glass strip of the four live readings, each with a trace of the last few
 * minutes from the rolling window. Blank while no device is reporting.
 */

import type { Point, SystemState } from '../api'
import { Sparkline } from './ui'

function Cell({
  label, value, unit, sub, color, trace,
}: {
  label: string
  value: string
  unit?: string
  sub: string
  color: string
  trace: number[]
}) {
  return (
    <div style={{ padding: '20px 22px 16px', minWidth: 0 }}>
      <div className="eyebrow">{label}</div>
      <div className="metric" style={{ fontSize: 38, marginTop: 10, color: value === '—' ? 'var(--text-faint)' : 'var(--text)' }}>
        {value}
        {unit && value !== '—' && <span className="unit">{unit}</span>}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-faint)', margin: '6px 0 10px', minHeight: 17, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {sub}
      </div>
      <Sparkline values={trace} color={color} />
    </div>
  )
}

export function Readouts({ state, points }: { state: SystemState | null; points: Point[] }) {
  const live = !!state?.device_connected && !!state?.readings_live
  const recent = live ? points.slice(-120) : []
  const fallback = state?.sensor_error ?? 'no device'

  return (
    <section className="glass readouts rise" style={{ padding: 0, overflow: 'hidden' }}>
      <Cell
        label="Indoor"
        value={live && state ? Math.round(state.indoor_lux).toLocaleString() : '—'}
        unit="lx"
        sub={live ? 'room sensor' : fallback}
        color="#f4f5f8"
        trace={recent.map((p) => p.indoor_lux)}
      />
      <Cell
        label="Daylight"
        value={live && state ? Math.round(state.outdoor_lux).toLocaleString() : '—'}
        unit="lx"
        sub={live ? 'reference sensor' : fallback}
        color="#a996ff"
        trace={recent.map((p) => p.outdoor_lux)}
      />
      <Cell
        label="Power draw"
        value={live && state ? (state.brightness * state.panel_rated_watts).toFixed(1) : '—'}
        unit="W"
        sub={state ? `of ${state.panel_rated_watts.toFixed(0)} W rated` : 'no device'}
        color="#ffb84d"
        trace={recent.map((p) => p.actual_watts)}
      />
      <Cell
        label="Saved this session"
        value={state && state.energy.full_wh > 0 ? state.energy.savings_percent.toFixed(0) : '—'}
        unit="%"
        sub={state && state.energy.full_wh > 0 ? `${state.energy.saved_wh.toFixed(2)} Wh vs always-100%` : 'since the server started'}
        color="#3fe0c5"
        trace={recent.map((p) => p.full_watts - p.actual_watts)}
      />
    </section>
  )
}
