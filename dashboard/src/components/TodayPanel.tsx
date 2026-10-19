/*
 * Today, from the stored history: what the system saved since midnight, and the most
 * recent light changes with who caused them.
 *
 * Reads the SQLite log, so it survives server restarts — unlike the session figures
 * above it, which reset. Only real-device sessions are in there.
 */

import { useEffect, useState } from 'react'
import { historyApi } from '../api'
import type { HistorySummary, LightChange } from '../api'
import { SectionHead } from './ui'

const REFRESH_MS = 15_000

const SOURCE: Record<string, { label: string; color: string; glyph: string }> = {
  auto: { label: 'AUTO', color: 'var(--teal)', glyph: '◐' },
  hold: { label: 'Hold', color: 'var(--text-dim)', glyph: '◼' },
  voice: { label: 'Voice', color: 'var(--violet)', glyph: '◉' },
  voice_typed: { label: 'Typed', color: 'var(--violet)', glyph: '⌨' },
  dashboard: { label: 'Dashboard', color: 'var(--amber)', glyph: '◆' },
}

function midnight() {
  const date = new Date()
  date.setHours(0, 0, 0, 0)
  return date.getTime() / 1000
}

function ago(seconds: number) {
  const delta = Math.max(0, Date.now() / 1000 - seconds)
  if (delta < 60) return 'just now'
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function hours(seconds: number) {
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

export function TodayPanel({ onOpenHistory }: { onOpenHistory: () => void }) {
  const [summary, setSummary] = useState<HistorySummary | null>(null)
  const [changes, setChanges] = useState<LightChange[]>([])
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      const start = midnight()
      Promise.all([historyApi.summary(start, null), historyApi.changes(start, null, 7)])
        .then(([nextSummary, nextChanges]) => {
          if (cancelled) return
          setSummary(nextSummary)
          setChanges(nextChanges)
          setFailed(false)
        })
        .catch(() => !cancelled && setFailed(true))
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])

  const empty = summary != null && summary.buckets === 0 && summary.changes === 0

  return (
    <section className="glass rise">
      <SectionHead
        eyebrow="Recorded today"
        title={<>Since <span className="accent-serif">midnight</span></>}
        right={<button className="ghost" onClick={onOpenHistory}>Full history →</button>}
      />

      {failed ? (
        <div style={{ color: 'var(--text-faint)', fontSize: 13 }}>History is unavailable — is the server running?</div>
      ) : empty ? (
        <div className="inset" style={{ padding: '22px 20px', color: 'var(--text-dim)', fontSize: 13.5, lineHeight: 1.6 }}>
          Nothing recorded yet today. History is written only while the real ESP32 is connected —
          it will start filling the moment the board says hello.
        </div>
      ) : (
        <div className="grid-half" style={{ gap: 22 }}>
          <div className="stack" style={{ gap: 0 }}>
            {[
              {
                label: 'Energy saved',
                value: summary?.savings_percent == null ? '—' : summary.savings_percent.toFixed(0),
                unit: '%',
                color: 'var(--teal)',
                sub: summary ? `${summary.saved_wh.toFixed(2)} Wh of ${summary.full_wh.toFixed(2)} Wh` : '',
              },
              {
                label: 'Energy used',
                value: summary ? summary.actual_wh.toFixed(summary.actual_wh < 10 ? 2 : 1) : '—',
                unit: 'Wh',
                color: 'var(--amber)',
                sub: 'by the panel, from stored samples',
              },
              {
                label: 'Device online',
                value: summary ? hours(summary.connected_seconds) : '—',
                unit: '',
                color: 'var(--text)',
                sub: summary ? `${summary.readings.toLocaleString()} readings stored` : '',
              },
            ].map((stat, index) => (
              <div
                key={stat.label}
                className="between"
                style={{ padding: '14px 0', borderTop: index ? '1px solid var(--border)' : 'none', alignItems: 'flex-end' }}
              >
                <div>
                  <div className="eyebrow">{stat.label}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 4 }}>{stat.sub}</div>
                </div>
                <div className="metric" style={{ fontSize: 34, color: stat.color }}>
                  {stat.value}
                  {stat.unit && stat.value !== '—' && <span className="unit">{stat.unit}</span>}
                </div>
              </div>
            ))}
          </div>

          <div>
            <div className="between" style={{ marginBottom: 8 }}>
              <div className="eyebrow">Latest light changes</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>{summary?.changes ?? 0} today</div>
            </div>
            {changes.length === 0 ? (
              <div style={{ color: 'var(--text-faint)', fontSize: 13, paddingTop: 8 }}>No changes yet today.</div>
            ) : (
              <div className="stack" style={{ gap: 0 }}>
                {changes.map((change) => {
                  const source = SOURCE[change.source] ?? { label: change.source, color: 'var(--text-dim)', glyph: '•' }
                  return (
                    <div key={change.id} className="row" style={{ gap: 12, padding: '9px 0', borderBottom: '1px solid var(--border)' }}>
                      <span style={{ color: source.color, width: 16, textAlign: 'center', fontSize: 13 }}>{source.glyph}</span>
                      <div className="grow" style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600 }}>
                          <span className="mono" style={{ color: 'var(--text-faint)', fontWeight: 400 }}>
                            {change.from_brightness == null ? '' : `${Math.round(change.from_brightness * 100)}% → `}
                          </span>
                          <span className="mono" style={{ color: 'var(--amber)' }}>{Math.round(change.to_brightness * 100)}%</span>
                          <span style={{ color: source.color, marginLeft: 8, fontWeight: 600 }}>{source.label}</span>
                        </div>
                        <div style={{ fontSize: 11.5, color: 'var(--text-faint)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {change.transcript ? `“${change.transcript}”` : change.reason}
                        </div>
                      </div>
                      <span style={{ fontSize: 11, color: 'var(--text-faint)', whiteSpace: 'nowrap' }}>{ago(change.t)}</span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
