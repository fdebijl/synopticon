<script setup lang="ts">
// Pipeline page: command cards → whitelisted option forms → POST /api/jobs via
// the shared JobPanel, plus a job-history table. Ports pipeline.html.j2 +
// pipeline.js. The server is the single validator (JOB_SPECS); the forms only
// collect the params each builder accepts. All Run buttons disable while any job
// is queued/running (read from the shared jobs store, which owns the single
// /api/jobs poller, and refreshed on job-done).
import { computed, ref, reactive } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import { toast } from '../stores/toasts'
import { useJobs } from '../stores/jobs'
import { jobLabel } from '../utils/jobs'
import type { Job } from '../api/types'

const SPACES: [string, string][] = [
  ['', 'both (default)'],
  ['personal', 'personal'],
  ['shared', 'shared'],
]

type Field =
  | { kind: 'space'; param: string }
  | { kind: 'check'; param: string; label: string; default?: boolean }
  | { kind: 'number'; param: string; label: string; placeholder?: string; min?: number; max?: number }
  | { kind: 'text'; param: string; label: string; placeholder?: string }

interface CardDef {
  cmd: string
  title: string
  primary: boolean;
  desc: string
  disclosure?: string
  fields: Field[]
  overrides?: boolean
}

const CARDS: CardDef[] = [
  {
    cmd: 'sync',
    title: 'Sync',
    primary: true,
    desc: 'Pull the photo library, persons and faces from the NAS.',
    disclosure: 'Options',
    fields: [
      { kind: 'space', param: 'space' },
      { kind: 'check', param: 'hash', label: 'compute content hashes (--hash)' },
      { kind: 'check', param: 'skip_faces', label: 'skip faces (--skip-faces)' },
      { kind: 'check', param: 'all_faces', label: 'all faces (--all-faces)' },
      { kind: 'check', param: 'resume', label: 'resume from last sync', default: true },
    ],
  },
  {
    cmd: 'extract',
    title: 'Detect faces',
    primary: true,
    desc: 'Scan synced photos and record every face found in them.',
    disclosure: 'Options',
    fields: [
      { kind: 'space', param: 'space' },
      { kind: 'number', param: 'limit', label: 'Limit', placeholder: 'all', min: 1 },
      { kind: 'number', param: 'photo_id', label: 'Photo id', placeholder: '—', min: 1 },
    ],
  },
  {
    cmd: 'cluster',
    title: 'Group faces',
    primary: true,
    desc: 'Group the detected faces by person and match them against your Synology people.',
    fields: [],
  },
  {
    cmd: 'recluster',
    title: 'Re-group faces',
    primary: false,
    desc: 'Group the faces again with different settings. Works offline — no NAS access needed.',
    disclosure: 'Overrides',
    fields: [],
    overrides: true,
  },
  {
    cmd: 'report',
    title: 'Report',
    primary: false,
    desc: 'Regenerate the static HTML review report.',
    disclosure: 'Options',
    fields: [{ kind: 'number', param: 'run_id', label: 'Run id', placeholder: 'latest', min: 1 }],
  },
  {
    cmd: 'regen-crops',
    title: 'Regenerate crops',
    primary: false,
    desc: 'Rebuild face crop images from stored bboxes.',
    disclosure: 'Options',
    fields: [
      { kind: 'space', param: 'space' },
      { kind: 'check', param: 'only_missing', label: 'only missing crops', default: true },
      { kind: 'number', param: 'limit', label: 'Limit', placeholder: 'all', min: 1 },
    ],
  },
  {
    cmd: 'benchmark',
    title: 'Benchmark',
    primary: false,
    desc: 'Measure how fast face detection runs on a sample of photos.',
    disclosure: 'Options',
    fields: [
      { kind: 'space', param: 'space' },
      { kind: 'number', param: 'limit', label: 'Limit', placeholder: 'default', min: 1 },
      { kind: 'number', param: 'warmup', label: 'Warmup', placeholder: 'default', min: 0 },
    ],
  },
  {
    cmd: 'models-download',
    title: 'Download models',
    primary: false,
    desc: 'Fetch and verify model weights from the manifest.',
    disclosure: 'Options',
    fields: [
      {
        kind: 'text',
        param: 'only',
        label: 'Only',
        placeholder: 'comma-separated keys (blank = all)',
      },
      {
        kind: 'check',
        param: 'allow_record_hash',
        label: 'record hash of manually-added models (--allow-record-hash)',
      },
    ],
  },
]

