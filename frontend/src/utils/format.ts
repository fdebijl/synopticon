// Display formatters shared by the stats views. Plain functions over plain
// numbers: every one accepts null and renders an em dash for it.

export function num(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

/** 0..1 fraction → "5.2%"; one decimal under 10%, whole numbers above. */
export function pct(f: number | null | undefined): string {
  if (f == null || !Number.isFinite(f)) return '—'
  const p = f * 100
  if (p > 0 && p < 0.1) return '<0.1%'
  if (p > 99.5 && p < 100) return Math.min(p, 99.9).toFixed(1) + '%'
  return (p < 10 ? p.toFixed(1) : Math.round(p).toString()) + '%'
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i]
}

/** Seconds → "3d 4h", "2h 15m", "4m 10s", "12s". Two units at most. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  let s = Math.max(0, Math.round(seconds))
  if (s < 60) return s + 's'
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  s -= m * 60
  if (d) return h ? `${d}d ${h}h` : `${d}d`
  if (h) return m ? `${h}h ${m}m` : `${h}h`
  return s ? `${m}m ${s}s` : `${m}m`
}

export function fmtDate(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleDateString() : '—'
}

export function fmtMonthOf(ts: number | null | undefined): string {
  return ts
    ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
    : '—'
}

/**
 * Taken-time timestamps are Synology's local wall clock stored as if UTC, so
 * they are rendered in UTC to show the date the photo was actually taken.
 */
export function fmtTakenDate(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { timeZone: 'UTC' }) : '—'
}

/** 'YYYY-MM-DD' → a locale date, without a timezone shift. */
export function fmtDay(day: string | null | undefined): string {
  if (!day) return '—'
  const [y, m, d] = day.split('-').map(Number)
  if (!y || !m || !d) return day
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { timeZone: 'UTC' })
}

/** 'YYYY-MM' → "Mar 2026". */
export function fmtMonth(month: string): string {
  const [y, m] = month.split('-').map(Number)
  if (!y || !m) return month
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString(undefined, {
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

/** 1,284 → "1.3K" for axis ticks. */
export function compact(n: number): string {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
}
