/** Compact relative time for an epoch-seconds timestamp: `42s ago`, `3h ago`. */
export function ago(ts: unknown): string {
  const t = Number(ts)
  if (!t) return '—'
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t))
  if (s < 60) return s + 's ago'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm ago'
  const h = Math.floor(m / 60)
  if (h < 24) return h + 'h ago'
  return Math.floor(h / 24) + 'd ago'
}
