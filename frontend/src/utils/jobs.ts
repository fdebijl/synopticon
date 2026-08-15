// Friendly names for the job ids the API speaks. The ids themselves are the
// CLI command names and stay stable — this is the presentation layer only, so
// the UI never shows a homelabber "extract" or "cluster".
const JOB_LABELS: Record<string, string> = {
  sync: 'Sync',
  extract: 'Detect faces',
  cluster: 'Group faces',
  recluster: 'Re-group faces',
  report: 'Report',
  'regen-crops': 'Regenerate crops',
  benchmark: 'Benchmark',
  'models-download': 'Download models',
  apply: 'Apply corrections',
  dedupe: 'Duplicate photos',
  reset: 'Reset',
  'clear-queue': 'Clear review queue',
  'delete-crops': 'Delete crop images',
}

export function jobLabel(name: string | null | undefined): string {
  if (!name) return ''
  return JOB_LABELS[name] ?? name
}
