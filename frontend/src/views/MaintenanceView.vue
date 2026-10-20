<script setup lang="ts">
// Maintenance page: one card per destructive local-state command with live
// "what will be removed" counts. Those come from four /api/maintenance/*
// endpoints fetched in parallel, split by cost, so a slow table count or crops
// walk only holds back its own card. Consent (plan §6) is
// enforced by the shared confirm dialog: clear-queue / clear-applies /
// prune-queue / delete-crops / reset take a plain confirm, reset --all takes the typed phrase
// "reset all". Each action submits a job so the shared JobPanel streams its
// progress. The server's validate_consent re-checks every gate regardless.
// Photo deduplication lives on the Utilities page — it acts on the NAS library,
// not on local pipeline state.
import { ref, reactive, onMounted } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import { getJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'

const PHRASE_RESET_ALL = 'reset all'

interface OrphanCounts {
  by_status?: Record<string, number>
  prunable?: number | null
}

interface QueueCounts {
  pending_queue: number | null
  queued_applies: { approved: number | null; failed: number | null }
}
interface TableCounts {
  faces: number | null
  embeddings: number | null
  cluster_runs: number | null
  photos: number | null
}
interface CropUsage {
  files: number | null
  bytes: number | null
}

// A section is pending until its first response; a reload after a job keeps the
// previous figures on screen rather than flashing spinners back in.
function section<T>(url: string) {
  const data = ref<T | null>(null)
  const loading = ref(true)
  async function load(): Promise<void> {
    loading.value = true
    try {
      data.value = await getJSON<T>(url)
    } catch {
      /* transient */
    } finally {
      loading.value = false
    }
  }
  return { data, pending: () => loading.value && data.value == null, load }
}

const queue = section<QueueCounts>('/api/maintenance/counts')
const tables = section<TableCounts>('/api/maintenance/tables')
const orphans = section<OrphanCounts>('/api/maintenance/orphans')
const crops = section<CropUsage>('/api/maintenance/crops')
const panel = ref<InstanceType<typeof JobPanel> | null>(null)

const reset = reactive({ all: false, keepCrops: false })

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return 'n/a'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i]
}

function show(v: number | null | undefined): string {
  return v == null ? '—' : String(v)
}

const cropsText = () => {
  const c = crops.data.value
  if (!c || c.files == null) return 'n/a'
  return c.files + ' files · ' + fmtBytes(c.bytes)
}

function loadCounts(): void {
  void Promise.all([queue.load(), tables.load(), orphans.load(), crops.load()])
}

function startJob(
  name: string,
  params: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
): void {
  panel.value
    ?.start(name, params, extra)
    .then(() => window.setTimeout(loadCounts, 500))
    .catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 428) toast('Consent required.', 'error')
      else toast((e as Error).message || 'Failed to start job', 'error')
    })
}

async function clearQueue(): Promise<void> {
  const ok = await confirm({
    title: 'Clear review queue',
    message: 'Remove all pending review items? Approved and applied decisions are kept.',
    okLabel: 'Clear queue',
  })
  if (ok) startJob('clear-queue', {}, { confirm: true })
}

const queuedApplies = () => {
  const q = queue.data.value?.queued_applies
  return (q?.approved || 0) + (q?.failed || 0)
}

async function clearApplies(): Promise<void> {
  const ok = await confirm({
    title: 'Clear queued applies',
    message:
      'Send approved decisions that were never written to the NAS — plus any that failed — back to the review queue? Nothing is deleted, and already-applied decisions are kept.',
    okLabel: 'Clear queued applies',
  })
  if (ok) startJob('clear-applies', {}, { confirm: true })
}

// Orphans a human already decided on are counted but never pruned by default:
// an approved one still writes to the NAS from its stored payload, so it is worth
// naming in the dialog rather than sweeping up silently.
const pruneOpts = reactive({ includeApproved: false })

const prunable = () => orphans.data.value?.prunable ?? null
const decidedOrphans = () => {
  const by = orphans.data.value?.by_status
  if (!by) return 0
  return Object.entries(by)
    .filter(([s]) => s !== 'pending' && s !== 'rejected')
    .reduce((n, [, v]) => n + v, 0)
}
const orphanBreakdown = () => {
  const by = orphans.data.value?.by_status
  if (!by || Object.keys(by).length === 0) return 'none'
  return Object.entries(by)
    .sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `${n} ${s}`)
    .join(' · ')
}

async function pruneQueue(): Promise<void> {
  const decided = decidedOrphans()
  const extra =
    pruneOpts.includeApproved && decided > 0
      ? ` This also drops ${decided} item(s) you already decided on.`
      : ''
  const ok = await confirm({
    title: 'Remove broken review items',
    message:
      'Delete review items whose faces no longer exist in the database? Their photos were re-detected, so there is nothing left to show or apply — Group faces re-proposes them from the current faces.' +
      extra,
    okLabel: 'Remove items',
  })
  if (ok) startJob('prune-queue', { include_approved: pruneOpts.includeApproved }, { confirm: true })
}

async function deleteCrops(): Promise<void> {
  const ok = await confirm({
    title: 'Delete crop images',
    message: 'Wipe all cached face crops? They can be rebuilt with regen-crops.',
    okLabel: 'Delete crops',
  })
  if (ok) startJob('delete-crops', {}, { confirm: true })
}

