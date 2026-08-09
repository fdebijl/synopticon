<script setup lang="ts">
// Schedules: saved jobs on a cron timer, fired by the scheduler thread in the
// web process (web/scheduler.py). The form is driven entirely by the catalog the
// server sends with the listing — the param whitelist lives in web/jobs.py, so
// hand-writing the fields here would guarantee drift.
//
// Two things the server owns and this view only surfaces: the typed-phrase tier
// is unschedulable (a 422 with an explanation), and consent booleans are stored
// on the schedule and replayed at fire time, never a stored argv.
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import { getJSON, postJSON, putJSON, deleteJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'
import type {
  Schedule,
  ScheduleJobForm,
  ScheduleFormField,
  SchedulesResponse,
} from '../api/types'

const PRESETS: { label: string; cron: string }[] = [
  { label: 'Every hour', cron: '@hourly' },
  { label: 'Every 6 hours', cron: '0 */6 * * *' },
  { label: 'Nightly at 03:00', cron: '0 3 * * *' },
  { label: 'Weekly, Sunday 04:00', cron: '0 4 * * SUN' },
  { label: 'Monthly, 1st at 05:00', cron: '0 5 1 * *' },
]

const items = ref<Schedule[]>([])
const catalog = ref<ScheduleJobForm[]>([])
const loading = ref(true)
const panel = ref<InstanceType<typeof JobPanel> | null>(null)
const expanded = ref<number | null>(null)
/** Re-rendered once a minute so "in 3 hours" does not go stale on an open tab. */
const clock = ref(Date.now())
let ticker: number | null = null

const editing = ref<Schedule | null>(null)
const creating = ref(false)
const saving = ref(false)
const form = reactive({
  name: '',
  job: '',
  cron: '0 3 * * *',
  timezone: '',
  enabled: true,
  confirm: false,
  params: {} as Record<string, unknown>,
})
const preview = ref<number[]>([])
const cronError = ref('')
let previewTimer: number | null = null

const open = computed(() => creating.value || editing.value !== null)
const selectedForm = computed(() => catalog.value.find((j) => j.job === form.job) ?? null)
const zones = computed<string[]>(() => {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] }
  try {
    return intl.supportedValuesOf ? intl.supportedValuesOf('timeZone') : []
  } catch {
    return []
  }
})

// -- formatting -------------------------------------------------------------
function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

/** "in 3h 20m" / "12m ago". Coarse on purpose — this is a schedule, not a timer. */
function fmtRelative(t: number | null | undefined): string {
  if (!t) return ''
  const delta = t * 1000 - clock.value
  const ahead = delta >= 0
  let s = Math.round(Math.abs(delta) / 1000)
  if (s < 60) return ahead ? 'in under a minute' : 'just now'
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  const m = Math.floor((s - h * 3600) / 60)
  const parts = [d ? `${d}d` : '', h ? `${h}h` : '', d ? '' : m ? `${m}m` : ''].filter(Boolean)
  const text = parts.join(' ')
  return ahead ? `in ${text}` : `${text} ago`
}

// -- loading ----------------------------------------------------------------
async function load(): Promise<void> {
  try {
    const data = await getJSON<SchedulesResponse>('/api/schedules')
    items.value = data.items
    catalog.value = data.jobs
  } catch (e) {
    toast((e as Error).message || 'Could not load schedules', 'error')
  } finally {
    loading.value = false
  }
}

// -- editor -----------------------------------------------------------------
function defaultsFor(job: string): Record<string, unknown> {
  const spec = catalog.value.find((j) => j.job === job)
  const out: Record<string, unknown> = {}
  for (const f of spec?.fields ?? []) {
    if (f.type === 'bool') out[f.key] = f.default === true
    else if (f.type === 'multiselect') out[f.key] = []
    else out[f.key] = f.default ?? ''
  }
  return out
}

function startCreate(): void {
  const first = catalog.value[0]?.job ?? ''
  editing.value = null
  creating.value = true
  Object.assign(form, {
    name: '',
    job: first,
    cron: '0 3 * * *',
    timezone: '',
    enabled: true,
    confirm: false,
    params: defaultsFor(first),
  })
  void refreshPreview()
}

