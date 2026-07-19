// Live job progress over SSE with a polling fallback. Ports
// static/js/job-panel.js's transport half: an EventSource on
// /api/jobs/{id}/stream?after=<seq>; after 2 consecutive SSE errors it falls
// back to polling /api/jobs/{id}/events?after=<seq> every 1.5s. A single ingest
// path handles both transports and a seq cursor resumes without gaps. Terminal
// state arrives as a `final` event (the process exit code is authoritative).
//
// The old `synopticon:job-done` CustomEvent is replaced by an `onDone` callback:
// it fires once for the advisory `result` event (detail carries `stats`) and
// again on terminal `final` (detail carries `state`) — matching the two dispatch
// sites in job-panel.js so ApplyView can still distinguish them.
//
// On repeated SSE errors we re-check /api/auth/me before polling: an expired
// session otherwise just looks like a dead stream, so we route to login instead
// (plan risk #4).
import { ref } from 'vue'
import type { Ref } from 'vue'
import { getJSON } from '../api/client'
import type { JobEvent, JobState } from '../api/types'
import { fetchMe } from '../stores/auth'
import router from '../router'

const LOG_MAX = 500
const TERMINAL = new Set<JobState>(['succeeded', 'failed', 'cancelled', 'interrupted'])

// '' is a plain (neither active nor done) chip — the state a running phase lands
// in when the job ends in a non-success terminal state.
export type PhaseStatus = 'active' | 'done' | ''

export interface PhaseChip {
  name: string
  status: PhaseStatus
}

export interface JobProgress {
  done: number | null
  total: number | null
  pct: number
  indeterminate: boolean
}

export interface LogLine {
  seq: number
  level: string
  message: string
}

/** The detail passed to `onDone`: `result` events carry `stats`; the terminal
 *  `final` synthesises `{ state }`. Mirrors the old job-done CustomEvent detail. */
export type DoneDetail = JobEvent | { state: JobState }

export interface UseJobStreamOptions {
  onDone?: (detail: DoneDetail) => void
}

export interface JobStream {
  jobId: Ref<string | null>
  jobState: Ref<JobState | null>
  finished: Ref<boolean>
  phases: Ref<PhaseChip[]>
  progress: Ref<JobProgress | null>
  log: Ref<LogLine[]>
  result: Ref<Record<string, unknown> | null>
  error: Ref<string | null>
  attach: (jobId: string) => void
  stop: () => void
}

