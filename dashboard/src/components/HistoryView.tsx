/*
 * The long view: everything the SQLite history recorded while a real ESP32 was
 * connected.
 *
 * Only real-device sessions are ever stored — a mock device or a simulated reading
 * never reaches this page — so every number here is a measurement. Time with no
 * device is drawn as a gap in the line, never bridged, because a line across it would
 * claim readings that were never taken.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { historyApi } from '../api'
import type {
  DeviceSessionRow, HistoryPoint, HistorySummary, LightChange,
} from '../api'

type Range = { key: string; label: string; seconds: number | null }

const RANGES: Range[] = [
  { key: '1h', label: '1 h', seconds: 3600 },
  { key: '6h', label: '6 h', seconds: 6 * 3600 },
  { key: '24h', label: '24 h', seconds: 24 * 3600 },
  { key: '7d', label: '7 d', seconds: 7 * 86400 },
  { key: '30d', label: '30 d', seconds: 30 * 86400 },
  { key: 'all', label: 'All', seconds: null },
]

const REFRESH_MS = 15_000

const SOURCE_LABEL: Record<string, string> = {
  auto: 'AUTO model',
  hold: 'Manual hold',
  voice: 'Voice',
  voice_typed: 'Typed command',
  dashboard: 'Dashboard',
}

const SOURCE_COLOR: Record<string, string> = {
  auto: 'var(--teal)',
  hold: 'var(--text-dim)',
  voice: 'var(--violet)',
  voice_typed: 'var(--violet)',
  dashboard: 'var(--amber)',
}

/* --- formatting ------------------------------------------------------------ */

function pad(value: number) {
  return String(value).padStart(2, '0')
}

function timeLabel(seconds: number, withDate: boolean) {
  const date = new Date(seconds * 1000)
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (!withDate) return clock
  return `${pad(date.getDate())}/${pad(date.getMonth() + 1)} ${clock}`
}

function fullTimestamp(seconds: number) {
  const date = new Date(seconds * 1000)
  return `${date.toLocaleDateString()} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function duration(seconds: number) {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h ${minutes % 60}m`
  return `${Math.floor(hours / 24)}d ${hours % 24}h`
}

function percent(value: number | null | undefined) {
  return value == null ? '—' : `${Math.round(value * 100)}%`
}

/* --- chart data ----------------------------------------------------------- */

type ChartPoint = {
  t: number
  indoor: number | null
  outdoor: number | null
  brightness: number | null
  readings: number
  unavailable: number
  sensor_error?: string | null
}

/**
 * Bucket rows to chart points, with an explicit null wherever the device was absent.
 * Recharts bridges consecutive points, so a gap has to be a real null-valued point
 * or the line would run straight across a stretch nobody measured.
 */
function toChartPoints(rows: HistoryPoint[], step: number): ChartPoint[] {
  const points: ChartPoint[] = []
  let previous: HistoryPoint | null = null
  for (const row of rows) {
    if (previous && row.t_start - previous.t_start > step * 2.5) {
      points.push({
        t: previous.t_start + step, indoor: null, outdoor: null, brightness: null,
        readings: 0, unavailable: 0,
      })
    }
    points.push({
      t: row.t_start,
      indoor: row.indoor_lux_avg,
      outdoor: row.outdoor_lux_avg,
      brightness: row.brightness_avg == null ? null : row.brightness_avg * 100,
      readings: row.readings,
      unavailable: row.unavailable,
      sensor_error: row.sensor_error,
    })
    previous = row
  }
  return points
}

/* --- pieces --------------------------------------------------------------- */

function Stat({
  label, value, unit, accent, sub,
}: {
  label: string
  value: string
  unit?: string
  accent?: string
  sub?: string
}) {
  return (
    <div className="card" style={{ padding: '15px 17px' }}>
      <div className="label">{label}</div>
      <div className="metric" style={{ fontSize: 28, marginTop: 9, color: accent ?? 'var(--text)' }}>
        {value}
        {unit && <span className="unit">{unit}</span>}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-faint)', marginTop: 6, minHeight: 16 }}>
        {sub ?? ''}
      </div>
    </div>
  )
}

