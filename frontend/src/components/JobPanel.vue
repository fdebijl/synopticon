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
import { useJobStream, type DoneDetail } from '../composables/useJobStream'

const props = defineProps<{ jobId?: string }>()
const emit = defineEmits<{ done: [detail: DoneDetail] }>()

const stream = useJobStream({ onDone: (detail) => emit('done', detail) })
const { jobId, jobState, finished, phases, progress, log } = stream

const logEl = ref<HTMLPreElement | null>(null)
let logPaused = false

function fmtJobId(id: string): string {
  return /^\d+$/.test(id) ? '#' + id : id
}

const title = computed(() => (jobId.value ? fmtJobId(jobId.value) : 'Idle'))
const showCancel = computed(() => jobId.value !== null && !finished.value)

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
      <span v-if="finished && jobState" class="job-state badge" :class="`state-${jobState}`">{{
        jobState
      }}</span>
      <div class="job-panel-spacer"></div>
      <button
        v-if="showCancel"
        type="button"
        class="btn btn-sm btn-danger"
        @click="cancel"
      >
        Cancel
      </button>
    </div>
    <div class="phase-chips" aria-label="Phases">
      <span
        v-for="p in phases"
        :key="p.name"
        class="phase-chip"
        :class="p.status"
        >{{ p.name }}</span
      >
    </div>
    <div
      v-if="progress"
      class="progress"
      :class="{ indeterminate: progress.indeterminate }"
      role="progressbar"
      aria-live="polite"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="progress.indeterminate ? undefined : progress.pct"
    >
      <div
        class="progress-bar"
        :style="progress.indeterminate ? undefined : { width: progress.pct + '%' }"
      ></div>
    </div>
    <pre ref="logEl" class="job-log" tabindex="0" aria-label="Job log" @scroll="onLogScroll"><div
        v-for="line in log"
        :key="line.seq"
        :class="`log-${line.level}`"
      >{{ line.message }}</div></pre>
  </section>
</template>
