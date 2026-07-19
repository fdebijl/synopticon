// Running-job chip state (no Pinia). Lightly polls /api/jobs while at least one
// consumer (the topbar) is mounted; the chip mirrors base.html.j2's
// `running_job`. Full per-job SSE streaming lands in Phase 1 (useJobStream).
import { reactive } from 'vue'
import { getJSON } from '../api/client'
import type { Job } from '../api/types'

const RUNNING = new Set<Job['state']>(['queued', 'running'])
const POLL_MS = 5000

const state = reactive<{ running: Job | null }>({ running: null })
let timer: number | null = null
let subscribers = 0

async function poll(): Promise<void> {
  try {
    const data = await getJSON<{ items: Job[] }>('/api/jobs')
    state.running = data.items.find((j) => RUNNING.has(j.state)) ?? null
  } catch {
    // Transient errors are ignored; a 401 is handled inside the client.
  }
}

export function startJobPolling(): void {
  subscribers += 1
  if (timer === null) {
    void poll()
    timer = window.setInterval(poll, POLL_MS)
  }
}

export function stopJobPolling(): void {
  subscribers = Math.max(0, subscribers - 1)
  if (subscribers === 0 && timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

export function refreshJobs(): Promise<void> {
  return poll()
}

export function useJobs() {
  return { state, startJobPolling, stopJobPolling, refreshJobs }
}