function HistoryTooltip({ active, payload, withDate }: any) {
  if (!active || !payload?.length) return null
  const point = payload[0].payload as ChartPoint
  if (point.readings === 0 && point.unavailable === 0) return null
  return (
    <div className="card" style={{ padding: '11px 13px', minWidth: 180, boxShadow: 'var(--shadow)' }}>
      <div className="label" style={{ marginBottom: 8 }}>{timeLabel(point.t, withDate)}</div>
      {point.readings === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--rose)' }}>
          sensor unavailable{point.sensor_error ? `: ${point.sensor_error}` : ''}
        </div>
      ) : (
        <div className="stack" style={{ gap: 5, fontSize: 12 }}>
          <div className="between">
            <span style={{ color: 'var(--text-dim)' }}>Panel</span>
            <span className="metric" style={{ color: 'var(--amber)' }}>
              {point.brightness?.toFixed(0)}%
            </span>
          </div>
          <div className="between">
            <span style={{ color: 'var(--text-dim)' }}>Indoor</span>
            <span className="metric">{Math.round(point.indoor ?? 0).toLocaleString()} lx</span>
          </div>
          <div className="between">
            <span style={{ color: 'var(--text-dim)' }}>Outdoor</span>
            <span className="metric" style={{ color: 'var(--violet)' }}>
              {Math.round(point.outdoor ?? 0).toLocaleString()} lx
            </span>
          </div>
          <div style={{ color: 'var(--text-faint)', fontSize: 11, marginTop: 2 }}>
            {point.readings} reading{point.readings === 1 ? '' : 's'}
          </div>
        </div>
      )}
    </div>
  )
}

const axisTick = { fontSize: 10.5, fontFamily: 'var(--mono)' }

