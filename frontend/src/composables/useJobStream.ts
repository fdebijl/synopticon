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
import type { Job, JobEvent, JobState } from '../api/types'
import { fetchMe } from '../stores/auth'
import router from '../router'

const LOG_MAX = 2000
const TERMINAL = new Set<JobState>(['succeeded', 'failed', 'cancelled', 'interrupted'])

// Throughput is measured over a trailing window rather than the whole phase, so
// the reported rate and ETA follow a run that speeds up or slows down (an
// extract that hits a batch of large originals, a sync that starts paging) instead
// of being anchored to the phase's opening seconds.
const RATE_WINDOW_S = 30

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
  /** Items per second over the trailing window, null until two samples exist. */
  rate: number | null
  /** Seconds remaining at the current rate; null without a total or a rate. */
  etaSeconds: number | null
  /** Phase this progress belongs to, with its space when it has one. */
  label: string
}

export interface LogLine {
  seq: number
  level: string
  message: string
  /** 'stdout' | 'stderr' for console-mirrored lines; undefined for structured ones. */
  stream?: string
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
  /** Job metadata (name/argv/timestamps), fetched once on attach. */
  meta: Ref<Job | null>
  /** Wall-clock seconds the job has been running, or ran for once finished. */
  elapsed: Ref<number | null>
  attach: (jobId: string) => void
  stop: () => void
}

