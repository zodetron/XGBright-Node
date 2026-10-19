/*
 * Advanced settings: the numbers that decide how the system interprets vague speech,
 * and how long it stays out of AUTO.
 *
 * The magnitude steps are what "a little" and "a lot" mean in percent. They live here
 * rather than in the parser's source (architectural decision #7) precisely so they can
 * be tuned against a real room without touching code.
 */

import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Settings } from '../api'
import { SectionHead } from './ui'

function Field({
  label, hint, value, onChange, min, max, suffix,
}: {
  label: string
  hint: string
  value: number
  onChange: (value: number) => void
  min?: number
  max?: number
  suffix?: string
}) {
  return (
    <div className="between" style={{ alignItems: 'flex-start', gap: 16 }}>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 500 }}>{label}</div>
        <div style={{ fontSize: 11.5, color: 'var(--text-faint)', marginTop: 2 }}>
          {hint}
        </div>
      </div>
      <div className="row" style={{ gap: 7, width: 128 }}>
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        {suffix && (
          <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>{suffix}</span>
        )}
      </div>
    </div>
  )
}

export function SettingsPanel({ onError }: { onError: (message: string) => void }) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.settings().then(setSettings).catch((error) => onError(String(error)))
  }, [onError])

  if (!settings) {
    return (
      <section className="glass">
        <div className="eyebrow">Settings · loading</div>
      </section>
    )
  }

  const patch = (update: Partial<Settings>) => {
    setSettings({ ...settings, ...update })
    setDirty(true)
  }
  const patchStep = (key: keyof Settings['magnitude_steps'], value: number) =>
    patch({ magnitude_steps: { ...settings.magnitude_steps, [key]: value } })

  const save = () => {
    setSaving(true)
    api.saveSettings(settings)
      .then((saved) => {
        setSettings(saved)
        setDirty(false)
      })
      .catch((error) => onError(String(error.message ?? error)))
      .finally(() => setSaving(false))
  }

  return (
    <section className="glass rise">
      <SectionHead
        eyebrow="Settings"
        title={<>Tune the <span className="accent-serif">behaviour</span></>}
        right={
          <button className="primary" onClick={save} disabled={!dirty || saving}>
            {saving ? 'Saving…' : dirty ? 'Save changes' : 'Saved'}
          </button>
        }
      />

      <div className="stack" style={{ gap: 16 }}>
        <div
          className="eyebrow"
          style={{ color: 'var(--text-dim)', borderBottom: '1px solid var(--border)', paddingBottom: 7 }}
        >
          Voice step sizes
        </div>
        <Field
          label="“A little”"
          hint="slightly, a bit, thoda, जरा"
          value={settings.magnitude_steps.slight}
          onChange={(value) => patchStep('slight', value)}
          min={1} max={100} suffix="%"
        />
        <Field
          label="Default step"
          hint="brighter / dimmer with no magnitude given"
          value={settings.magnitude_steps.normal}
          onChange={(value) => patchStep('normal', value)}
          min={1} max={100} suffix="%"
        />
        <Field
          label="“A lot”"
          hint="much, way, bahut, खूप"
          value={settings.magnitude_steps.large}
          onChange={(value) => patchStep('large', value)}
          min={1} max={100} suffix="%"
        />

        <div
          className="eyebrow"
          style={{ color: 'var(--text-dim)', borderBottom: '1px solid var(--border)', paddingBottom: 7, marginTop: 6 }}
        >
          System
        </div>
        <Field
          label="Manual timeout"
          hint="how long a manual hold lasts before reverting to AUTO"
          value={settings.manual_timeout_seconds}
          onChange={(value) => patch({ manual_timeout_seconds: value })}
          min={10} suffix="sec"
        />
        <Field
          label="Panel rated power"
          hint="drives the energy and savings figures"
          value={settings.panel_rated_watts}
          onChange={(value) => patch({ panel_rated_watts: value })}
          min={1} suffix="W"
        />
        <Field
          label="Panel full output"
          hint="illuminance at the sensor when the panel is at 100%"
          value={settings.panel_full_output_lux}
          onChange={(value) => patch({ panel_full_output_lux: value })}
          min={1} suffix="lx"
        />
        <Field
          label="Chart window"
          hint="how much history the live chart keeps in memory"
          value={settings.rolling_window_seconds}
          onChange={(value) => patch({ rolling_window_seconds: value })}
          min={30} suffix="sec"
        />
        <Field
          label="History interval"
          hint="readings are averaged into one stored row per interval"
          value={settings.history_interval_seconds}
          onChange={(value) => patch({ history_interval_seconds: value })}
          min={1} suffix="sec"
        />
      </div>
    </section>
  )
}