function HistoryChart({
  points, withDate,
}: {
  points: ChartPoint[]
  withDate: boolean
}) {
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="label" style={{ paddingLeft: 4 }}>Panel output</div>
      <ResponsiveContainer width="100%" height={170}>
        <ComposedChart data={points} syncId="history" margin={{ top: 6, right: 54, bottom: 0, left: -6 }}>
          <defs>
            <linearGradient id="history-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--amber)" stopOpacity={0.5} />
              <stop offset="100%" stopColor="var(--amber)" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--border)" strokeDasharray="2 5" vertical={false} />
          <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} hide />
          <YAxis
            domain={[0, 100]} unit="%" width={50} stroke="var(--text-faint)"
            tick={axisTick} tickLine={false} axisLine={false}
          />
          <Tooltip content={<HistoryTooltip withDate={withDate} />} cursor={{ stroke: 'var(--border-hi)' }} />
          <Area
            type="stepAfter" dataKey="brightness" stroke="var(--amber)" strokeWidth={1.5}
            fill="url(#history-fill)" isAnimationActive={false} connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>

      <div className="label" style={{ paddingLeft: 4, marginTop: 10 }}>Light levels</div>
      <ResponsiveContainer width="100%" height={210}>
        <ComposedChart data={points} syncId="history" margin={{ top: 6, right: 0, bottom: 0, left: -6 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="2 5" vertical={false} />
          <XAxis
            dataKey="t" type="number" domain={['dataMin', 'dataMax']}
            tickFormatter={(value) => timeLabel(value, withDate)}
            stroke="var(--text-faint)" tick={axisTick} tickLine={false}
            axisLine={{ stroke: 'var(--border)' }} minTickGap={60}
          />
          {/* Outdoor daylight is often 10-100x the indoor reading, so each gets its
              own axis — on a shared one the indoor line would sit flat on zero. */}
          <YAxis
            yAxisId="indoor" width={50} stroke="var(--text-faint)" tick={axisTick}
            tickLine={false} axisLine={false} unit="lx"
          />
          <YAxis
            yAxisId="outdoor" orientation="right" width={54} stroke="var(--violet)"
            tick={axisTick} tickLine={false} axisLine={false}
          />
          <Tooltip content={<HistoryTooltip withDate={withDate} />} cursor={{ stroke: 'var(--border-hi)' }} />
          <Line
            yAxisId="indoor" type="monotone" dataKey="indoor" stroke="var(--text)"
            strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls={false}
          />
          <Line
            yAxisId="outdoor" type="monotone" dataKey="outdoor" stroke="var(--violet)"
            strokeWidth={1.25} strokeDasharray="4 3" dot={false} isAnimationActive={false}
            connectNulls={false}
          />
        </ComposedChart>
      </ResponsiveContainer>

      <div className="row" style={{ gap: 20, paddingTop: 4, paddingLeft: 46, flexWrap: 'wrap' }}>
        {[
          ['var(--amber)', 'Panel output'],
          ['var(--text)', 'Indoor lux (left axis)'],
          ['var(--violet)', 'Outdoor lux (right axis)'],
        ].map(([color, label]) => (
          <div key={label} className="row" style={{ gap: 7 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: color }} />
            <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ChangesTable({ changes }: { changes: LightChange[] }) {
  if (!changes.length) {
    return (
      <div style={{ fontSize: 12.5, color: 'var(--text-faint)', padding: '8px 0' }}>
        No light changes recorded in this range.
      </div>
    )
  }
  return (
    <div style={{ maxHeight: 420, overflowY: 'auto', margin: '0 -4px' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          <tr style={{ textAlign: 'left' }}>
            {['When', 'Change', 'Source', 'Detail'].map((heading) => (
              <th
                key={heading}
                className="label"
                style={{
                  padding: '6px 4px', position: 'sticky', top: 0,
                  background: 'var(--surface-hi)', borderBottom: '1px solid var(--border)',
                }}
              >
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => {
            const up = change.from_brightness != null
              && change.to_brightness > change.from_brightness
            return (
              <tr key={change.id} style={{ borderBottom: '1px solid var(--border)' }}>
                <td className="metric" style={{ padding: '8px 4px', fontSize: 11.5, color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>
                  {fullTimestamp(change.t)}
                </td>
                <td className="metric" style={{ padding: '8px 4px', whiteSpace: 'nowrap' }}>
                  <span style={{ color: 'var(--text-faint)' }}>{percent(change.from_brightness)}</span>
                  <span style={{ color: up ? 'var(--amber)' : 'var(--teal)', margin: '0 6px' }}>
                    {change.from_brightness == null ? '•' : up ? '↑' : '↓'}
                  </span>
                  <span style={{ color: 'var(--amber)' }}>{percent(change.to_brightness)}</span>
                </td>
                <td style={{ padding: '8px 4px', whiteSpace: 'nowrap' }}>
                  <span style={{ color: SOURCE_COLOR[change.source] ?? 'var(--text-dim)', fontWeight: 500 }}>
                    {SOURCE_LABEL[change.source] ?? change.source}
                  </span>
                </td>
                <td style={{ padding: '8px 4px', color: 'var(--text-dim)' }}>
                  {change.transcript ? <>“{change.transcript}”</> : change.reason}
                  {change.mode_name && (
                    <span style={{ color: 'var(--text-faint)' }}> · {change.mode_name}</span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function SessionsList({ sessions }: { sessions: DeviceSessionRow[] }) {
  if (!sessions.length) {
    return <div style={{ fontSize: 12.5, color: 'var(--text-faint)' }}>No sessions in this range.</div>
  }
  return (
    <div className="stack" style={{ gap: 0, maxHeight: 420, overflowY: 'auto' }}>
      {sessions.map((session) => {
        const open = session.disconnected_at == null
        const end = session.disconnected_at ?? Date.now() / 1000
        return (
          <div
            key={session.id}
            className="between"
            style={{ padding: '9px 0', borderBottom: '1px solid var(--border)', alignItems: 'flex-start' }}
          >
            <div>
              <div style={{ fontSize: 12.5, fontWeight: 500 }}>
                {session.device_id}
                <span style={{ color: 'var(--text-faint)', fontWeight: 400 }}>
                  {session.firmware ? ` · fw ${session.firmware}` : ''}
                </span>
              </div>
              <div className="metric" style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 4 }}>
                {fullTimestamp(session.connected_at)}
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="metric" style={{ fontSize: 13, color: open ? 'var(--green)' : 'var(--text-dim)' }}>
                {duration(end - session.connected_at)}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 4 }}>
                {open ? 'recording now' : (session.end_reason ?? '').replace(/_/g, ' ')}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* --- the view ------------------------------------------------------------- */

export function HistoryView({ onError }: { onError: (message: string) => void }) {
  const [rangeKey, setRangeKey] = useState('24h')
  // A picked calendar day overrides the rolling presets.
  const [day, setDay] = useState('')
  const [summary, setSummary] = useState<HistorySummary | null>(null)
  const [points, setPoints] = useState<ChartPoint[]>([])
  const [step, setStep] = useState(5)
  const [changes, setChanges] = useState<LightChange[]>([])
  const [sessions, setSessions] = useState<DeviceSessionRow[]>([])
  const [loading, setLoading] = useState(false)

  // A picked day is a fixed window; a rolling preset has no end ("up to now"), is
  // re-anchored to the current time on every refresh, and keeps refreshing itself.
  const dayWindow = useMemo(() => {
    if (!day) return null
    const start = new Date(`${day}T00:00:00`).getTime() / 1000
    return { start, end: start + 86400 }
  }, [day])
  const live = dayWindow == null

  const load = useCallback(() => {
    const range = RANGES.find((r) => r.key === rangeKey) ?? RANGES[2]
    const start = dayWindow
      ? dayWindow.start
      : range.seconds == null ? null : Date.now() / 1000 - range.seconds
    const end = dayWindow?.end ?? null
    setLoading(true)
    Promise.all([
      historyApi.summary(start, end),
      historyApi.samples(start, end),
      historyApi.changes(start, end),
      historyApi.sessions(start, end),
    ])
      .then(([nextSummary, samples, nextChanges, nextSessions]) => {
        setSummary(nextSummary)
        setStep(samples.step_seconds)
        setPoints(toChartPoints(samples.points, samples.step_seconds))
        setChanges(nextChanges)
        setSessions(nextSessions)
      })
      .catch((error) => onError(`history: ${String(error.message ?? error)}`))
      .finally(() => setLoading(false))
  }, [rangeKey, dayWindow, onError])

  useEffect(() => {
    load()
    if (!live) return
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load, live])

  const span = points.length > 1 ? points[points.length - 1].t - points[0].t : 0
  const withDate = span > 20 * 3600
  const empty = summary != null && summary.buckets === 0 && summary.changes === 0

  const exportStart = dayWindow?.start ?? null
  const exportEnd = dayWindow?.end ?? null

  return (
    <div className="stack" style={{ gap: 'var(--gap)' }}>
      <div className="card" style={{ padding: '14px 18px' }}>
        <div className="between" style={{ flexWrap: 'wrap', gap: 14 }}>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            {RANGES.map((range) => (
              <button
                key={range.key}
                className={!day && rangeKey === range.key ? 'primary' : 'ghost'}
                style={{ padding: '6px 12px' }}
                onClick={() => { setDay(''); setRangeKey(range.key) }}
              >
                {range.label}
              </button>
            ))}
            <input
              type="date"
              value={day}
              onChange={(event) => setDay(event.target.value)}
              title="Show one calendar day"
              style={{ width: 150, marginLeft: 6 }}
            />
          </div>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
              {loading ? 'loading…' : summary?.recording ? '● recording' : 'not recording — no ESP32 connected'}
            </span>
            {/* Exports follow a picked day; with a rolling preset they export everything,
                since "the last hour" means something different by the time it's opened. */}
            <a className="button-link" href={historyApi.exportUrl('samples', exportStart, exportEnd)}>
              Export samples CSV
            </a>
            <a className="button-link" href={historyApi.exportUrl('light_changes', exportStart, exportEnd)}>
              Export changes CSV
            </a>
          </div>
        </div>
      </div>

      {empty ? (
        <div className="card" style={{ padding: '40px 24px', textAlign: 'center' }}>
          <div style={{ fontSize: 15, fontWeight: 500 }}>Nothing recorded in this range</div>
          <div style={{ fontSize: 12.5, color: 'var(--text-faint)', marginTop: 8, maxWidth: 520, marginInline: 'auto' }}>
            History is written only while a real ESP32 is connected — from its hello until it
            disconnects. The mock device and simulated readings are never stored.
          </div>
        </div>
      ) : (
        <>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
              gap: 'var(--gap)',
            }}
          >
            <Stat
              label="Energy used"
              value={summary ? summary.actual_wh.toFixed(summary.actual_wh < 10 ? 2 : 0) : '—'}
              unit="Wh"
              accent="var(--amber)"
              sub={summary ? `vs ${summary.full_wh.toFixed(summary.full_wh < 10 ? 2 : 0)} Wh at 100%` : undefined}
            />
            <Stat
              label="Energy saved"
              value={summary?.savings_percent == null ? '—' : summary.savings_percent.toFixed(0)}
              unit={summary?.savings_percent == null ? undefined : '%'}
              accent="var(--teal)"
              sub={summary ? `${summary.saved_wh.toFixed(2)} Wh saved` : undefined}
            />
            <Stat
              label="Light changes"
              value={summary ? String(summary.changes) : '—'}
              sub={summary
                ? Object.entries(summary.changes_by_source)
                  .map(([source, n]) => `${n} ${(SOURCE_LABEL[source] ?? source).toLowerCase()}`)
                  .join(' · ')
                : undefined}
            />
            <Stat
              label="Device connected"
              value={summary ? duration(summary.connected_seconds) : '—'}
              sub={summary
                ? `${sessions.length} session${sessions.length === 1 ? '' : 's'} · ${summary.readings.toLocaleString()} readings`
                : undefined}
            />
          </div>

          <div className="card" style={{ padding: '18px 18px 12px' }}>
            <div className="between" style={{ marginBottom: 12 }}>
              <div className="label">Recorded history</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
                {points.length.toLocaleString()} points · {step}s each
                {summary?.unavailable ? ` · ${summary.unavailable} sensor-unavailable readings` : ''}
              </div>
            </div>
            <HistoryChart points={points} withDate={withDate} />
          </div>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 2fr) minmax(280px, 1fr)',
              gap: 'var(--gap)',
              alignItems: 'start',
            }}
          >
            <div className="card">
              <div className="between" style={{ marginBottom: 10 }}>
                <div className="label">Light changes</div>
                <div style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
                  newest first · AUTO moves under 2% not logged
                </div>
              </div>
              <ChangesTable changes={changes} />
            </div>
            <div className="card">
              <div className="label" style={{ marginBottom: 6 }}>Device sessions</div>
              <SessionsList sessions={sessions} />
            </div>
          </div>
        </>
      )}
    </div>
  )
}
