<script setup lang="ts">
// Utilities: interactive tools that act on the library itself rather than on
// local pipeline state (which is what Maintenance is for). QuickMerger talks to
// the NAS directly through /api/quickmerger/*; deduplication runs as a job, so
// it shares the standard JobPanel and the typed-phrase consent gate the server
// re-checks in validate_consent. The two backups are plain downloads rather
// than jobs — what they produce is a file the browser has to receive, not a log.
import { ref, reactive, onMounted } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import QuickMerger from '../components/QuickMerger.vue'
import { ApiError, getJSON, downloadFile } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'

const PHRASE_DEDUPE = 'delete duplicates'

const panel = ref<InstanceType<typeof JobPanel> | null>(null)
const dedupe = reactive({ exact: true, visual: false, threshold: '' })

interface BackupInfo {
  config: { path: string; exists: boolean; secret_keys: string[] }
  database: { backend: string; bytes: number | null }
}
const backup = ref<BackupInfo | null>(null)
const includeSecrets = ref(false)
const busyConfig = ref(false)
const busyDatabase = ref(false)

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return 'unknown size'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i]
}

const databaseNote = () => {
  const db = backup.value?.database
  if (!db) return ''
  if (db.backend === 'postgres')
    return 'PostgreSQL — exported table by table into a portable SQLite file.'
  return 'SQLite — ' + fmtBytes(db.bytes) + ' on disk. The copy is compacted, so expect it smaller.'
}

function downloadError(e: unknown): void {
  if (e instanceof ApiError && e.status === 409)
    toast('A backup is already being prepared.', 'error')
  else toast((e as Error).message || 'Download failed', 'error')
}

async function downloadSettings(): Promise<void> {
  if (includeSecrets.value) {
    const ok = await confirm({
      title: 'Include credentials',
      message:
        'The downloaded file will contain your NAS and database passwords in plain text. Store it somewhere you would store the passwords themselves.',
      okLabel: 'Download with credentials',
    })
    if (!ok) return
  }
  busyConfig.value = true
  try {
    const name = await downloadFile(
      '/api/backup/config' + (includeSecrets.value ? '?secrets=1' : ''),
      'synopticon-config.toml',
    )
    toast('Saved ' + name, 'ok')
  } catch (e) {
    downloadError(e)
  } finally {
    busyConfig.value = false
  }
}

async function downloadDatabase(): Promise<void> {
  busyDatabase.value = true
  toast('Preparing the snapshot — this can take a while on a large library.')
  try {
    const name = await downloadFile('/api/backup/database', 'synopticon-backup.db')
    toast('Saved ' + name, 'ok')
  } catch (e) {
    downloadError(e)
  } finally {
    busyDatabase.value = false
  }
}

onMounted(() => {
  getJSON<BackupInfo>('/api/backup/info')
    .then((info) => (backup.value = info))
    .catch(() => {
      /* the cards still work; only the blurb is missing */
    })
})

function startJob(
  name: string,
  params: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
): void {
  panel.value?.start(name, params, extra).catch((e: unknown) => {
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
</script>

<template>
  <div class="page util-page">
    <JobPanel ref="panel" />

    <QuickMerger />

    <section class="card util-card">
      <h3>Deduplicate photos</h3>
      <p class="muted">
        Delete duplicate photos from the NAS using stored hashes. Dry run is free; applying deletes
        on the NAS.
      </p>
      <div class="util-opts">
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
      <div class="util-actions">
        <button type="button" class="btn" @click="dedupePreview">Dry run</button>
        <button type="button" class="btn btn-danger" @click="dedupeApply">
          Delete duplicates…
        </button>
      </div>
    </section>

    <section class="card util-card">
      <h3>Back up settings</h3>
      <p class="muted">
        Download your configuration file. Restore it by putting it back where it came from<template
          v-if="backup?.config"
        >
          (<code>{{ backup.config.path }}</code
          >)</template
        >.
      </p>
      <div class="util-opts">
        <label class="opt-check"
          ><input type="checkbox" v-model="includeSecrets" /> include credentials (passwords in
          plain text)</label
        >
        <p class="muted hint">
          Left off, these are blanked out and you re-enter them after restoring:
          {{ backup?.config.secret_keys.join(', ') || 'every password' }}.
        </p>
      </div>
      <div class="util-actions">
        <button type="button" class="btn" :disabled="busyConfig" @click="downloadSettings">
          {{ busyConfig ? 'Preparing…' : 'Download settings' }}
        </button>
      </div>
    </section>

    <section class="card util-card">
      <h3>Download database</h3>
      <p class="muted">
        A full copy of everything Synopticon knows — photos, faces, embeddings, face groups, review
        decisions and your account. Nothing is deleted and the NAS is untouched.
      </p>
      <p v-if="backup" class="muted">{{ databaseNote() }}</p>
      <div class="util-actions">
        <button type="button" class="btn" :disabled="busyDatabase" @click="downloadDatabase">
          {{ busyDatabase ? 'Preparing…' : 'Download database' }}
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.util-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.util-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.util-card h3 {
  margin: 0;
}
.util-opts {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.util-opts .opt-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.util-opts label {
  font-size: var(--fs-sm);
}
.opt-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.util-actions {
  display: flex;
  gap: var(--sp-2);
  padding-top: var(--sp-2);
}
.util-opts .hint {
  margin: 0;
  font-size: var(--fs-sm);
}
</style>
