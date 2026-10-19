/*
 * Server client and live-data hook.
 *
 * The dashboard gets its data two ways: a WebSocket push for anything that changes
 * every second (state, chart points, voice results), and plain REST for the CRUD that
 * happens when a human clicks something. Mixing them is deliberate — pushing config
 * edits over the socket would mean inventing a request/response protocol on top of it
 * for no benefit.
 */

import { useEffect, useRef, useState } from 'react'

export type Mode = { id: string; name: string; target_lux: number }

export type Settings = {
  magnitude_steps: { slight: number; normal: number; large: number }
  manual_timeout_seconds: number
  panel_rated_watts: number
  rolling_window_seconds: number
  sensor_target_lux_default: number
  panel_full_output_lux: number
  history_interval_seconds: number
}

export type Energy = {
  actual_wh: number
  full_wh: number
  saved_wh: number
  savings_percent: number
  samples: number
}

export type SystemState = {
  type: 'state'
  mode: 'AUTO' | 'MANUAL'
  active_mode_id: string | null
  active_mode_name: string | null
  manual_seconds_remaining: number | null
  last_reason: string
  indoor_lux: number
  outdoor_lux: number
  brightness: number
  predicted_brightness: number | null
  device_connected: boolean
  device_id: string | null
  device_simulated: boolean
  readings_live: boolean
  sensor_error: string | null
  panel_rated_watts: number
  energy: Energy
  model: { loaded: boolean; model: string; metrics: Record<string, number> }
}

export type Point = {
  t: number
  indoor_lux: number
  outdoor_lux: number
  brightness: number
  actual_watts: number
  full_watts: number
  mode: string
  mode_name: string | null
}

export type VoiceResult = {
  transcript: string
  action: string
  applied: boolean
  brightness: number
  savings_percent: number
  confirmation_text: string
  intent: { kind: string; language: string }
}

/* --- HISTORY (SQLite, real-device sessions only) -------------------------- */

/** One stored bucket. Lux and brightness are null when no reading landed in it. */
export type HistoryPoint = {
  t_start: number
  t_end: number
  readings: number
  unavailable: number
  indoor_lux_avg: number | null
  indoor_lux_min: number | null
  indoor_lux_max: number | null
  outdoor_lux_avg: number | null
  outdoor_lux_min: number | null
  outdoor_lux_max: number | null
  brightness_avg: number | null
  actual_wh: number
  full_wh: number
  mode?: string | null
  mode_name?: string | null
  sensor_error?: string | null
}

export type LightChange = {
  id: number
  session_id: number
  t: number
  from_brightness: number | null
  to_brightness: number
  source: 'auto' | 'hold' | 'voice' | 'voice_typed' | 'dashboard'
  reason: string | null
  mode: string | null
  mode_name: string | null
  transcript: string | null
}

export type DeviceSessionRow = {
  id: number
  device_id: string
  firmware: string | null
  connected_at: number
  disconnected_at: number | null
  end_reason: string | null
}

export type HistorySummary = {
  buckets: number
  readings: number
  unavailable: number
  actual_wh: number
  full_wh: number
  saved_wh: number
  savings_percent: number | null
  indoor_lux_avg: number | null
  outdoor_lux_avg: number | null
  first_t: number | null
  last_t: number | null
  changes: number
  changes_by_source: Record<string, number>
  connected_seconds: number
  recording: boolean
}

function rangeQuery(start: number | null, end: number | null, extra = '') {
  const params = new URLSearchParams()
  if (start != null) params.set('start', String(start))
  if (end != null) params.set('end', String(end))
  const query = params.toString()
  return query ? `?${query}${extra && `&${extra}`}` : extra ? `?${extra}` : ''
}

export const historyApi = {
  summary: (start: number | null, end: number | null) =>
    request<HistorySummary>(`/api/history/summary${rangeQuery(start, end)}`),
  samples: (start: number | null, end: number | null, maxPoints = 1200) =>
    request<{ step_seconds: number; downsampled: boolean; points: HistoryPoint[] }>(
      `/api/history/samples${rangeQuery(start, end, `max_points=${maxPoints}`)}`),
  changes: (start: number | null, end: number | null, limit = 300) =>
    request<{ changes: LightChange[] }>(
      `/api/history/changes${rangeQuery(start, end, `limit=${limit}`)}`)
      .then((r) => r.changes),
  sessions: (start: number | null, end: number | null) =>
    request<{ sessions: DeviceSessionRow[] }>(
      `/api/history/sessions${rangeQuery(start, end)}`).then((r) => r.sessions),
  exportUrl: (table: 'samples' | 'light_changes', start: number | null,
    end: number | null) => `/api/history/export/${table}.csv${rangeQuery(start, end)}`,
}

