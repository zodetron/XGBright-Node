/*
 * The voice channel: push-to-talk, and what the system made of what it heard.
 *
 * Push-to-talk always uses the INMP441 on the ESP32 — that is the microphone this
 * system is built around, and the only one whose audio represents the real signal
 * path. With no device attached the button is disabled and says why; it never quietly
 * substitutes the laptop's microphone, because a transcript from the wrong microphone
 * looks identical to a real one on screen.
 *
 * The laptop microphone is available only behind an explicit, separately labelled
 * control, and only while no device is attached. It exists to exercise Whisper and the
 * parser before the hardware is on the network, and every result it produces is
 * marked as coming from it.
 *
 * The text box remains, because it is the only way to tell a Whisper mistake from a
 * parser mistake — same command, one path with speech recognition and one without.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { SystemState, VoiceResult } from '../api'
import { playPcmBase64, recordWav } from '../recorder'
import { SectionHead } from './ui'

const RECORD_MS = 5000
// A short lockout after each capture. Without it a double-click fires a second
// recording while the first is still being transcribed, and the two results race.
const COOLDOWN_MS = 1500

const ACTION_COLOURS: Record<string, string> = {
  up: 'var(--amber)', down: 'var(--violet)', set: 'var(--amber)',
  mode: 'var(--teal)', auto: 'var(--teal)', unknown: 'var(--rose)',
}

type Phase = 'idle' | 'recording' | 'thinking' | 'cooldown'

export function VoicePanel({
  lastVoice, state, onError,
}: {
  lastVoice: VoiceResult | null
  state: SystemState | null
  onError: (message: string) => void
}) {
  const [text, setText] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [level, setLevel] = useState(0)
  const [remaining, setRemaining] = useState(0)
  const [micSource, setMicSource] = useState<'esp32' | 'browser' | null>(null)
  const [local, setLocal] = useState<VoiceResult | null>(null)
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])

  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  const result = local ?? lastVoice
  const busy = phase !== 'idle'
  const deviceAttached = !!state?.device_connected

  const cooldown = useCallback(() => {
    setPhase('cooldown')
    timers.current.push(setTimeout(() => setPhase('idle'), COOLDOWN_MS))
  }, [])

  const countdown = useCallback(() => {
    setRemaining(Math.ceil(RECORD_MS / 1000))
    for (let i = 1; i <= RECORD_MS / 1000; i++) {
      timers.current.push(setTimeout(
        () => setRemaining(Math.ceil(RECORD_MS / 1000) - i), i * 1000))
    }
  }, [])

  /** Record on the ESP32's own microphone. The only thing the main button does. */
  const pushToTalk = async () => {
    if (busy || !deviceAttached) return
    setLocal(null)
    try {
      const started = await api.startDeviceRecording(RECORD_MS)
      if (started.source !== 'esp32') {
        // The device dropped off between the state update and the click. Say so
        // rather than recording something else instead.
        onError('ESP32 is not connected — nothing was recorded.')
        setPhase('idle')
        return
      }
      setMicSource('esp32')
      setPhase('recording')
      countdown()
      // The device streams the clip up itself; the result arrives over the dashboard
      // WebSocket as a voice_result, so there is nothing to await here.
      timers.current.push(setTimeout(() => {
        setPhase('thinking')
        timers.current.push(setTimeout(cooldown, 2500))
      }, RECORD_MS))
    } catch (error: any) {
      onError(String(error?.message ?? error))
      setPhase('idle')
    }
  }

  /**
   * Record on this laptop instead. Only reachable while no device is attached, and
   * every result it produces is labelled as coming from the laptop.
   */
  const recordOnLaptop = async () => {
    if (busy) return
    setLocal(null)
    try {
      setMicSource('browser')
      setPhase('recording')
      countdown()
      const wav = await recordWav(RECORD_MS, setLevel)

      setPhase('thinking')
      const spoken = await api.uploadAudio(wav)
      setLocal(spoken)
      if (spoken.audio_b64) {
        // No ESP32 speaker to play the confirmation, so play it here instead.
        playPcmBase64(spoken.audio_b64).catch(() => {})
      }
      cooldown()
    } catch (error: any) {
      const message = String(error?.message ?? error)
      onError(message.includes('Permission') || message.includes('denied')
        ? 'Microphone permission denied — allow it in the browser.'
        : message)
      setPhase('idle')
      setLevel(0)
    }
  }

  const send = () => {
    if (!text.trim()) return
    setPhase('thinking')
    api.simulateVoice(text.trim())
      .then(setLocal)
      .catch((error) => onError(String(error.message ?? error)))
      .finally(() => { setPhase('idle'); setText('') })
  }

  const buttonLabel = {
    idle: deviceAttached ? 'Tap to speak · 5 s' : 'ESP32 not connected',
    recording: `Listening… ${remaining}s`,
    thinking: 'Transcribing…',
    cooldown: 'Ready shortly',
  }[phase]

  const recording = phase === 'recording'
  const ring = recording ? Math.max(0.15, level) : 0

  return (
    <section className="glass rise">
      <SectionHead
        eyebrow="Voice"
        title={<>Just <span className="accent-serif">say it</span></>}
        right={
          <span className="chip" style={{ color: deviceAttached ? 'var(--teal)' : 'var(--text-faint)' }}>
            {deviceAttached ? 'INMP441 on device' : 'device offline'}
          </span>
        }
      />

      {/* --- push to talk --- */}
      <div className="row" style={{ gap: 18, marginBottom: 18 }}>
        <button
          onClick={pushToTalk}
          disabled={busy || !deviceAttached}
          aria-label={buttonLabel}
          title={deviceAttached
            ? 'Records 5 s on the ESP32 microphone'
            : 'Connect the ESP32 to record on its microphone'}
          style={{
            width: 84, height: 84, borderRadius: '50%', padding: 0, flex: 'none',
            display: 'grid', placeItems: 'center',
            border: 'none',
            background: recording
              ? 'radial-gradient(circle at 35% 30%, #ffc2cc, #ff5f7c 60%, #c9304f)'
              : 'radial-gradient(circle at 35% 30%, #fff1d6, #ffb84d 58%, #e8871c)',
            boxShadow: recording
              ? `0 0 0 ${6 + ring * 18}px rgba(255,122,144,0.18), 0 10px 30px -8px rgba(255,95,124,0.8)`
              : 'inset 0 2px 0 rgba(255,255,255,0.6), 0 12px 30px -10px rgba(255,170,60,0.85)',
            transition: 'box-shadow 90ms linear, background 0.2s',
          }}
        >
          {recording ? (
            <span className="metric" style={{ fontSize: 30, color: '#2a0710' }}>{remaining}</span>
          ) : (
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#2a1806" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
            </svg>
          )}
        </button>

        <div className="grow" style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 15 }}>{buttonLabel}</div>
          <div style={{ fontSize: 12, color: 'var(--text-faint)', marginTop: 3 }}>
            {recording && micSource === 'browser'
              ? 'speak now — laptop microphone'
              : recording
                ? 'recording on the ESP32'
                : 'English · हिंदी · मराठी · Hinglish'}
          </div>
          {/* Live input level, so a muted or wrong mic is obvious immediately. */}
          <div style={{ height: 6, borderRadius: 999, background: 'rgba(0,0,0,0.35)', overflow: 'hidden', marginTop: 10 }}>
            <div style={{
              height: '100%', width: `${level * 100}%`, borderRadius: 999,
              background: 'linear-gradient(90deg, var(--teal), var(--amber))',
              transition: 'width 90ms linear',
            }} />
          </div>
        </div>
      </div>

      {!deviceAttached && (
        <div className="inset between" style={{ marginBottom: 16, padding: '10px 10px 10px 14px', borderStyle: 'dashed' }}>
          <span style={{ fontSize: 12, color: 'var(--text-faint)' }}>
            No device — test on this laptop's mic. Results are marked as such.
          </span>
          <button className="ghost" onClick={recordOnLaptop} disabled={busy}
                  style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            Use laptop mic
          </button>
        </div>
      )}

      {/* --- last result --- */}
      {result ? (
        <div className="inset" style={{ padding: '16px 18px', marginBottom: 16 }}>
          <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
            <span className="chip" style={{
              padding: '3px 10px', fontSize: 10.5, letterSpacing: '0.08em', textTransform: 'uppercase',
              color: ACTION_COLOURS[result.action] ?? 'var(--text-dim)',
              borderColor: ACTION_COLOURS[result.action] ?? 'var(--border-hi)',
            }}>
              {result.action}
            </span>
            <span className="eyebrow">{result.intent.language}</span>
            {micSource && (
              <span className="eyebrow" style={{ color: micSource === 'esp32' ? 'var(--teal)' : 'var(--rose)' }}>
                {micSource === 'esp32' ? 'via device mic' : 'via laptop mic'}
              </span>
            )}
            {!result.applied && (
              <span style={{ fontSize: 11, color: 'var(--rose)' }}>device did not acknowledge</span>
            )}
          </div>
          <div style={{ fontFamily: 'var(--display)', fontSize: 20, fontWeight: 700, letterSpacing: '-0.02em', marginBottom: 8 }}>
            “{result.transcript}”
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>
            {result.confirmation_text}
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 13, color: 'var(--text-faint)', marginBottom: 16 }}>
          Nothing heard yet. Press the mic, or type a command below.
        </div>
      )}

      <div className="row" style={{ gap: 8 }}>
        <input
          type="text"
          placeholder="or type it: thoda tez karo"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && send()}
          style={{ fontFamily: 'var(--font)', borderRadius: 999, paddingLeft: 18 }}
        />
        <button className="primary" onClick={send} disabled={busy || !text.trim()}>Send</button>
      </div>
    </section>
  )
}