const GROUPS = [
  { key: 'primary', cards: CARDS.filter((c) => c.primary) },
  { key: 'ops', cards: CARDS.filter((c) => !c.primary) },
]

// Per-card reactive form state, seeded from the field defaults.
const params = reactive<Record<string, Record<string, string | boolean>>>({})
for (const card of CARDS) {
  const p: Record<string, string | boolean> = {}
  for (const f of card.fields) {
    if (f.kind === 'check') p[f.param] = f.default ?? false
    else p[f.param] = ''
  }
  params[card.cmd] = p
}

interface KvRow {
  key: string
  val: string
}
const overrides = reactive<Record<string, KvRow[]>>({})
for (const card of CARDS) if (card.overrides) overrides[card.cmd] = [{ key: '', val: '' }]

const open = reactive<Record<string, boolean>>({})
function toggle(cmd: string): void {
  open[cmd] = !open[cmd]
}

function addKv(cmd: string): void {
  overrides[cmd].push({ key: '', val: '' })
}
function removeKv(cmd: string, i: number): void {
  overrides[cmd].splice(i, 1)
}

function buildOverrides(cmd: string): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const row of overrides[cmd] || []) {
    const k = row.key.trim()
    if (!k) continue
    const raw = row.val.trim()
    let val: unknown
    try {
      val = JSON.parse(raw)
    } catch {
      val = raw
    }
    out[k] = val
  }
  return out
}

function buildParams(card: CardDef): Record<string, unknown> {
  const p = params[card.cmd]
  const out: Record<string, unknown> = {}
  for (const f of card.fields) {
    if (f.kind === 'check') {
      out[f.param] = !!p[f.param]
    } else {
      const s = String(p[f.param] ?? '').trim()
      if (s !== '') out[f.param] = s
    }
  }
  if (card.overrides) out.overrides = buildOverrides(card.cmd)
  return out
}

const panel = ref<InstanceType<typeof JobPanel> | null>(null)
const { state: jobs, refreshJobs } = useJobs()
const history = computed<Job[]>(() => jobs.history)
const running = computed(() => jobs.running !== null)

function run(card: CardDef): void {
  panel.value
    ?.start(card.cmd, buildParams(card))
    .then(() => void refreshJobs())
    .catch((err: Error) => toast(err.message || 'Failed to start job', 'error'))
}

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

function fmtDuration(m: Job): string {
  if (!m.started_at) return '—'
  const end = m.ended_at || Date.now() / 1000
  const s = Math.max(0, Math.round(end - m.started_at))
  if (s < 60) return s + 's'
  const mm = Math.floor(s / 60)
  const ss = s % 60
  return mm + 'm ' + ss + 's'
}

</script>

