/*
 * Small shared pieces: the section header every panel opens with, and a sparkline.
 */

import type { ReactNode } from 'react'

export function SectionHead({
  eyebrow, title, right,
}: {
  eyebrow: string
  title: ReactNode
  right?: ReactNode
}) {
  return (
    <div className="between" style={{ marginBottom: 18, alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h2 className="title">{title}</h2>
      </div>
      {right && <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>{right}</div>}
    </div>
  )
}

/**
 * A minimal line of recent values. Draws nothing for fewer than two points rather
 * than inventing a flat line.
 */
export function Sparkline({
  values, color, height = 34,
}: {
  values: number[]
  color: string
  height?: number
}) {
  if (values.length < 2) return <div style={{ height }} />
  const width = 120
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = width / (values.length - 1)
  const path = values
    .map((value, index) => {
      const x = index * step
      const y = height - 3 - ((value - min) / span) * (height - 6)
      return `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const id = `spark-${color.replace(/[^a-z]/gi, '')}`
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
         style={{ width: '100%', height, display: 'block' }} aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={`${path} L${width},${height} L0,${height} Z`} fill={`url(#${id})`} />
      <path d={path} fill="none" stroke={color} strokeWidth={1.6}
            vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  )
}