async function runReset(): Promise<void> {
  const params: Record<string, unknown> = { keep_crops: reset.keepCrops }
  if (reset.all) {
    params.all = true
    const ok = await confirm({
      title: 'Reset EVERYTHING',
      message:
        'This drops all local pipeline data including synced photos. The NAS is untouched, but you will need to re-sync.',
      phrase: PHRASE_RESET_ALL,
      okLabel: 'Reset all',
    })
    if (ok) startJob('reset', params, { confirm_phrase: PHRASE_RESET_ALL })
  } else {
    const ok = await confirm({
      title: 'Reset local database',
      message: 'Drop faces, embeddings, face groups and the review queue from the local DB?',
      okLabel: 'Reset',
    })
    if (ok) startJob('reset', params, { confirm: true })
  }
}

onMounted(loadCounts)
</script>

<template>
  <div class="page">
    <JobPanel ref="panel" @done="loadCounts" />

    <div class="maint-grid" style="margin-top: var(--sp-4)">
      <section class="card maint-card">
        <h3>Clear review queue</h3>
        <p class="muted">Remove pending review items. Approved/applied decisions are unaffected.</p>
        <p>
          Pending items:
          <span class="maint-count"
            ><span v-if="queue.pending()" class="spinner" aria-label="Loading"></span
            ><template v-else>{{ show(queue.data.value?.pending_queue) }}</template></span
          >
        </p>
        <div class="maint-actions">
          <button type="button" class="btn btn-danger" @click="clearQueue">Clear queue…</button>
        </div>
      </section>

      <section class="card maint-card">
        <h3>Clear queued applies</h3>
        <p class="muted">
          Return approved decisions that never reached the NAS, and failed ones, to the review
          queue. Applied decisions are kept.
        </p>
        <p v-if="queue.pending()">
          Waiting: <span class="spinner" aria-label="Loading"></span>
        </p>
        <p v-else>
          Waiting:
          <span class="maint-count">{{ show(queue.data.value?.queued_applies?.approved) }}</span> ·
          Failed: <span class="maint-count">{{ show(queue.data.value?.queued_applies?.failed) }}</span>
        </p>
        <div class="maint-actions">
          <button
            type="button"
            class="btn btn-danger"
            :disabled="queuedApplies() === 0"
            @click="clearApplies"
          >
            Clear queued applies…
          </button>
        </div>
      </section>

      <section class="card maint-card">
        <h3>Remove broken review items</h3>
        <p class="muted">
          Items whose faces were re-detected under new ids. They show no face and cannot be
          repaired by Regenerate crops; Group faces re-proposes them from the current faces.
        </p>
        <p>
          Broken:
          <span class="maint-count"
            ><span v-if="orphans.pending()" class="spinner" aria-label="Loading"></span
            ><template v-else>{{ orphanBreakdown() }}</template></span
          >
        </p>
        <div class="maint-opts">
          <label class="opt-check"
            ><input type="checkbox" v-model="pruneOpts.includeApproved" /> also remove ones you
            already decided on ({{ decidedOrphans() }})</label
          >
        </div>
        <div class="maint-actions">
          <button
            type="button"
            class="btn btn-danger"
            :disabled="!prunable() && !(pruneOpts.includeApproved && decidedOrphans() > 0)"
            @click="pruneQueue"
          >
            Remove items…
          </button>
        </div>
      </section>

      <section class="card maint-card">
        <h3>Delete crop images</h3>
        <p class="muted">
          Wipe cached face crops to reclaim disk. They rebuild on demand via regen-crops.
        </p>
        <p>
          On disk:
          <span class="maint-count"
            ><span v-if="crops.pending()" class="spinner" aria-label="Loading"></span
            ><template v-else>{{ cropsText() }}</template></span
          >
        </p>
        <div class="maint-actions">
          <button type="button" class="btn btn-danger" @click="deleteCrops">Delete crops…</button>
        </div>
      </section>

      <section class="card maint-card danger-card">
        <h3>Reset local database</h3>
        <p class="muted">Drop pipeline data from the local DB. This does not touch the NAS.</p>
        <p v-if="tables.pending()">
          Faces: <span class="spinner" aria-label="Loading"></span>
        </p>
        <p v-else>
          Faces: <span class="maint-count">{{ show(tables.data.value?.faces) }}</span> ·
          Embeddings: <span class="maint-count">{{ show(tables.data.value?.embeddings) }}</span> ·
          Grouping runs:
          <span class="maint-count">{{ show(tables.data.value?.cluster_runs) }}</span> · Photos:
          <span class="maint-count">{{ show(tables.data.value?.photos) }}</span>
        </p>
        <div class="maint-opts">
          <label class="opt-check"
            ><input type="checkbox" v-model="reset.all" /> reset EVERYTHING (--all, includes synced
            photos)</label
          >
          <label class="opt-check"
            ><input type="checkbox" v-model="reset.keepCrops" /> keep crop images
            (--keep-crops)</label
          >
        </div>
        <div class="maint-actions">
          <button type="button" class="btn btn-danger" @click="runReset">Reset…</button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.maint-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: var(--sp-3);
}
.maint-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.maint-card h3 {
  margin: 0;
}
.maint-card.danger-card {
  border-color: var(--danger);
  box-shadow: 0 0 0 1px var(--danger) inset;
}
.maint-count {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.maint-card .spinner {
  display: inline-block;
  vertical-align: middle;
}
.maint-opts {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.maint-opts .opt-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.maint-opts label {
  font-size: var(--fs-sm);
}
.opt-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.maint-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: auto;
  padding-top: var(--sp-2);
}
</style>