/* --- REST ---------------------------------------------------------------- */

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `${response.status} ${response.statusText}`)
  }
  return response.json()
}

export const api = {
  modes: () => request<{ modes: Mode[] }>('/api/modes').then((r) => r.modes),
  createMode: (name: string, target_lux: number) =>
    request<Mode>('/api/modes', {
      method: 'POST',
      body: JSON.stringify({ name, target_lux }),
    }),
  updateMode: (id: string, patch: Partial<Mode>) =>
    request<Mode>(`/api/modes/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deleteMode: (id: string) =>
    request<{ deleted: string }>(`/api/modes/${id}`, { method: 'DELETE' }),

  settings: () => request<Settings>('/api/settings'),
  saveSettings: (patch: Partial<Settings>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  override: (payload: { auto?: boolean; mode_id?: string; brightness?: number }) =>
    request<unknown>('/api/override', { method: 'POST', body: JSON.stringify(payload) }),

  simulateVoice: (transcript: string) =>
    request<VoiceResult>('/api/simulate/voice', {
      method: 'POST',
      body: JSON.stringify({ transcript }),
    }),

  /** Ramp the panel up and down, to confirm it physically responds. */
  sweepPanel: () =>
    request<{ ok: boolean; note: string }>('/api/light/sweep', { method: 'POST' }),

  /** Ask the ESP32 to record from its own mic. Returns null source if none attached. */
  startDeviceRecording: (duration_ms: number) =>
    request<{ source: 'esp32' | null; duration_ms: number; device_connected: boolean }>(
      '/api/voice/record',
      { method: 'POST', body: JSON.stringify({ duration_ms }) },
    ),

  /** Send browser-captured audio through the same pipeline. */
  uploadAudio: async (wav: Blob) => {
    const response = await fetch('/api/voice/audio', {
      method: 'POST',
      headers: { 'content-type': 'audio/wav' },
      body: wav,
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<VoiceResult & {
      audio_b64: string | null
      source: string
    }>
  },
}

/* --- LIVE SOCKET --------------------------------------------------------- */

const MAX_POINTS = 600

/**
 * Subscribes to the server's live feed, reconnecting on drop.
 *
 * Chart points accumulate client-side from the per-second state messages rather than
 * refetching the whole series each tick — the server already pushes exactly what
 * changed, and re-rendering 600 points from scratch every second is wasteful.
 */
export function useLiveData() {
  const [state, setState] = useState<SystemState | null>(null)
  const [points, setPoints] = useState<Point[]>([])
  const [connected, setConnected] = useState(false)
  const [lastVoice, setLastVoice] = useState<VoiceResult | null>(null)
  const socketRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    let closed = false
    let retry: ReturnType<typeof setTimeout>

    const connect = () => {
      if (closed) return
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      const socket = new WebSocket(`${scheme}://${location.host}/ws/dashboard`)
      socketRef.current = socket

      socket.onopen = () => setConnected(true)

      socket.onmessage = (event) => {
        const message = JSON.parse(event.data)

        if (message.type === 'state') {
          setState(message as SystemState)
          // Only chart a sample when a device is actually attached. The server keeps
          // its last reading after a disconnect, and plotting that would draw a flat
          // line made of a measurement that is no longer being taken.
          if (message.readings_live) {
            setPoints((previous) => {
              const point: Point = {
                t: Date.now() / 1000,
                indoor_lux: message.indoor_lux,
                outdoor_lux: message.outdoor_lux,
                brightness: message.brightness,
                actual_watts: message.brightness * message.panel_rated_watts,
                full_watts: message.panel_rated_watts,
                mode: message.mode,
                mode_name: message.active_mode_name,
              }
              const next = [...previous, point]
              return next.length > MAX_POINTS ? next.slice(-MAX_POINTS) : next
            })
          }
        } else if (message.type === 'series') {
          setPoints(message.points.slice(-MAX_POINTS))
        } else if (message.type === 'voice_result') {
          setLastVoice(message as VoiceResult)
        }
      }

      const reconnect = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(connect, 1500)
      }
      socket.onclose = reconnect
      socket.onerror = () => socket.close()
    }

    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      socketRef.current?.close()
    }
  }, [])

  return { state, points, connected, lastVoice }
}