/** Compact duration for the UI: `42s`, `4m 12s`, `1h 07m`. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

/** Rate with a unit that stays readable for slow work: `41.2/s`, `2.4/min`. */
export function formatRate(perSecond: number): string {
  if (perSecond >= 1) return `${perSecond.toFixed(1)}/s`
  return `${(perSecond * 60).toFixed(1)}/min`
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
  const meta = ref<Job | null>(null)
  const elapsed = ref<number | null>(null)

  // Non-reactive transport state.
  let seq = 0
  let sseErrors = 0
  let es: EventSource | null = null
  let pollTimer: number | null = null
  let clockTimer: number | null = null
  let logSeq = 0
  // Trailing (timestamp, done) samples for the current phase's rate estimate.
  let rateSamples: Array<[number, number]> = []
  let activePhaseKey: string | null = null

  function closeTransports(): void {
    if (es) {
      es.close()
      es = null
    }
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer)
      pollTimer = null
    }
    if (clockTimer !== null) {
      window.clearInterval(clockTimer)
      clockTimer = null
    }
  }

  function reset(): void {
    seq = 0
    sseErrors = 0
    logSeq = 0
    rateSamples = []
    activePhaseKey = null
    finished.value = false
    jobState.value = null
    phases.value = []
    progress.value = null
    log.value = []
    result.value = null
    error.value = null
    meta.value = null
    elapsed.value = null
    closeTransports()
  }

  function setPhase(phase: string | undefined, space?: string): void {
    if (!phase) return
    const existing = phases.value.find((p) => p.name === phase)
    // Any previously-active phase is now considered complete.
    for (const p of phases.value) {
      if (p.name !== phase && p.status === 'active') p.status = 'done'
    }
    if (existing) existing.status = 'active'
    else phases.value.push({ name: phase, status: 'active' })
    // A phase keyed by name *and* space: the pipeline reuses one phase name
    // across spaces, and each pass is a fresh unit of work — so it gets its own
    // log heading and its own rate baseline.
    const key = space ? `${phase} (${space})` : phase
    if (key !== activePhaseKey) {
      // Narrate the transition into the log too: the chips only show *that* a
      // phase ran, while the log is what a user scrolls back through afterwards.
      appendLog('phase', `▶ ${key}`)
      activePhaseKey = key
      rateSamples = []
    }
  }

  function setProgress(evt: JobEvent): void {
    const done = evt.done
    const total = evt.total
    const label = evt.space ? `${evt.phase} (${evt.space})` : evt.phase || ''

    // Sample throughput, keeping only the trailing window.
    const now = typeof evt.ts === 'number' ? evt.ts : Date.now() / 1000
    if (typeof done === 'number') {
      rateSamples.push([now, done])
      const cutoff = now - RATE_WINDOW_S
      while (rateSamples.length > 2 && rateSamples[0][0] < cutoff) rateSamples.shift()
    }
    let rate: number | null = null
    if (rateSamples.length >= 2) {
      const [t0, d0] = rateSamples[0]
      const [t1, d1] = rateSamples[rateSamples.length - 1]
      if (t1 > t0 && d1 > d0) rate = (d1 - d0) / (t1 - t0)
    }

    if (total == null) {
      progress.value = {
        done: done ?? null, total: null, pct: 0, indeterminate: true,
        rate, etaSeconds: null, label,
      }
      return
    }
    const pct = total ? Math.round(((done ?? 0) / total) * 100) : 0
    const remaining = total - (done ?? 0)
    progress.value = {
      done: done ?? 0, total, pct, indeterminate: false, rate, label,
      etaSeconds: rate && remaining > 0 ? remaining / rate : null,
    }
  }

  function appendLog(
    level: string | undefined,
    message: string | undefined,
    stream?: string,
  ): void {
    log.value.push({ seq: logSeq++, level: level || 'info', message: message ?? '', stream })
    if (log.value.length > LOG_MAX) log.value.splice(0, log.value.length - LOG_MAX)
  }

  function ingest(evt: JobEvent): void {
    if (typeof evt.seq === 'number' && evt.seq > seq) seq = evt.seq
    switch (evt.event) {
      case 'phase':
        setPhase(evt.phase, evt.space)
        break
      case 'progress':
        setPhase(evt.phase, evt.space)
        setProgress(evt)
        break
      case 'log':
        appendLog(evt.level, evt.message, evt.stream)
        break
      case 'result': {
        const stats = (evt.stats as Record<string, unknown>) || {}
        appendLog('info', 'result: ' + JSON.stringify(stats))
        result.value = stats
        options.onDone?.(evt)
        break
      }
      case 'error': {
        const msg = evt.message || 'error'
        // A failure with no structured `error` event gets one synthesized from
        // the tail of stderr — which the console mirror has usually already
        // shown. Keep the event (it is what `error` binds to) but don't print
        // the same sentence twice.
        const recent = log.value.slice(-5).map((l) => l.message)
        if (!recent.includes(msg)) appendLog('error', msg)
        error.value = msg
        break
      }
      case 'final':
        finish(evt.state, evt.exit_code)
        break
    }
  }

  function finish(state: JobState | undefined, exitCode?: number | null): void {
    if (finished.value) return
    finished.value = true
    closeTransports()
    tickClock() // snap elapsed to the end before the interval is gone
    // A terminal line the user can see, so the log always ends by saying how the
    // run ended rather than just trailing off.
    const took = elapsed.value !== null ? ` after ${formatDuration(elapsed.value)}` : ''
    if (state === 'succeeded') {
      appendLog('info', `✓ completed${took}`)
    } else {
      const code = typeof exitCode === 'number' ? ` (exit code ${exitCode})` : ''
      appendLog(state === 'cancelled' ? 'warning' : 'error', `${state}${code}${took}`)
    }
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
    // Never leave a previous socket behind: assigning over `es` would drop the
    // only reference to a still-open stream, which then holds a server-side
    // generator alive until the job ends.
    if (es) {
      es.close()
      es = null
    }
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

  // Elapsed comes from the job's own started_at/ended_at rather than from when
  // this component mounted, so attaching to a job already in flight (a page
  // reload, or opening /jobs/<id> later) still reports the real runtime.
  function tickClock(): void {
    const m = meta.value
    if (!m?.started_at) return
    const end = m.ended_at ?? Date.now() / 1000
    elapsed.value = Math.max(0, end - m.started_at)
  }

  async function loadMeta(id: string): Promise<void> {
    try {
      const m = await getJSON<Job>('/api/jobs/' + id)
      if (jobId.value !== id) return // a later attach() won the race
      meta.value = m
      tickClock()
      if (clockTimer === null && !m.ended_at) {
        clockTimer = window.setInterval(tickClock, 1000)
      }
    } catch {
      /* unknown job / offline — the stream itself will surface the problem */
    }
  }

  function attach(id: string): void {
    // Idempotent. `start()` attaches on the POST response and a route change to
    // /jobs/<id> mounts a panel that attaches again; without this the second
    // call tore down a working stream and opened a duplicate, so the server saw
    // two SSE generators per job and replayed the whole event ring into the
    // second one.
    if (jobId.value === id && !finished.value) return
    reset()
    jobId.value = id
    void loadMeta(id)
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
    meta,
    elapsed,
    attach,
    stop,
  }
}
