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
  'clear-applies': 'Clear queued applies',
  'prune-queue': 'Remove broken review items',
  'delete-crops': 'Delete crop images',
}

export function jobLabel(name: string | null | undefined): string {
  if (!name) return ''
  return JOB_LABELS[name] ?? name
}

// The run ledger records every CLI invocation, including commands the GUI can
// never start, and names `models download` by its group alone.
const COMMAND_LABELS: Record<string, string> = {
  models: JOB_LABELS['models-download'],
  check: 'Connection check',
  hwinfo: 'Hardware info',
  review: 'Review server (legacy)',
  'apply-all': 'Apply everything',
  eval: 'Evaluation',
  'reset-password': 'Reset password',
  'web-access': 'Web access',
  'disable-2fa': 'Turn off two-step sign-in',
  'session-pin': 'Session pinning',
}

/** Label for a raw CLI command name, as the run ledger stores it. */
export function commandLabel(name: string | null | undefined): string {
  if (!name) return ''
  return COMMAND_LABELS[name] ?? jobLabel(name)
}
