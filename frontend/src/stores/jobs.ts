// The single source of `/api/jobs` truth (no Pinia). Every consumer — the topbar
// chip, the dashboard, the pipeline history table — subscribes here instead of
// running its own timer.
//
// Three properties matter for keeping the GUI responsive, all of which the
// previous per-view timers lacked:
//
//  * One poller, refcounted. Views used to poll /api/jobs independently (topbar
//    5 s + dashboard 10 s + pipeline 4 s), so a single page issued three
//    overlapping request streams for the same data.
//  * An in-flight guard. A plain `setInterval` keeps firing while earlier
//    requests hang, so a slow backend accumulates unbounded concurrent requests
//    and turns a latency blip into a pile-up it cannot recover from.
//  * Backoff + visibility gating. Errors slow the poll down instead of hammering
//    a struggling server, and a hidden tab stops polling entirely (two open tabs
//    otherwise double the load forever).
import { reactive } from 'vue'
import { getJSON } from '../api/client'
import type { Job } from '../api/types'

const RUNNING = new Set<Job['state']>(['queued', 'running'])
// Poll faster while something is running; idle history barely changes.
const POLL_ACTIVE_MS = 5000
const POLL_IDLE_MS = 15000
const BACKOFF_MAX_MS = 60000

const state = reactive<{ running: Job | null; history: Job[]; loaded: boolean }>({
  running: null,
  history: [],
  loaded: false,
})

let timer: number | null = null
let subscribers = 0
let inFlight: Promise<void> | null = null
let failures = 0

function nextDelay(): number {
  if (failures > 0) {
    return Math.min(POLL_ACTIVE_MS * 2 ** (failures - 1), BACKOFF_MAX_MS)
  }
  return state.running ? POLL_ACTIVE_MS : POLL_IDLE_MS
}

function schedule(): void {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
  if (subscribers === 0) return
  timer = window.setTimeout(tick, nextDelay())
}

function tick(): void {
  if (document.visibilityState === 'hidden') {
    schedule()
    return
  }
  void poll().finally(schedule)
}

/** Fetch once. Concurrent callers share the in-flight request rather than
 *  issuing a second one. */
export function poll(): Promise<void> {
  if (inFlight) return inFlight
  inFlight = getJSON<{ items: Job[] }>('/api/jobs')
    .then((data) => {
      const items = data.items || []
      state.history = items
      state.running = items.find((j) => RUNNING.has(j.state)) ?? null
      state.loaded = true
      failures = 0
    })
    .catch(() => {
      // Transient; a 401 is handled inside the client. Back off so a struggling
      // backend is not hammered.
      failures += 1
    })
    .finally(() => {
      inFlight = null
    })
  return inFlight
}

function onVisibility(): void {
  // Coming back to a backgrounded tab should refresh immediately.
  if (document.visibilityState === 'visible' && subscribers > 0) {
    failures = 0
    void poll().finally(schedule)
  }
}

export function startJobPolling(): void {
  subscribers += 1
  if (subscribers === 1) {
    document.addEventListener('visibilitychange', onVisibility)
    void poll().finally(schedule)
  }
}

export function stopJobPolling(): void {
  subscribers = Math.max(0, subscribers - 1)
  if (subscribers === 0) {
    document.removeEventListener('visibilitychange', onVisibility)
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }
}

/** Force an immediate refresh (after submitting or cancelling a job) and
 *  re-arm the timer at the new cadence. */
export function refreshJobs(): Promise<void> {
  failures = 0
  return poll().finally(schedule)
}

export function useJobs() {
  return { state, startJobPolling, stopJobPolling, refreshJobs }
}
