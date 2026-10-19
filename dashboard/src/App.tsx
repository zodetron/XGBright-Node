/*
 * Application shell.
 *
 * Three views behind one floating glass bar: Live (what the room is doing now),
 * History (what the SQLite log recorded) and Setup (modes and tuning — edited rarely,
 * so kept off the screen people watch). The tab lives in the URL hash so a reload or a
 * shared link lands on the same view.
 *
 * The aurora behind everything is driven by the panel's real output via `--light`;
 * with no device reporting it falls back to a dim ground rather than a guess.
 */

import { useCallback, useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { api, useLiveData } from './api'
import type { Mode } from './api'
import { Controls } from './components/Controls'
import { EnergyChart } from './components/EnergyChart'
import { Hero } from './components/Hero'
import { HistoryView } from './components/HistoryView'
import { ModesManager } from './components/ModesManager'
import { Readouts } from './components/Readouts'
import { SettingsPanel } from './components/SettingsPanel'
import { TodayPanel } from './components/TodayPanel'
import { VoicePanel } from './components/VoicePanel'

type Tab = 'live' | 'history' | 'setup'
const TABS: { key: Tab; label: string }[] = [
  { key: 'live', label: 'Live' },
  { key: 'history', label: 'History' },
  { key: 'setup', label: 'Setup' },
]

function readTab(): Tab {
  const hash = location.hash.slice(1)
  return hash === 'history' || hash === 'setup' ? hash : 'live'
}

function Logo() {
  return (
    <svg width="30" height="30" viewBox="0 0 30 30" aria-hidden>
      <defs>
        <radialGradient id="logo-core" cx="40%" cy="35%" r="70%">
          <stop offset="0%" stopColor="#fff4dc" />
          <stop offset="55%" stopColor="#ffb84d" />
          <stop offset="100%" stopColor="#ff7a2e" />
        </radialGradient>
      </defs>
      <circle cx="15" cy="15" r="13.5" fill="none" stroke="rgba(255,255,255,0.18)" />
      <path d="M15 1.5 A13.5 13.5 0 0 1 28.5 15" fill="none" stroke="#3fe0c5" strokeWidth="2" strokeLinecap="round" />
      <circle cx="15" cy="15" r="6.5" fill="url(#logo-core)" />
    </svg>
  )
}

function TopBar({
  tab, onTab, serverLive, deviceLive, simulated,
}: {
  tab: Tab
  onTab: (tab: Tab) => void
  serverLive: boolean
  deviceLive: boolean
  simulated: boolean
}) {
  return (
    <header
      className="glass"
      style={{
        position: 'sticky', top: 14, zIndex: 20, padding: '10px 12px 10px 18px',
        borderRadius: 999, marginBottom: 22,
      }}
    >
      <div className="between" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div className="row" style={{ gap: 11 }}>
          <Logo />
          <div style={{ fontFamily: 'var(--display)', fontWeight: 800, fontSize: 19, letterSpacing: '-0.03em' }}>
            XGBright
          </div>
          <span className="hide-sm" style={{ fontSize: 12, color: 'var(--text-faint)', marginLeft: 2 }}>
            adaptive lighting
          </span>
        </div>

        <nav className="segmented" aria-label="Views">
          {TABS.map(({ key, label }) => (
            <button key={key} className={tab === key ? 'on' : ''} onClick={() => onTab(key)}
                    aria-current={tab === key ? 'page' : undefined}>
              {label}
            </button>
          ))}
        </nav>

        <div className="row" style={{ gap: 8 }}>
          <span className="chip hide-sm"><span className={`dot ${serverLive ? 'live' : ''}`} />{serverLive ? 'Server' : 'Reconnecting'}</span>
          {simulated ? (
            <span className="chip" style={{ color: '#1a0508', background: 'var(--rose)', borderColor: 'var(--rose)' }}>
              SIMULATED DEVICE
            </span>
          ) : (
            <span className="chip">
              <span className={`dot ${deviceLive ? 'live' : ''}`} />
              {deviceLive ? 'ESP32 connected' : 'ESP32 offline'}
            </span>
          )}
        </div>
      </div>
    </header>
  )
}

export default function App() {
  const { state, points, connected, lastVoice } = useLiveData()
  const [modes, setModes] = useState<Mode[]>([])
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>(readTab)

  const loadModes = useCallback(() => {
    api.modes().then(setModes).catch((cause) => setError(String(cause)))
  }, [])

  useEffect(loadModes, [loadModes])

  useEffect(() => {
    const onHash = () => setTab(readTab())
    addEventListener('hashchange', onHash)
    return () => removeEventListener('hashchange', onHash)
  }, [])

  // Errors are transient notices, not state to be dismissed by hand.
  useEffect(() => {
    if (!error) return
    const timer = setTimeout(() => setError(null), 5000)
    return () => clearTimeout(timer)
  }, [error])

  const changeTab = (next: Tab) => {
    history.replaceState(null, '', next === 'live' ? location.pathname : `#${next}`)
    setTab(next)
    scrollTo({ top: 0, behavior: 'smooth' })
  }

  const live = !!state?.device_connected && !!state?.readings_live
  const light = live && state ? Math.max(0, Math.min(1, state.brightness)) : 0

  return (
    <div style={{ '--light': light } as CSSProperties}>
      <div className="aurora" aria-hidden>
        <span className="bloom-light" />
        <span className="bloom-teal" />
        <span className="bloom-violet" />
      </div>

      <div className="page">
        <TopBar
          tab={tab}
          onTab={changeTab}
          serverLive={connected}
          deviceLive={!!state?.device_connected}
          simulated={!!state?.device_simulated}
        />

        {tab === 'live' && state?.device_simulated && (
          <div
            className="glass"
            role="status"
            style={{ marginBottom: 'var(--gap)', padding: '14px 20px', borderColor: 'rgba(255,122,144,0.5)', color: 'var(--rose)', fontSize: 13.5 }}
          >
            <strong>Simulated data.</strong> A mock device ({state.device_id}) is connected — every reading
            below is generated, not measured. None of it is written to history.
          </div>
        )}

        {tab === 'live' && (
          <div className="stack" style={{ gap: 'var(--gap)' }}>
            <div className="grid-hero">
              <Hero state={state} />
              <Controls state={state} modes={modes} onError={setError} />
            </div>
            <Readouts state={state} points={points} />
            <div className="grid-main">
              <EnergyChart points={points} state={state} />
              <VoicePanel lastVoice={lastVoice} state={state} onError={setError} />
            </div>
            <TodayPanel onOpenHistory={() => changeTab('history')} />
          </div>
        )}

        {tab === 'history' && <HistoryView onError={setError} />}

        {tab === 'setup' && (
          <div className="grid-half">
            <ModesManager modes={modes} onChange={loadModes} onError={setError} />
            <SettingsPanel onError={setError} />
          </div>
        )}

        <footer className="between" style={{ marginTop: 40, fontSize: 12, color: 'var(--text-faint)', flexWrap: 'wrap' }}>
          <span>
            {state?.model.loaded
              ? `${state.model.model} · MAE ${state.model.metrics.mae?.toFixed(3)}`
              : 'model not loaded — AUTO is using the fallback heuristic'}
          </span>
          <span>Live view: 1 Hz in memory · History: 5 s buckets in SQLite, real device only</span>
        </footer>
      </div>

      {error && (
        <div
          className="glass"
          role="alert"
          style={{
            position: 'fixed', left: '50%', bottom: 24, transform: 'translateX(-50%)', zIndex: 50,
            padding: '12px 20px', borderRadius: 999, color: 'var(--rose)', fontSize: 13,
            borderColor: 'rgba(255,122,144,0.4)', maxWidth: 'calc(100vw - 32px)',
          }}
        >
          {error}
        </div>
      )}
    </div>
  )
}