function startEdit(s: Schedule): void {
  creating.value = false
  editing.value = s
  Object.assign(form, {
    name: s.name,
    job: s.job,
    cron: s.cron,
    timezone: s.timezone ?? '',
    enabled: s.enabled,
    confirm: s.confirm,
    params: { ...defaultsFor(s.job), ...s.params },
  })
  void refreshPreview()
}

function closeEditor(): void {
  creating.value = false
  editing.value = null
  preview.value = []
  cronError.value = ''
}

// Changing the job resets to that job's defaults: params are per-job and
// carrying a stale key over would just be rejected by the server's whitelist.
watch(
  () => form.job,
  (job, previous) => {
    if (previous !== undefined && job !== previous) form.params = defaultsFor(job)
  },
)

watch([() => form.cron, () => form.timezone], () => {
  if (previewTimer !== null) window.clearTimeout(previewTimer)
  previewTimer = window.setTimeout(() => void refreshPreview(), 300)
})

async function refreshPreview(): Promise<void> {
  if (!form.cron.trim()) {
    preview.value = []
    cronError.value = ''
    return
  }
  try {
    const res = await postJSON<{ next: number[] }>('/api/schedules/preview', {
      cron: form.cron,
      timezone: form.timezone,
    })
    preview.value = res.next
    cronError.value = ''
  } catch (e) {
    preview.value = []
    cronError.value = e instanceof ApiError ? e.message : 'Could not parse that expression'
  }
}

/** Drop empty optional fields so the server sees "unset", not "" or NaN. */
function payloadParams(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of selectedForm.value?.fields ?? []) {
    const value = form.params[f.key]
    if (f.type === 'bool') {
      if (value) out[f.key] = true
      else if (f.default === true) out[f.key] = false
    } else if (f.type === 'multiselect') {
      if (Array.isArray(value) && value.length) out[f.key] = value
    } else if (value !== '' && value != null) {
      out[f.key] = value
    }
  }
  return out
}

async function save(): Promise<void> {
  saving.value = true
  const body = {
    name: form.name,
    job: form.job,
    cron: form.cron,
    timezone: form.timezone,
    enabled: form.enabled,
    confirm: form.confirm,
    params: payloadParams(),
  }
  try {
    if (editing.value) await putJSON(`/api/schedules/${editing.value.id}`, body)
    else await postJSON('/api/schedules', body)
    toast(editing.value ? 'Schedule updated' : 'Schedule created')
    closeEditor()
    await load()
  } catch (e) {
    toast((e as Error).message || 'Could not save the schedule', 'error')
  } finally {
    saving.value = false
  }
}

// -- row actions ------------------------------------------------------------
async function toggle(s: Schedule): Promise<void> {
  try {
    await postJSON(`/api/schedules/${s.id}/enabled`, { enabled: !s.enabled })
    await load()
  } catch (e) {
    toast((e as Error).message || 'Could not update the schedule', 'error')
  }
}

async function remove(s: Schedule): Promise<void> {
  const ok = await confirm({
    title: 'Delete schedule',
    message: `Remove "${s.name}"? Jobs it already started are unaffected.`,
    okLabel: 'Delete',
  })
  if (!ok) return
  try {
    await deleteJSON(`/api/schedules/${s.id}`)
    if (editing.value?.id === s.id) closeEditor()
    await load()
  } catch (e) {
    toast((e as Error).message || 'Could not delete the schedule', 'error')
  }
}

async function runNow(s: Schedule): Promise<void> {
  try {
    const res = await postJSON<{ job_id: string }>(`/api/schedules/${s.id}/run`)
    panel.value?.attach(res.job_id)
    toast(`Started ${s.job}`)
    await load()
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      toast(`Not started: ${(e.body?.detail as string) ?? 'already in flight'}`, 'error')
    } else {
      toast((e as Error).message || 'Could not start the job', 'error')
    }
  }
}

function paramSummary(s: Schedule): string {
  const parts = Object.entries(s.params)
    .filter(([, v]) => v !== '' && v != null && v !== false)
    .map(([k, v]) => (v === true ? k : `${k}=${Array.isArray(v) ? v.join('+') : String(v)}`))
  return parts.join(' · ')
}

function toggleRuns(id: number): void {
  expanded.value = expanded.value === id ? null : id
}

function fieldId(f: ScheduleFormField): string {
  return `sched-field-${f.key}`
}

