<script setup lang="ts">
// Maintenance page: one card per destructive command with live "what will be
// removed" counts (/api/maintenance/counts). Ports maintenance.html.j2 +
// maintenance.js. Consent (plan §6) is enforced by the shared confirm dialog:
// clear-queue / delete-crops / reset take a plain confirm; dedupe --apply and
// reset --all take typed phrases ("delete duplicates" / "reset all"). Each
// action submits a job so the shared JobPanel streams its progress. The server's
// validate_consent re-checks every gate regardless.
import { ref, reactive, onMounted } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import { getJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'

const PHRASE_DEDUPE = 'delete duplicates'
const PHRASE_RESET_ALL = 'reset all'

interface Counts {
  pending_queue: number | null
  faces: number | null
  embeddings: number | null
  cluster_runs: number | null
  photos: number | null
  crops: { files: number | null; bytes: number | null }
}
const counts = ref<Counts | null>(null)
const panel = ref<InstanceType<typeof JobPanel> | null>(null)

const dedupe = reactive({ exact: true, visual: false, threshold: '' })
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
  const c = counts.value?.crops
  if (!c || c.files == null) return 'n/a'
  return c.files + ' files · ' + fmtBytes(c.bytes)
}

async function loadCounts(): Promise<void> {
  try {
    counts.value = await getJSON<Counts>('/api/maintenance/counts')
  } catch {
    /* transient */
  }
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

function dedupeParams(): Record<string, unknown> {
  const params: Record<string, unknown> = { exact: dedupe.exact, visual: dedupe.visual }
  const t = dedupe.threshold.trim()
  if (t !== '') params.threshold = t
  return params
}

function dedupePreview(): void {
  if (!dedupe.exact && !dedupe.visual) {
    toast('Pick exact and/or visual.', 'error')
    return
  }
  startJob('dedupe', dedupeParams())
}

async function dedupeApply(): Promise<void> {
  if (!dedupe.exact && !dedupe.visual) {
    toast('Pick exact and/or visual.', 'error')
    return
  }
  const ok = await confirm({
    title: 'Delete duplicate photos',
    message: 'This permanently deletes duplicate photos from the NAS.',
    phrase: PHRASE_DEDUPE,
    okLabel: 'Delete duplicates',
  })
  if (!ok) return
  const params = dedupeParams()
  params.apply = true
  startJob('dedupe', params, { confirm: true, confirm_phrase: PHRASE_DEDUPE })
}

async function clearQueue(): Promise<void> {
  const ok = await confirm({
    title: 'Clear review queue',
    message: 'Remove all pending review items? Approved and applied decisions are kept.',
    okLabel: 'Clear queue',
  })
  if (ok) startJob('clear-queue', {}, { confirm: true })
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
      message: 'Drop faces, embeddings, clusters and the review queue from the local DB?',
      okLabel: 'Reset',
    })
    if (ok) startJob('reset', params, { confirm: true })
  }
}

onMounted(() => void loadCounts())
</script>

<template>
  <div class="page">
    <JobPanel ref="panel" @done="loadCounts" />

    <div class="maint-grid" style="margin-top: var(--sp-4)">
      <section class="card maint-card">
        <h3>Deduplicate photos</h3>
        <p class="muted">
          Delete duplicate photos from the NAS using stored hashes. Dry run is free; applying
          deletes on the NAS.
        </p>
        <div class="maint-opts">
          <label class="opt-check"
            ><input type="checkbox" v-model="dedupe.exact" /> exact (sha256 identical)</label
          >
          <label class="opt-check"
            ><input type="checkbox" v-model="dedupe.visual" /> visual (pHash near-duplicates)</label
          >
          <div class="opt-row">
            <label for="dd-threshold">Hamming threshold</label>
            <input
              class="input input-sm"
              id="dd-threshold"
              type="number"
              min="0"
              max="64"
              placeholder="default"
              v-model="dedupe.threshold"
            />
          </div>
        </div>
        <div class="maint-actions">
          <button type="button" class="btn" @click="dedupePreview">Dry run</button>
          <button type="button" class="btn btn-danger" @click="dedupeApply">
            Delete duplicates…
          </button>
        </div>
      </section>

      <section class="card maint-card">
        <h3>Clear review queue</h3>
        <p class="muted">Remove pending review items. Approved/applied decisions are unaffected.</p>
        <p>Pending items: <span class="maint-count">{{ show(counts?.pending_queue) }}</span></p>
        <div class="maint-actions">
          <button type="button" class="btn btn-danger" @click="clearQueue">Clear queue…</button>
        </div>
      </section>

      <section class="card maint-card">
        <h3>Delete crop images</h3>
        <p class="muted">
          Wipe cached face crops to reclaim disk. They rebuild on demand via regen-crops.
        </p>
        <p>On disk: <span class="maint-count">{{ cropsText() }}</span></p>
        <div class="maint-actions">
          <button type="button" class="btn btn-danger" @click="deleteCrops">Delete crops…</button>
        </div>
      </section>

      <section class="card maint-card danger-card">
        <h3>Reset local database</h3>
        <p class="muted">Drop pipeline data from the local DB. This does not touch the NAS.</p>
        <p>
          Faces: <span class="maint-count">{{ show(counts?.faces) }}</span> · Embeddings:
          <span class="maint-count">{{ show(counts?.embeddings) }}</span> · Cluster runs:
          <span class="maint-count">{{ show(counts?.cluster_runs) }}</span> · Photos:
          <span class="maint-count">{{ show(counts?.photos) }}</span>
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
