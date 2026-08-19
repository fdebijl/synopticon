<script setup lang="ts">
// Live job panel: title/state chip, phase chips, progress bar, a 500-line log
// ring with pause-on-scroll-up, and a cancel button. Ports
// templates/partials/job_panel.html.j2 + the rendering half of job-panel.js.
// Transport lives in useJobStream; this component only renders and drives it.
//
// Public API (used by later phases — the setup wizard mounts two of these):
//   props.jobId?         attach to an existing job on mount
//   start(name, params, extra?) => Promise<string>   POST /api/jobs then attach
//   attach(jobId)                                     follow an existing job
//   emit('done', detail)   fires for the advisory `result` event and again on
//                          terminal `final` (detail carries state), matching the
//                          old synopticon:job-done CustomEvent.
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { postJSON } from '../api/client'
import { toast } from '../stores/toasts'
import {
  useJobStream,
  formatDuration,
  formatRate,
  type DoneDetail,
} from '../composables/useJobStream'

const props = defineProps<{ jobId?: string }>()
const emit = defineEmits<{ done: [detail: DoneDetail] }>()

const stream = useJobStream({ onDone: (detail) => emit('done', detail) })
const { jobId, jobState, finished, phases, progress, log, meta, elapsed, error } = stream

const logEl = ref<HTMLPreElement | null>(null)
let logPaused = false

function fmtJobId(id: string): string {
  return /^\d+$/.test(id) ? '#' + id : id
}

const title = computed(() => (jobId.value ? fmtJobId(jobId.value) : 'Idle'))
const showCancel = computed(() => jobId.value !== null && !finished.value)
// Show the state for a running job too — previously only terminal states got a
// chip, so a queued or running job carried no status at all.
const stateLabel = computed(() => jobState.value ?? meta.value?.state ?? null)

/** `sync.faces (personal) · 14,500 / 14,521 (99%) · 12.4/s · ETA 2s` */
const progressText = computed(() => {
  const p = progress.value
  if (!p) return ''
  const parts: string[] = []
  if (p.label) parts.push(p.label)
  if (p.total != null) {
    parts.push(`${p.done?.toLocaleString()} / ${p.total.toLocaleString()} (${p.pct}%)`)
  } else if (p.done != null) {
    parts.push(p.done.toLocaleString())
  }
  if (p.rate) parts.push(formatRate(p.rate))
  if (p.etaSeconds != null && !finished.value) parts.push(`ETA ${formatDuration(p.etaSeconds)}`)
  return parts.join(' · ')
})

const elapsedText = computed(() =>
  elapsed.value === null ? '' : formatDuration(elapsed.value),
)
const logEmpty = computed(() => log.value.length === 0)
// Why it failed, as a headline — a reason buried in a scrolled log is easy to
// miss, and it is the first thing anyone looks for on a red job.
const failure = computed(() => error.value || meta.value?.error || null)

function onLogScroll(): void {
  const el = logEl.value
  if (!el) return
  logPaused = el.scrollHeight - el.scrollTop - el.clientHeight >= 24
}