onMounted(() => {
  void load()
  ticker = window.setInterval(() => (clock.value = Date.now()), 60000)
})
onUnmounted(() => {
  if (ticker !== null) window.clearInterval(ticker)
  if (previewTimer !== null) window.clearTimeout(previewTimer)
})
</script>

<template>
  <div class="page sched-page">
    <JobPanel ref="panel" />

    <section class="card sched-intro">
      <div>
        <h3>Schedules</h3>
        <p class="muted">
          Run pipeline, utility and apply jobs on a cron timer. Times use the server's timezone unless you pick one.
        </p>
      </div>
      <button type="button" class="btn btn-primary" @click="startCreate" v-if="!open">
        New schedule
      </button>
    </section>

    <!-- Editor -->
    <section v-if="open" class="card sched-editor">
      <h3>{{ editing ? 'Edit schedule' : 'New schedule' }}</h3>

      <div class="sched-grid">
        <label class="fld">
          <span>Name</span>
          <input class="input" v-model="form.name" placeholder="Nightly sync" />
        </label>

        <label class="fld">
          <span>Job</span>
          <select class="select" v-model="form.job">
            <option v-for="j in catalog" :key="j.job" :value="j.job">{{ j.label }}</option>
          </select>
        </label>
      </div>

      <p v-if="selectedForm" class="muted">{{ selectedForm.description }}</p>
      <p v-if="selectedForm?.warning" class="sched-warning">{{ selectedForm.warning }}</p>

      <!-- Job params, described by the server catalog -->
      <div v-if="selectedForm?.fields.length" class="sched-params">
        <div v-for="f in selectedForm.fields" :key="f.key" class="fld">
          <label v-if="f.type === 'bool'" class="opt-check">
            <input type="checkbox" v-model="form.params[f.key]" />
            {{ f.label }}
          </label>
          <template v-else-if="f.type === 'multiselect'">
            <span>{{ f.label }}</span>
            <div class="sched-multi">
              <label v-for="opt in f.options" :key="opt" class="opt-check">
                <input type="checkbox" :value="opt" v-model="(form.params[f.key] as string[])" />
                {{ opt }}
              </label>
            </div>
          </template>
          <template v-else-if="f.type === 'select'">
            <label :for="fieldId(f)">{{ f.label }}</label>
            <select class="select" :id="fieldId(f)" v-model="form.params[f.key]">
              <option v-for="opt in f.options" :key="opt" :value="opt">
                {{ opt === '' ? 'all' : opt }}
              </option>
            </select>
          </template>
          <template v-else>
            <label :for="fieldId(f)">{{ f.label }}</label>
            <input
              class="input"
              :id="fieldId(f)"
              :type="f.type === 'int' ? 'number' : 'text'"
              v-model="form.params[f.key]"
            />
          </template>
          <span v-if="f.help" class="muted">{{ f.help }}</span>
        </div>
      </div>

      <div class="sched-grid">
        <label class="fld">
          <span>Cron expression</span>
          <input class="input mono" v-model="form.cron" placeholder="0 3 * * *" spellcheck="false" />
          <span class="muted">minute hour day-of-month month day-of-week</span>
        </label>

        <label class="fld">
          <span>Timezone</span>
          <input
            class="input"
            v-model="form.timezone"
            list="sched-zones"
            placeholder="server default"
          />
          <datalist id="sched-zones">
            <option v-for="z in zones" :key="z" :value="z" />
          </datalist>
        </label>
      </div>

      <div class="sched-presets">
        <button
          v-for="p in PRESETS"
          :key="p.cron"
          type="button"
          class="btn btn-sm"
          @click="form.cron = p.cron"
        >
          {{ p.label }}
        </button>
      </div>

      <p v-if="cronError" class="sched-error" role="alert">{{ cronError }}</p>
      <p v-else-if="preview.length" class="muted">
        Next runs: <span class="mono">{{ preview.map(fmtTime).join(' · ') }}</span>
      </p>

      <div class="sched-consent">
        <label class="opt-check">
          <input type="checkbox" v-model="form.enabled" />
          enabled
        </label>
        <label v-if="selectedForm?.needs_confirm" class="opt-check">
          <input type="checkbox" v-model="form.confirm" />
          I understand this runs unattended and may write to the NAS
        </label>
      </div>

      <div class="sched-actions">
        <button type="button" class="btn btn-primary" :disabled="saving" @click="save">
          {{ editing ? 'Save changes' : 'Create schedule' }}
        </button>
        <button type="button" class="btn" @click="closeEditor">Cancel</button>
      </div>
    </section>

    <!-- Listing -->
    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="!items.length" class="muted">
      No schedules yet. Anything you can run from the Pipeline, Utilities or Apply pages can be put
      on a timer here, except the actions that need a typed confirmation, they always stay manual.
    </p>

    <section v-for="s in items" :key="s.id" class="card sched-card" :class="{ off: !s.enabled }">
      <div class="sched-head">
        <div class="sched-title">
          <strong>{{ s.name }}</strong>
          <span class="badge">{{ s.job_label }}</span>
          <span v-if="!s.enabled" class="badge state-cancelled">disabled</span>
          <span
            v-else-if="s.last_status && s.last_status !== 'submitted'"
            class="badge"
            :class="s.last_status === 'error' ? 'state-failed' : 'state-queued'"
            >{{ s.last_status }}</span
          >
        </div>
        <div class="sched-buttons">
          <button type="button" class="btn btn-sm" @click="runNow(s)">Run now</button>
          <button type="button" class="btn btn-sm" @click="startEdit(s)">Edit</button>
          <button type="button" class="btn btn-sm" @click="toggle(s)">
            {{ s.enabled ? 'Disable' : 'Enable' }}
          </button>
          <button type="button" class="btn btn-sm btn-danger" @click="remove(s)">Delete</button>
        </div>
      </div>

      <div class="sched-meta muted">
        <span class="mono">{{ s.cron }}</span>
        <span v-if="s.timezone">· {{ s.timezone }}</span>
        <span v-if="paramSummary(s)">· {{ paramSummary(s) }}</span>
      </div>

      <div class="sched-meta">
        <span v-if="s.enabled">
          Next: <strong>{{ fmtTime(s.next_run_at) }}</strong>
          <span class="muted"> ({{ fmtRelative(s.next_run_at) }})</span>
        </span>
        <span v-else class="muted">Not scheduled</span>
        <span class="muted">
          · Last: {{ fmtTime(s.last_run_at) }}
          <template v-if="s.last_run_at">({{ fmtRelative(s.last_run_at) }})</template>
        </span>
        <button
          v-if="s.runs && s.runs.length"
          type="button"
          class="btn btn-ghost btn-sm"
          @click="toggleRuns(s.id)"
        >
          {{ expanded === s.id ? 'Hide history' : `History (${s.runs.length})` }}
        </button>
      </div>

      <ul v-if="expanded === s.id" class="sched-runs">
        <li v-for="r in s.runs" :key="r.id">
          <span class="mono">{{ fmtTime(r.fired_at) }}</span>
          <span class="badge" :class="r.status === 'error' ? 'state-failed' : ''">{{
            r.status
          }}</span>
          <RouterLink v-if="r.job_id" :to="`/jobs/${r.job_id}`" class="mono"
            >#{{ r.job_id }}</RouterLink
          >
          <span v-if="r.job_state" class="badge" :class="`state-${r.job_state}`">{{
            r.job_state
          }}</span>
          <span v-if="r.detail" class="muted">{{ r.detail }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.sched-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sched-intro {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
  justify-content: space-between;
}
.sched-intro h3,
.sched-editor h3 {
  margin: 0 0 var(--sp-1);
}
.sched-editor {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sched-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--sp-3);
}
.sched-params {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--sp-2);
  padding: var(--sp-2);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
}
.fld {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-sm);
}
.sched-multi {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.opt-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.sched-presets {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.sched-consent,
.sched-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  align-items: center;
}
.sched-warning {
  color: var(--danger);
  font-size: var(--fs-sm);
  margin: 0;
}
.sched-error {
  color: var(--danger);
  font-size: var(--fs-sm);
  margin: 0;
}
.sched-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sched-card.off {
  opacity: 0.65;
}
.sched-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  justify-content: space-between;
}
.sched-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.sched-buttons {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
}
.sched-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  align-items: center;
  font-size: var(--fs-sm);
}
.sched-runs {
  list-style: none;
  margin: 0;
  padding: var(--sp-2);
  border-top: 1px solid var(--border-soft);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
}
.sched-runs li {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  flex-wrap: wrap;
}
</style>
