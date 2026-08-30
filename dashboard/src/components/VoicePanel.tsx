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
    idle: deviceAttached ? 'Record 5s on device' : 'ESP32 not connected',
    recording: `Listening… ${remaining}s`,
    thinking: 'Transcribing…',
    cooldown: 'Ready shortly',
  }[phase]

  return (
    <div className="card">
      <div className="between" style={{ marginBottom: 14 }}>
        <div className="label">Voice</div>
        <div className="label" style={{
          color: deviceAttached ? 'var(--teal)' : 'var(--text-faint)',
        }}>
          {deviceAttached ? 'INMP441 on device' : 'device offline'}
        </div>
      </div>

      {/* --- push to talk --- */}
      <div className="row" style={{ gap: 14, marginBottom: 16 }}>
        <button
          className={phase === 'recording' ? '' : 'primary'}
          onClick={pushToTalk}
          disabled={busy || !deviceAttached}
          title={deviceAttached
            ? 'Records on the ESP32 microphone'
            : 'Connect the ESP32 to record on its microphone'}
          style={{
            minWidth: 168, padding: '12px 18px', fontSize: 14, fontWeight: 600,
            borderColor: phase === 'recording' ? 'var(--rose)' : undefined,
            color: phase === 'recording' ? 'var(--rose)' : undefined,
          }}
        >
          <span className="row" style={{ gap: 9, justifyContent: 'center' }}>
            <span
              style={{
                width: 10, height: 10, borderRadius: '50%',
                background: phase === 'recording' ? 'var(--rose)' : 'currentColor',
                animation: phase === 'recording'
                  ? 'pulse 1s ease-in-out infinite' : 'none',
              }}
            />
            {buttonLabel}
          </span>
        </button>

        {/* Live input level, so a muted or wrong mic is obvious immediately. */}
        <div className="grow">
          <div style={{
            height: 8, borderRadius: 4, background: 'var(--bg-elevated)',
            border: '1px solid var(--border)', overflow: 'hidden',
          }}>
            <div style={{
              height: '100%', width: `${level * 100}%`,
              background: 'linear-gradient(90deg, var(--teal), var(--amber))',
              transition: 'width 90ms linear',
            }} />
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 5 }}>
            {phase === 'recording' && micSource === 'browser'
              ? 'speak now — laptop microphone'
              : phase === 'recording'
                ? 'recording on the ESP32'
                : `${RECORD_MS / 1000}s capture · ${COOLDOWN_MS / 1000}s between takes`}
          </div>
        </div>
      </div>

      {!deviceAttached && (
        <div className="between" style={{
          marginBottom: 14, padding: '9px 12px',
          border: '1px dashed var(--border-hi)', borderRadius: 'var(--radius-sm)',
        }}>
          <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
            No device. You can test the pipeline on this laptop's microphone instead —
            results are marked as such.
          </span>
          <button className="ghost" onClick={recordOnLaptop} disabled={busy}
                  style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            Use laptop mic
          </button>
        </div>
      )}

      {/* --- last result --- */}
      {result ? (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-sm)', padding: '13px 15px', marginBottom: 14,
        }}>
          <div className="row" style={{ gap: 8, marginBottom: 9, flexWrap: 'wrap' }}>
            <span style={{
              fontSize: 10, fontWeight: 700, letterSpacing: '0.07em',
              textTransform: 'uppercase',
              color: ACTION_COLOURS[result.action] ?? 'var(--text-dim)',
              border: `1px solid ${ACTION_COLOURS[result.action] ?? 'var(--border-hi)'}`,
              borderRadius: 4, padding: '2px 7px',
            }}>
              {result.action}
            </span>
            <span className="label" style={{ letterSpacing: '0.06em' }}>
              {result.intent.language}
            </span>
            {micSource && (
              <span className="label" style={{
                letterSpacing: '0.06em',
                color: micSource === 'esp32' ? 'var(--teal)' : 'var(--rose)',
              }}>
                {micSource === 'esp32' ? 'via device mic' : 'via laptop mic'}
              </span>
            )}
            {!result.applied && (
              <span style={{ fontSize: 11, color: 'var(--rose)' }}>
                device did not acknowledge
              </span>
            )}
          </div>
          <div style={{ fontSize: 14, marginBottom: 8 }}>“{result.transcript}”</div>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55 }}>
            {result.confirmation_text}
          </div>
        </div>
      ) : (
        <div style={{
          fontSize: 12.5, color: 'var(--text-faint)', marginBottom: 14, padding: '4px 0',
        }}>
          Nothing heard yet. Press record, or type a command below.
        </div>
      )}

      <div className="row" style={{ gap: 8 }}>
        <input
          type="text"
          placeholder="or type it: thoda tez karo"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && send()}
          style={{ fontFamily: 'var(--font)' }}
        />
        <button onClick={send} disabled={busy || !text.trim()}>Send</button>
      </div>
    </div>
  )
}