export function useJobStream(options: UseJobStreamOptions = {}): JobStream {
  const jobId = ref<string | null>(null)
  const jobState = ref<JobState | null>(null)
  const finished = ref(false)
  const phases = ref<PhaseChip[]>([])
  const progress = ref<JobProgress | null>(null)
  const log = ref<LogLine[]>([])
  const result = ref<Record<string, unknown> | null>(null)
  const error = ref<string | null>(null)

  // Non-reactive transport state.
  let seq = 0
  let sseErrors = 0
  let es: EventSource | null = null
  let pollTimer: number | null = null
  let logSeq = 0

  function closeTransports(): void {
    if (es) {
      es.close()
      es = null
    }
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  function reset(): void {
    seq = 0
    sseErrors = 0
    logSeq = 0
    finished.value = false
    jobState.value = null
    phases.value = []
    progress.value = null
    log.value = []
    result.value = null
    error.value = null
    closeTransports()
  }

  function setPhase(phase: string | undefined): void {
    if (!phase) return
    const existing = phases.value.find((p) => p.name === phase)
    // Any previously-active phase is now considered complete.
    for (const p of phases.value) {
      if (p.name !== phase && p.status === 'active') p.status = 'done'
    }
    if (existing) existing.status = 'active'
    else phases.value.push({ name: phase, status: 'active' })
  }

  function setProgress(done: number | undefined, total: number | undefined): void {
    if (total == null) {
      progress.value = { done: done ?? null, total: null, pct: 0, indeterminate: true }
    } else {
      const pct = total ? Math.round(((done ?? 0) / total) * 100) : 0
      progress.value = { done: done ?? 0, total, pct, indeterminate: false }
    }
  }

  function appendLog(level: string | undefined, message: string | undefined): void {
    log.value.push({ seq: logSeq++, level: level || 'info', message: message ?? '' })
    if (log.value.length > LOG_MAX) log.value.splice(0, log.value.length - LOG_MAX)
  }

  function ingest(evt: JobEvent): void {
    if (typeof evt.seq === 'number' && evt.seq > seq) seq = evt.seq
    switch (evt.event) {
      case 'phase':
        setPhase(evt.phase)
        break
      case 'progress':
        setPhase(evt.phase)
        setProgress(evt.done, evt.total)
        break
      case 'log':
        appendLog(evt.level, evt.message)
        break
      case 'result': {
        const stats = (evt.stats as Record<string, unknown>) || {}
        appendLog('info', 'result: ' + JSON.stringify(stats))
        result.value = stats
        options.onDone?.(evt)
        break
      }
      case 'error':
        appendLog('error', evt.message || 'error')
        error.value = evt.message || 'error'
        break
      case 'final':
        finish(evt.state)
        break
    }
  }

  function finish(state: JobState | undefined): void {
    if (finished.value) return
    finished.value = true
    closeTransports()
    if (progress.value) progress.value = { ...progress.value, indeterminate: false }
    // Match job-panel.js: clear `active` from every phase; on success mark them
    // all done, otherwise a still-active phase drops to a plain chip.
    for (const p of phases.value) {
      if (state === 'succeeded') p.status = 'done'
      else if (p.status === 'active') p.status = ''
    }
    jobState.value = state ?? null
    options.onDone?.({ state: (state ?? 'failed') as JobState })
  }

  function connectSSE(): void {
    if (finished.value || jobId.value === null) return
    try {
      es = new EventSource('/api/jobs/' + jobId.value + '/stream?after=' + seq)
    } catch {
      startPolling()
      return
    }
    es.onmessage = (ev) => {
      try {
        ingest(JSON.parse(ev.data) as JobEvent)
      } catch {
        /* comment ping line */
      }
    }
    es.onerror = () => {
      if (es) {
        es.close()
        es = null
      }
      if (finished.value) return
      sseErrors += 1
      if (sseErrors >= 2) void authCheckThenPoll()
      else window.setTimeout(connectSSE, 500)
    }
  }

  // Before falling back to polling, confirm the session is still valid — a 401
  // on the EventSource surfaces only as an opaque error, so distinguish an
  // expired session (→ login) from a merely buffering proxy (→ poll).
  async function authCheckThenPoll(): Promise<void> {
    try {
      const me = await fetchMe(true)
      if (!me.authenticated) {
        const current = router.currentRoute.value
        void router.push({ name: 'login', query: { next: current.fullPath } })
        return
      }
    } catch {
      /* /api/auth/me unreachable — treat as transient and keep trying to poll */
    }
    startPolling()
  }

  function startPolling(): void {
    if (finished.value) return
    const tick = (): void => {
      if (finished.value) return
      getJSON<{ events: JobEvent[]; state: JobState; seq?: number }>(
        '/api/jobs/' + jobId.value + '/events?after=' + seq,
      )
        .then((res) => {
          for (const e of res.events || []) ingest(e)
          if (!finished.value && TERMINAL.has(res.state)) finish(res.state)
          if (!finished.value) pollTimer = window.setTimeout(tick, 1500)
        })
        .catch(() => {
          if (!finished.value) pollTimer = window.setTimeout(tick, 1500)
        })
    }
    tick()
  }

  function attach(id: string): void {
    reset()
    jobId.value = id
    connectSSE()
  }

  function stop(): void {
    closeTransports()
  }

  return {
    jobId,
    jobState,
    finished,
    phases,
    progress,
    log,
    result,
    error,
    attach,
    stop,
  }
}