// Autoscroll to the newest line unless the user scrolled up to read history.
watch(
  () => log.value.length,
  () => {
    if (logPaused) return
    void nextTick(() => {
      const el = logEl.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)

function attach(id: string): void {
  stream.attach(id)
}

async function start(
  name: string,
  params: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
): Promise<string> {
  const res = await postJSON<{ job_id: string }>('/api/jobs', { name, params, ...extra })
  attach(res.job_id)
  return res.job_id
}

async function cancel(): Promise<void> {
  if (!jobId.value || finished.value) return
  try {
    await postJSON('/api/jobs/' + jobId.value + '/cancel')
    toast('Cancelling…')
  } catch (e) {
    toast((e as Error).message, 'error')
  }
}

onMounted(() => {
  if (props.jobId) attach(props.jobId)
})
onBeforeUnmount(() => stream.stop())

defineExpose({ start, attach })
</script>

<template>
  <section class="job-panel" aria-label="Job progress">
    <div class="job-panel-head">
      <span class="job-panel-title">{{ title }}</span>
      <span v-if="stateLabel" class="job-state badge" :class="`state-${stateLabel}`">{{
        stateLabel
      }}</span>
      <span v-if="meta?.name" class="muted mono job-panel-cmd">{{
        (meta.argv || [meta.name]).join(' ')
      }}</span>
      <div class="job-panel-spacer"></div>
      <span v-if="elapsedText" class="muted mono" aria-label="Elapsed">{{ elapsedText }}</span>
      <button
        v-if="showCancel"
        type="button"
        class="btn btn-sm btn-danger"
        @click="cancel"
      >
        Cancel
      </button>
    </div>
    <div v-if="phases.length" class="phase-chips" aria-label="Phases">
      <span
        v-for="p in phases"
        :key="p.name"
        class="phase-chip"
        :class="p.status"
        >{{ p.name }}</span
      >
    </div>
    <p v-if="failure" class="job-error" role="alert">{{ failure }}</p>
    <div v-if="progress">
      <div class="job-progress-meta mono" aria-live="polite">{{ progressText }}</div>
      <div
        class="progress"
        :class="{ indeterminate: progress.indeterminate }"
        role="progressbar"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-valuenow="progress.indeterminate ? undefined : progress.pct"
      >
        <div
          class="progress-bar"
          :style="progress.indeterminate ? undefined : { width: progress.pct + '%' }"
        ></div>
      </div>
    </div>
    <pre ref="logEl" class="job-log" tabindex="0" aria-label="Job log" @scroll="onLogScroll"><div
        v-if="logEmpty"
        class="log-placeholder"
      >{{ finished ? 'no output was recorded for this job' : 'waiting for output…' }}</div><div
        v-for="line in log"
        :key="line.seq"
        :class="[`log-${line.level}`, line.stream ? `log-stream-${line.stream}` : '']"
      >{{ line.message }}</div></pre>
  </section>
</template>

<style scoped>
.job-panel {
  background: var(--bg-raised);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
  box-shadow: var(--shadow-card);
  margin-top: var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.job-panel-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.job-panel-title {
  font-weight: 600;
}
.job-panel-spacer {
  flex: 1;
}
.job-panel-cmd {
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.phase-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.phase-chip {
  font-size: var(--fs-sm);
  padding: 2px var(--sp-2);
  border-radius: 999px;
  background: var(--bg-sunken);
  color: var(--text-2);
  border: 1px solid var(--border-soft);
}
.phase-chip.active {
  background: var(--sel-tint-strong);
  color: var(--action);
  border-color: transparent;
}
.phase-chip.done {
  background: rgba(31, 157, 77, 0.12);
  color: var(--ok);
  border-color: transparent;
}

/* Live counts/rate/ETA above the bar. Tabular figures stop the digits jittering
   as the numbers tick over. */
.job-progress-meta {
  font-size: var(--fs-sm);
  color: var(--text-2);
  margin-bottom: var(--sp-1);
  font-variant-numeric: tabular-nums;
  min-height: 1.2em;
}
.progress {
  height: 8px;
  background: var(--bg-sunken);
  border-radius: 999px;
  overflow: hidden;
}
.progress-bar {
  height: 100%;
  width: 0;
  background: var(--action);
  border-radius: 999px;
  transition: width 0.2s;
}
.progress.indeterminate .progress-bar {
  width: 40%;
  background: linear-gradient(90deg, transparent, var(--action), transparent);
  animation: indeterminate 1.2s infinite;
}
@keyframes indeterminate {
  0% {
    transform: translateX(-120%);
  }
  100% {
    transform: translateX(320%);
  }
}

.job-log {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  background: var(--bg-sunken);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
}
.job-log .log-warning {
  color: var(--warn);
}
.job-log .log-error {
  color: var(--danger);
}
/* Phase transitions are the log's structure — brighter than the lines they group. */
.job-log .log-phase {
  color: var(--action);
  font-weight: 600;
  margin-top: var(--sp-1);
}
/* Console-mirrored lines are secondary to the structured ones. */
.job-log .log-stream-stdout {
  color: var(--text-2);
}
.job-log .log-placeholder {
  color: var(--text-2);
  font-style: italic;
}
.job-error {
  margin: 0;
  background: rgba(216, 67, 67, 0.12);
  color: var(--danger);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-base);
  word-break: break-word;
}

@media (prefers-reduced-motion: reduce) {
  .progress.indeterminate .progress-bar {
    animation: none;
    width: 100%;
    opacity: 0.5;
  }
}
</style>