<template>
  <div class="page">
    <JobPanel ref="panel" @done="refreshJobs" />

    <template v-for="group in GROUPS" :key="group.key">
      <div v-if="group.key === 'ops'" class="ops-divider">
        <span class="ops-divider-label">Other operations</span>
      </div>

      <div class="cmd-grid" :class="`cmd-grid-${group.key}`">
        <section
          v-for="card in group.cards"
          :key="card.cmd"
          class="card cmd-card"
          :class="{ open: open[card.cmd] }"
        >
          <div class="cmd-head">
            <div>
              <div class="cmd-title">{{ card.title }}</div>
              <div class="cmd-desc">{{ card.desc }}</div>
            </div>
            <template v-if="card.disclosure">
              <div class="cmd-head-spacer"></div>
              <button type="button" class="disclosure" @click="toggle(card.cmd)">
                {{ card.disclosure }}
              </button>
            </template>
          </div>

          <div v-if="card.disclosure" class="cmd-opts">
            <template v-if="card.overrides">
              <p class="muted">
                Only <code>clustering.*</code> / <code>crossref.*</code> keys are accepted (the server
                validates too). Values are JSON: <code>0.55</code>, <code>true</code>,
                <code>"cw"</code>.
              </p>
              <div class="kv-editor">
                <div v-for="(row, i) in overrides[card.cmd]" :key="i" class="kv-row">
                  <input class="input" v-model="row.key" placeholder="clustering.threshold" />
                  <input class="input" v-model="row.val" placeholder="0.55" />
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    aria-label="Remove"
                    @click="removeKv(card.cmd, i)"
                  >
                    &times;
                  </button>
                </div>
              </div>
              <button type="button" class="btn btn-sm" @click="addKv(card.cmd)">
                + add override
              </button>
            </template>

            <template v-for="f in card.fields" :key="f.param">
              <div v-if="f.kind === 'space'" class="opt-row">
                <label>Space</label>
                <select class="select" v-model="params[card.cmd][f.param]">
                  <option v-for="[v, l] in SPACES" :key="v" :value="v">{{ l }}</option>
                </select>
              </div>
              <label v-else-if="f.kind === 'check'" class="opt-check">
                <input type="checkbox" v-model="params[card.cmd][f.param]" /> {{ f.label }}
              </label>
              <div v-else-if="f.kind === 'number'" class="opt-row">
                <label>{{ f.label }}</label>
                <input
                  class="input input-sm"
                  type="number"
                  :min="f.min"
                  :max="f.max"
                  :placeholder="f.placeholder"
                  v-model="params[card.cmd][f.param]"
                />
              </div>
              <div v-else class="opt-row">
                <label>{{ f.label }}</label>
                <input
                  class="input"
                  type="text"
                  :placeholder="f.placeholder"
                  v-model="params[card.cmd][f.param]"
                />
              </div>
            </template>
          </div>

          <div class="cmd-actions">
            <button
              type="button"
              class="btn"
              :class="group.key === 'primary' ? 'btn-action' : 'btn-quiet'"
              :disabled="running"
              @click="run(card)"
            >
              Run
            </button>
          </div>
        </section>
      </div>
    </template>

    <div class="card" style="margin-top: var(--sp-4)">
      <h3>Job history</h3>
      <p v-if="running" class="run-note">
        A job is running — Run buttons are disabled until it finishes.
      </p>
      <table class="data history-table">
        <thead>
          <tr>
            <th>Job</th>
            <th>State</th>
            <th>Started at</th>
            <th>Ended at</th>
            <th>Duration</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!history.length">
            <td colspan="6" class="muted">No jobs yet.</td>
          </tr>
          <tr v-for="m in history" :key="m.id">
            <td class="hist-name">{{ jobLabel(m.name) }}</td>
            <td><span class="badge" :class="`state-${m.state}`">{{ m.state }}</span></td>
            <td class="hist-time">{{ fmtTime(m.started_at) }}</td>
            <td class="hist-time">{{ fmtTime(m.ended_at) }}</td>
            <td>{{ fmtDuration(m) }}</td>
            <td><RouterLink :to="`/jobs/${m.id}`">view</RouterLink></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.cmd-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--sp-3);
}
.cmd-grid-primary {
  margin-top: var(--sp-4);
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
}
.cmd-grid-primary .cmd-card {
  min-height: 180px;
  padding: var(--sp-5);
  gap: var(--sp-3);
}
.cmd-grid-primary .cmd-title {
  font-size: var(--fs-xl);
}
.cmd-grid-ops .cmd-card {
  padding: var(--sp-3) var(--sp-4);
}
.cmd-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.ops-divider {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin: var(--sp-5) 0 var(--sp-3);
}
.ops-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-soft);
}
.ops-divider-label {
  font-size: var(--fs-sm);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-2);
}
.btn-quiet {
  background: transparent;
  border-color: var(--border);
  color: var(--text-2);
}
.btn-quiet:hover:not(:disabled) {
  background: var(--bg-sunken);
  color: var(--text);
}
.cmd-head {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.cmd-head .cmd-title {
  font-weight: 600;
  font-size: var(--fs-lg);
}
.cmd-head .cmd-desc {
  color: var(--text-2);
  font-size: var(--fs-sm);
  margin-top: 2px;
}
.cmd-head-spacer {
  flex: 1;
}
.cmd-opts {
  border-top: 1px solid var(--border-soft);
  padding-top: var(--sp-2);
  display: none;
  flex-direction: column;
  gap: var(--sp-2);
}
.cmd-card.open .cmd-opts {
  display: flex;
}
.opt-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.opt-row label {
  font-size: var(--fs-sm);
  color: var(--text-2);
  min-width: 72px;
}
.opt-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--text);
  min-width: 0;
}
.kv-editor {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.kv-row {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
}
.kv-row .input {
  flex: 1;
}
.cmd-actions {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  margin-top: auto;
}
.disclosure {
  background: transparent;
  border: none;
  color: var(--action);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-sm);
  padding: 0;
}
.history-table td,
.history-table th {
  white-space: nowrap;
}
.history-table td.hist-name {
  font-weight: 600;
}
.history-table td.hist-time {
  color: var(--text-2);
  font-size: var(--fs-sm);
  font-variant-numeric: tabular-nums;
}
.run-note {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
</style>
