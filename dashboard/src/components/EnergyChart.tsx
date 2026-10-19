/*
 * The centrepiece: what the system actually drew versus what the incumbent
 * always-100% panel would have drawn, at one-second resolution.
 *
 * The gap between the two areas *is* the saving, so the chart is built to make that
 * gap the most legible thing on it: the hypothetical is a flat dimmed ceiling, the
 * actual is a filled amber area beneath it, and the space between them is tinted teal.
 * Reading the saving requires no arithmetic and no legend lookup.
 *
 * Rolling window only — a server restart starts it over. The long-term record is
 * the History tab.
 */

import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { Point, SystemState } from '../api'

function clockLabel(seconds: number) {
  const date = new Date(seconds * 1000)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`
}

function ChartTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null
  const point = payload[0].payload as Point & { saved: number }
  const savedPercent = point.full_watts > 0 ? (point.saved / point.full_watts) * 100 : 0

  return (
    <div
      className="card"
      style={{ padding: '11px 13px', minWidth: 168, boxShadow: 'var(--shadow)' }}
    >
      <div className="label" style={{ marginBottom: 8 }}>{clockLabel(point.t)}</div>
      <div className="stack" style={{ gap: 5, fontSize: 12 }}>
        <div className="between">
          <span style={{ color: 'var(--text-dim)' }}>Actual</span>
          <span className="metric" style={{ color: 'var(--amber)' }}>
            {point.actual_watts.toFixed(1)} W
          </span>
        </div>
        <div className="between">
          <span style={{ color: 'var(--text-dim)' }}>At 100%</span>
          <span className="metric" style={{ color: 'var(--text-faint)' }}>
            {point.full_watts.toFixed(1)} W
          </span>
        </div>
        <div
          className="between"
          style={{ borderTop: '1px solid var(--border)', paddingTop: 5, marginTop: 2 }}
        >
          <span style={{ color: 'var(--text-dim)' }}>Saved</span>
          <span className="metric" style={{ color: 'var(--teal)' }}>
            {savedPercent.toFixed(0)}%
          </span>
        </div>
      </div>
    </div>
  )
}

export function EnergyChart({
  points, state,
}: {
  points: Point[]
  state: SystemState | null
}) {
  // The teal band is drawn as a stacked area sitting on top of the actual usage, so
  // its height is literally the difference between the two lines.
  const data = points.map((point) => ({
    ...point,
    saved: Math.max(0, point.full_watts - point.actual_watts),
  }))

  const energy = state?.energy
  const windowSeconds = points.length > 1 ? points[points.length - 1].t - points[0].t : 0

  return (
    <div className="card" style={{ padding: '20px 20px 12px' }}>
      <div className="between" style={{ marginBottom: 18 }}>
        <div>
          <div className="label">Power draw · live</div>
          <div style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 4 }}>
            {points.length} samples over {Math.round(windowSeconds)}s · in-memory · full record in History
          </div>
        </div>

        <div className="row" style={{ gap: 26 }}>
          <div style={{ textAlign: 'right' }}>
            <div className="label">Energy saved</div>
            <div
              className="metric"
              style={{ fontSize: 26, color: 'var(--teal)', marginTop: 5 }}
            >
              {(energy?.savings_percent ?? 0).toFixed(0)}
              <span className="unit">%</span>
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="label">This session</div>
            <div
              className="metric"
              style={{ fontSize: 26, color: 'var(--text-dim)', marginTop: 5 }}
            >
              {(energy?.saved_wh ?? 0).toFixed(2)}
              <span className="unit">Wh</span>
            </div>
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={data} margin={{ top: 4, right: 24, bottom: 0, left: -12 }}>
          <defs>
            <linearGradient id="fill-actual" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--amber)" stopOpacity={0.55} />
              <stop offset="100%" stopColor="var(--amber)" stopOpacity={0.06} />
            </linearGradient>
            <linearGradient id="fill-saved" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--teal)" stopOpacity={0.30} />
              <stop offset="100%" stopColor="var(--teal)" stopOpacity={0.10} />
            </linearGradient>
          </defs>

          <CartesianGrid stroke="var(--border)" strokeDasharray="2 5" vertical={false} />
          <XAxis
            dataKey="t"
            tickFormatter={clockLabel}
            stroke="var(--text-faint)"
            tick={{ fontSize: 10.5, fontFamily: 'var(--mono)' }}
            tickLine={false}
            axisLine={{ stroke: 'var(--border)' }}
            minTickGap={54}
          />
          <YAxis
            stroke="var(--text-faint)"
            tick={{ fontSize: 10.5, fontFamily: 'var(--mono)' }}
            tickLine={false}
            axisLine={false}
            width={46}
            unit="W"
          />
          <Tooltip content={<ChartTooltip />} cursor={{ stroke: 'var(--border-hi)' }} />

          {/* Stack order matters: actual on the bottom, the saving stacked above it,
              so together they reach the always-100% ceiling. */}
          <Area
            type="monotone"
            dataKey="actual_watts"
            stackId="power"
            stroke="var(--amber)"
            strokeWidth={1.75}
            fill="url(#fill-actual)"
            isAnimationActive={false}
            name="Actual"
          />
          <Area
            type="monotone"
            dataKey="saved"
            stackId="power"
            stroke="var(--teal)"
            strokeWidth={1.25}
            strokeDasharray="4 3"
            fill="url(#fill-saved)"
            isAnimationActive={false}
            name="Saved vs 100%"
          />
        </AreaChart>
      </ResponsiveContainer>

      <div className="row" style={{ gap: 20, paddingTop: 6, paddingLeft: 42 }}>
        <div className="row" style={{ gap: 7 }}>
          <span style={{
            width: 11, height: 11, borderRadius: 3, background: 'var(--amber)',
          }} />
          <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>Actual draw</span>
        </div>
        <div className="row" style={{ gap: 7 }}>
          <span style={{
            width: 11, height: 11, borderRadius: 3,
            background: 'var(--teal)', opacity: 0.45,
          }} />
          <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>
            Saved vs always-100%
          </span>
        </div>
      </div>
    </div>
  )
}
