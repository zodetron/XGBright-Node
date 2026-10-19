/*
 * The room, at a glance: one huge number for how lit it is, inside a ring that also
 * marks where AUTO would put it.
 *
 * With no device reporting the ring empties and the number becomes "—". The last
 * known value is never shown here, because it would read as the room's current state.
 */

import type { SystemState } from '../api'

const RING = 2 * Math.PI * 118

function countdown(seconds: number | null) {
  if (seconds == null) return null
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
}

function Gauge({ value, predicted, live }: {
  value: number
  predicted: number | null
  live: boolean
}) {
  const filled = live ? Math.max(0, Math.min(1, value)) : 0
  // Where the ring's tick for the model's suggestion sits, measured from 12 o'clock.
  const tickAngle = predicted == null ? null : -90 + 360 * Math.max(0, Math.min(1, predicted))

  return (
    <div style={{ position: 'relative', width: 280, height: 280, flex: 'none' }}>
      {/* The bloom behind the ring scales with the real output. */}
      <div
        style={{
          position: 'absolute', inset: 30, borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(255,200,120,0.9), rgba(255,150,50,0.25) 45%, transparent 70%)',
          opacity: live ? 0.12 + filled * 0.75 : 0,
          transform: `scale(${0.75 + filled * 0.45})`,
          filter: 'blur(18px)',
          transition: 'opacity 0.8s ease, transform 0.8s ease',
        }}
      />
      <svg viewBox="0 0 280 280" style={{ position: 'relative', width: '100%', height: '100%' }}>
        <defs>
          <linearGradient id="ring-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#ffe0ad" />
            <stop offset="100%" stopColor="#ff9a2e" />
          </linearGradient>
        </defs>
        <circle cx="140" cy="140" r="118" fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth="14" />
        {/* Minute-style ticks give the ring scale without numbers. */}
        {Array.from({ length: 40 }, (_, index) => {
          const angle = (index / 40) * 2 * Math.PI
          const inner = index % 10 === 0 ? 92 : 97
          return (
            <line
              key={index}
              x1={140 + Math.sin(angle) * inner} y1={140 - Math.cos(angle) * inner}
              x2={140 + Math.sin(angle) * 101} y2={140 - Math.cos(angle) * 101}
              stroke="rgba(255,255,255,0.16)" strokeWidth={index % 10 === 0 ? 1.6 : 1}
            />
          )
        })}
        {/* A zero-length arc with round caps still draws a dot, so omit it entirely. */}
        {filled > 0.005 && <circle
          cx="140" cy="140" r="118" fill="none"
          stroke="url(#ring-grad)" strokeWidth="14" strokeLinecap="round"
          strokeDasharray={`${RING * filled} ${RING}`}
          transform="rotate(-90 140 140)"
          style={{ transition: 'stroke-dasharray 0.8s cubic-bezier(0.2,0.8,0.2,1)',
                   filter: 'drop-shadow(0 0 10px rgba(255,170,60,0.55))' }}
        />}
        {live && tickAngle != null && (
          <g transform={`rotate(${tickAngle + 90} 140 140)`}>
            <line x1="140" y1="10" x2="140" y2="34" stroke="var(--teal)" strokeWidth="3" strokeLinecap="round" />
          </g>
        )}
      </svg>
      <div
        style={{
          position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
        }}
      >
        <div className="metric" style={{ fontSize: live ? 92 : 72, fontWeight: 800, color: live ? 'var(--text)' : 'var(--text-faint)' }}>
          {live ? Math.round(filled * 100) : '—'}
          {live && <span className="unit" style={{ fontSize: 30 }}>%</span>}
        </div>
        <div className="eyebrow" style={{ marginTop: 8 }}>panel output</div>
      </div>
    </div>
  )
}

export function Hero({ state }: { state: SystemState | null }) {
  const live = !!state?.device_connected && !!state?.readings_live
  const manual = state?.mode === 'MANUAL'
  const watts = state ? state.brightness * state.panel_rated_watts : 0
  const timer = countdown(state?.manual_seconds_remaining ?? null)

  let headline
  if (!state?.device_connected) {
    headline = <>The room is <span className="accent-serif">offline</span>.</>
  } else if (!live) {
    headline = <>Connected, but <span className="accent-serif">no readings</span>.</>
  } else if (manual) {
    headline = state.active_mode_name
      ? <>Holding <span className="accent-serif">{state.active_mode_name}</span>.</>
      : <>Holding a <span className="accent-serif">manual</span> level.</>
  } else {
    headline = <>Following the <span className="accent-serif">daylight</span>.</>
  }

  let detail
  if (!state?.device_connected) {
    detail = 'Power the ESP32 and it will appear here within seconds.'
  } else if (!live) {
    detail = state?.sensor_error ?? 'The board is on the socket but is not sending light readings.'
  } else if (manual) {
    detail = `Sensor input is ignored while a manual level is held${timer ? ` — AUTO resumes in ${timer}` : ''}.`
  } else {
    detail = `The XGBoost model sets the panel from ${Math.round(state.indoor_lux).toLocaleString()} lx indoors and ${Math.round(state.outdoor_lux).toLocaleString()} lx of daylight, once a second.`
  }

  return (
    <section className="glass rise" style={{ padding: 30, overflow: 'hidden' }}>
      <div className="row" style={{ gap: 34, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'center' }}>
        <Gauge value={state?.brightness ?? 0} predicted={state?.predicted_brightness ?? null} live={live} />

        <div style={{ flex: '1 1 260px', minWidth: 0 }}>
          <div className="row" style={{ gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
            <span
              className="chip"
              style={{
                color: manual ? 'var(--violet)' : 'var(--teal)',
                borderColor: manual ? 'rgba(169,150,255,0.35)' : 'rgba(63,224,197,0.35)',
              }}
            >
              {state?.mode ?? '—'}
            </span>
            {manual && timer && <span className="chip mono">⟲ {timer}</span>}
            {live && <span className="chip mono">{watts.toFixed(1)} W</span>}
          </div>

          <h1
            style={{
              margin: 0, fontFamily: 'var(--display)', fontWeight: 800,
              fontSize: 'clamp(34px, 4.2vw, 54px)', letterSpacing: '-0.04em', lineHeight: 0.98,
            }}
          >
            {headline}
          </h1>
          <p style={{ margin: '16px 0 0', color: 'var(--text-dim)', fontSize: 14.5, maxWidth: 420, lineHeight: 1.6 }}>
            {detail}
          </p>

          {live && state?.predicted_brightness != null && (
            <div className="row" style={{ gap: 8, marginTop: 18, fontSize: 12, color: 'var(--text-faint)' }}>
              <span style={{ width: 14, height: 3, borderRadius: 2, background: 'var(--teal)' }} />
              AUTO would set {Math.round(state.predicted_brightness * 100)}%
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
