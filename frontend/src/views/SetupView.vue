<script setup lang="ts">
// Resumable 6-step setup wizard. Ports templates/setup.html.j2 + setup.js:
//   0 account · 1 NAS + test-connection · 2 storage · 3 models · 4 sync · 5 done.
// Resumable from GET /api/setup/status; each step advances only once its own gate
// is satisfied. Rendered bare (no app chrome) via route meta. All step <section>s
// stay mounted so the two JobPanels keep streaming across step navigation.
import { ref, reactive, onMounted, nextTick } from 'vue'
import { getJSON, postJSON, putJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { fetchMe } from '../stores/auth'
import router from '../router'
import JobPanel from '../components/JobPanel.vue'
import type { DoneDetail } from '../composables/useJobStream'
import type {
  JobState,
  SetupStatus,
  ProbeResult,
  StorageCheckResult,
} from '../api/types'

const LAST_STEP = 5
const current = ref(0)
const status = ref<SetupStatus | null>(null)

const STEP_LABELS: { label: string; mark: string }[] = [
  { label: 'Account', mark: '1' },
  { label: 'NAS', mark: '2' },
  { label: 'Storage', mark: '3' },
  { label: 'Models', mark: '4' },
  { label: 'Sync', mark: '5' },
  { label: 'Done', mark: '✓' },
]

function dotState(n: number): '' | 'current' | 'done' {
  return n < current.value ? 'done' : n === current.value ? 'current' : ''
}

const wizardEl = ref<HTMLElement | null>(null)

function goto(step: number): void {
  current.value = Math.max(0, Math.min(LAST_STEP, step))
  // Focus the first control of the newly shown step for keyboard users.
  void nextTick(() => {
    const active = wizardEl.value?.querySelector('.wizard-step.active')
    const focusable = active?.querySelector<HTMLElement>('input, button, a')
    focusable?.focus()
  })
}

/** Earliest step not yet satisfied, given /api/setup/status. */
function resumeStep(s: SetupStatus): number {
  if (!s.account_created) return 0
  if (!s.nas_configured) return 1
  if (!s.models_ready) return 3
  if (!s.photos_synced) return 4
  return LAST_STEP
}

// A `done` emit is either the advisory `result` (JobEvent, has `event`) or the
// terminal `final` ({ state }). We only react to the terminal one here.
function terminalState(detail: DoneDetail): JobState | null {
  return 'event' in detail ? null : detail.state
}

// -- Step 0: create account ------------------------------------------------
const account = reactive({ username: '', password: '' })
const accountError = ref('')
const accountBusy = ref(false)

async function createAccount(): Promise<void> {
  accountError.value = ''
  accountBusy.value = true
  try {
    await postJSON('/api/auth/create-account', {
      username: account.username.trim(),
      password: account.password,
    })
    // The account now exists and a session cookie is set — refresh the auth
    // store so the router guard no longer forces first-boot / login.
    await fetchMe(true)
    goto(1)
  } catch (ex) {
    accountError.value =
      (ex instanceof ApiError && ex.message) || 'Could not create account.'
  } finally {
    accountBusy.value = false
  }
}

// -- Step 1: NAS + test connection ----------------------------------------
const nas = reactive({
  url: '',
  account: '',
  password: '',
  otp: '',
  verifyTls: true,
  spacePersonal: true,
  spaceShared: false,
})
const probe = ref<ProbeResult | null>(null)
const testBusy = ref(false)
const nasSaveEnabled = ref(false)

function nasSpaces(): string[] {
  const spaces: string[] = []
  if (nas.spacePersonal) spaces.push('personal')
  if (nas.spaceShared) spaces.push('shared')
  return spaces
}

async function testConnection(): Promise<void> {
  testBusy.value = true
  try {
    const result = await postJSON<ProbeResult>('/api/setup/test-connection', {
      url: nas.url.trim(),
      account: nas.account.trim(),
      password: nas.password,
      otp_code: nas.otp.trim(),
      verify_tls: nas.verifyTls,
      spaces: nasSpaces(),
    })
    probe.value = result
    nasSaveEnabled.value = !!result.ok
    if (result.ok) toast('Connection OK', 'ok')
    else toast(result.error || 'Connection failed', 'error')
  } catch (ex) {
    toast((ex instanceof Error && ex.message) || 'Test failed', 'error')
  } finally {
    testBusy.value = false
  }
}

async function saveNas(): Promise<void> {
  // Drop empty otp so it is not persisted; keep password only if entered.
  const body: Record<string, unknown> = {
    url: nas.url.trim(),
    account: nas.account.trim(),
    verify_tls: nas.verifyTls,
    spaces: nasSpaces(),
  }
  if (nas.password) body.password = nas.password
  if (nas.otp.trim()) body.otp_code = nas.otp.trim()
  try {
    await putJSON('/api/config', { nas: body })
    goto(2)
  } catch (ex) {
    // If PUT /api/config is unavailable, don't trap the user — advance and let
    // them configure via Settings later.
    if (ex instanceof ApiError && (ex.status === 404 || ex.status === 405)) {
      goto(2)
      return
    }
    toast((ex instanceof Error && ex.message) || 'Could not save NAS config', 'error')
  }
}

// -- Step 2: storage -------------------------------------------------------
const storage = reactive({
  dataDir: './data',
  modelsDir: './models',
  keepOriginals: false,
  cacheGb: 50,
})
const storageResult = ref<StorageCheckResult | null>(null)

async function checkStorage(): Promise<void> {
  try {
    storageResult.value = await postJSON<StorageCheckResult>('/api/setup/check-storage', {
      data_dir: storage.dataDir.trim(),
      models_dir: storage.modelsDir.trim(),
    })
  } catch (ex) {
    toast((ex instanceof Error && ex.message) || 'Check failed', 'error')
  }
}

async function saveStorage(): Promise<void> {
  try {
    await putJSON('/api/config', {
      storage: {
        data_dir: storage.dataDir.trim(),
        models_dir: storage.modelsDir.trim(),
        keep_originals: storage.keepOriginals,
        originals_cache_gb: Number(storage.cacheGb) || 50,
      },
    })
    goto(3)
  } catch (ex) {
    if (ex instanceof ApiError && (ex.status === 404 || ex.status === 405)) {
      goto(3)
      return
    }
    toast((ex instanceof Error && ex.message) || 'Could not save storage config', 'error')
  }
}

// -- Step 3: models --------------------------------------------------------
const modelsPanel = ref<InstanceType<typeof JobPanel> | null>(null)
const modelsReady = ref(false)
const modelsMissing = ref<string[]>([])
const showContinueAnyway = ref(false)
const downloadBusy = ref(false)

function showMissingModels(missing: string[]): void {
  modelsMissing.value = missing || []
  // A partial/failed download must never dead-end: offer a way forward.
  showContinueAnyway.value = true
}

// Reflect the authoritative on-disk status into the models step's controls.
function applyModelsStatus(s: SetupStatus): void {
  if (s.models_ready) {
    modelsReady.value = true
    modelsMissing.value = []
    showContinueAnyway.value = false
  } else if (s.models_missing && s.models_missing.length) {
    showMissingModels(s.models_missing)
  }
}

async function downloadModels(): Promise<void> {
  downloadBusy.value = true
  try {
    await modelsPanel.value?.start('models-download', {})
  } catch (ex) {
    toast((ex instanceof Error && ex.message) || 'Could not start download', 'error')
    downloadBusy.value = false
  }
}

function onModelsDone(detail: DoneDetail): void {
  const st = terminalState(detail)
  if (st === null) return // advisory result event — ignore
  // Any terminal state: re-fetch the real on-disk status. The download job
  // exits non-zero when AdaFace/MagFace aren't exportable, but the other models
  // may still be present — disk presence, not job exit code, decides.
  getJSON<SetupStatus>('/api/setup/status')
    .then((s) => {
      status.value = s
      if (s.models_ready) {
        modelsReady.value = true
        modelsMissing.value = []
        showContinueAnyway.value = false
        toast('Models ready', 'ok')
      } else {
        showMissingModels(s.models_missing)
      }
    })
    .catch(() => {
      // Status unreachable — fall back to the job state so we never trap.
      if (st === 'succeeded') modelsReady.value = true
      else showContinueAnyway.value = true
    })
  // Re-enable the Download button so the user can retry after a failed run.
  downloadBusy.value = false
}

// -- Step 4: first sync (skippable) ---------------------------------------
const syncPanel = ref<InstanceType<typeof JobPanel> | null>(null)
const syncBusy = ref(false)
const syncNext = ref(false)

async function runSync(): Promise<void> {
  syncBusy.value = true
  try {
    await syncPanel.value?.start('sync', {})
  } catch (ex) {
    toast((ex instanceof Error && ex.message) || 'Could not start sync', 'error')
    syncBusy.value = false
  }
}

function onSyncDone(detail: DoneDetail): void {
  const st = terminalState(detail)
  if (st === null) return
  if (st === 'succeeded') {
    syncNext.value = true
    toast('Sync complete', 'ok')
  }
}

// -- init ------------------------------------------------------------------
function prefill(s: SetupStatus): void {
  if (s.nas.url) nas.url = s.nas.url
  if (s.nas.account) nas.account = s.nas.account
  nas.verifyTls = s.nas.verify_tls !== false
  const spaces = s.nas.spaces && s.nas.spaces.length ? s.nas.spaces : ['personal']
  nas.spacePersonal = spaces.indexOf('personal') !== -1
  nas.spaceShared = spaces.indexOf('shared') !== -1
  if (s.storage.data_dir) storage.dataDir = s.storage.data_dir
  if (s.storage.models_dir) storage.modelsDir = s.storage.models_dir
  storage.keepOriginals = !!s.storage.keep_originals
  if (s.storage.originals_cache_gb) storage.cacheGb = s.storage.originals_cache_gb
}

onMounted(async () => {
  try {
    const s = await getJSON<SetupStatus>('/api/setup/status')
    status.value = s
    prefill(s)
    applyModelsStatus(s)
    goto(resumeStep(s))
  } catch {
    // No status (e.g. not yet reachable) — start at the top.
    goto(0)
  }
})
</script>

<template>
  <main class="auth-body">
    <div
      ref="wizardEl"
      class="auth-card auth-card-wide wizard"
      role="main"
      aria-labelledby="wizard-heading"
    >
      <div class="brand brand-lg">
        <img class="brand-mark" src="/img/logo.svg" alt="" aria-hidden="true" />
        <span class="brand-name">Synopticon</span>
      </div>
      <h1 class="auth-title" id="wizard-heading">Setup wizard</h1>

      <ol class="wizard-steps" aria-label="Setup progress">
        <li
          v-for="(s, i) in STEP_LABELS"
          :key="i"
          :data-state="dotState(i) || undefined"
        >
          <span class="wizard-dot">{{ s.mark }}</span><span>{{ s.label }}</span>
        </li>
      </ol>

      <!-- Step 0: create admin account (skipped when one already exists) -->
      <section
        class="wizard-step"
        :class="{ active: current === 0 }"
        aria-label="Create admin account"
      >
        <p class="step-title">Create your admin account</p>
        <p class="muted">Synopticon needs one admin account before it can do anything else.</p>
        <p v-if="accountError" class="auth-error" role="alert">{{ accountError }}</p>
        <form class="auth-form" @submit.prevent="createAccount">
          <div class="field">
            <label for="su-username">Username</label>
            <input
              type="text"
              id="su-username"
              v-model="account.username"
              autocomplete="username"
              required
            />
          </div>
          <div class="field">
            <label for="su-password">Password</label>
            <input
              type="password"
              id="su-password"
              v-model="account.password"
              autocomplete="new-password"
              required
              minlength="8"
            />
          </div>
          <button type="submit" class="btn btn-action btn-block" :disabled="accountBusy">
            Create account &amp; continue
          </button>
        </form>
      </section>

      <!-- Step 1: NAS details + test connection -->
      <section
        class="wizard-step"
        :class="{ active: current === 1 }"
        aria-label="NAS connection"
      >
        <p class="step-title">Connect to your NAS</p>
        <p class="muted">Read-only. Testing the connection never changes anything on the NAS.</p>
        <div class="field">
          <label for="nas-url">NAS URL</label>
          <input
            type="text"
            id="nas-url"
            v-model="nas.url"
            placeholder="https://nas.local:5001"
            autocomplete="off"
          />
        </div>
        <div class="field-row">
          <div class="field">
            <label for="nas-account">Account</label>
            <input type="text" id="nas-account" v-model="nas.account" autocomplete="off" />
          </div>
          <div class="field">
            <label for="nas-password">Password</label>
            <input
              type="password"
              id="nas-password"
              v-model="nas.password"
              autocomplete="off"
            />
          </div>
        </div>
        <div class="field">
          <label for="nas-otp">2FA code (optional)</label>
          <input
            type="text"
            id="nas-otp"
            v-model="nas.otp"
            inputmode="numeric"
            autocomplete="off"
            placeholder="only if 2FA is enabled"
          />
        </div>
        <div class="field field-inline">
          <input type="checkbox" id="nas-verify-tls" v-model="nas.verifyTls" />
          <label for="nas-verify-tls">Verify TLS certificate (uncheck for self-signed)</label>
        </div>
        <div class="field">
          <span class="muted">Spaces to include</span>
          <div class="field-inline">
            <input type="checkbox" id="nas-space-personal" v-model="nas.spacePersonal" />
            <label for="nas-space-personal">Personal</label>
          </div>
          <div class="field-inline">
            <input type="checkbox" id="nas-space-shared" v-model="nas.spaceShared" />
            <label for="nas-space-shared">Shared</label>
          </div>
        </div>
        <button type="button" class="btn" :disabled="testBusy" @click="testConnection">
          {{ testBusy ? 'Testing…' : 'Test connection' }}
        </button>
        <ul v-if="probe" class="probe-steps">
          <li
            v-for="(step, i) in probe.steps"
            :key="i"
            :class="step.ok ? 'probe-ok' : 'probe-fail'"
          >
            <span class="probe-mark">{{ step.ok ? '✓' : '✗' }}</span>
            <span class="probe-name">{{ step.name }}</span>
            <span class="probe-detail">{{ step.detail }}</span>
          </li>
        </ul>
        <div class="wizard-actions">
          <button type="button" class="btn btn-ghost" @click="goto(current - 1)">Back</button>
          <div class="spacer"></div>
          <button
            type="button"
            class="btn btn-action"
            :disabled="!nasSaveEnabled"
            @click="saveNas"
          >
            Save &amp; continue
          </button>
        </div>
      </section>

      <!-- Step 2: storage -->
      <section
        class="wizard-step"
        :class="{ active: current === 2 }"
        aria-label="Storage"
      >
        <p class="step-title">Storage locations</p>
        <p class="muted">Where Synopticon keeps its database, crops and downloaded originals.</p>
        <div class="field">
          <label for="st-data-dir">Data directory</label>
          <input type="text" id="st-data-dir" v-model="storage.dataDir" />
        </div>
        <div class="field">
          <label for="st-models-dir">Models directory</label>
          <input type="text" id="st-models-dir" v-model="storage.modelsDir" />
        </div>
        <div class="field-inline">
          <input type="checkbox" id="st-keep-originals" v-model="storage.keepOriginals" />
          <label for="st-keep-originals">Keep downloaded originals on disk</label>
        </div>
        <div class="field">
          <label for="st-cache-gb">Originals cache (GB)</label>
          <input type="number" id="st-cache-gb" min="1" step="1" v-model="storage.cacheGb" />
        </div>
        <button type="button" class="btn" @click="checkStorage">Check writability</button>
        <div v-if="storageResult" class="storage-result">
          <div
            v-for="(d, key) in storageResult.dirs"
            :key="key"
            :class="d.ok ? 'ok' : 'bad'"
          >
            {{ (d.ok ? '✓ ' : '✗ ') + key + ': ' + d.detail
            }}<template v-if="d.free_gb != null"> ({{ d.free_gb }} GB free)</template>
          </div>
        </div>
        <div class="wizard-actions">
          <button type="button" class="btn btn-ghost" @click="goto(current - 1)">Back</button>
          <div class="spacer"></div>
          <button type="button" class="btn btn-action" @click="saveStorage">
            Save &amp; continue
          </button>
        </div>
      </section>

      <!-- Step 3: model download -->
      <section
        class="wizard-step"
        :class="{ active: current === 3 }"
        aria-label="Model download"
      >
        <p class="step-title">Download face models</p>
        <p class="muted">Detection and embedding weights (a few hundred MB). This can take a while.</p>
        <JobPanel ref="modelsPanel" @done="onModelsDone" />
        <div v-if="modelsMissing.length" class="models-missing" role="alert">
          <p class="models-missing-title">Some models could not be downloaded</p>
          <p>
            AdaFace and MagFace cannot be downloaded automatically — their weights are not
            redistributed. Export them manually (see <strong>README &rarr; Models</strong>), then
            re-run the download, or continue and add them before detecting faces.
          </p>
          <p class="muted">
            First sync works without any models; detecting faces later requires all five.
          </p>
          <ul>
            <li v-for="key in modelsMissing" :key="key"><code>{{ key }}</code></li>
          </ul>
        </div>
        <div class="wizard-actions">
          <button type="button" class="btn btn-ghost" @click="goto(current - 1)">Back</button>
          <button type="button" class="btn" :disabled="downloadBusy" @click="downloadModels">
            Download models
          </button>
          <div class="spacer"></div>
          <button
            v-if="showContinueAnyway"
            type="button"
            class="btn btn-ghost"
            @click="goto(4)"
          >
            Continue anyway
          </button>
          <button type="button" class="btn btn-action" :disabled="!modelsReady" @click="goto(4)">
            Continue
          </button>
        </div>
      </section>

      <!-- Step 4: first sync (skippable) -->
      <section
        class="wizard-step"
        :class="{ active: current === 4 }"
        aria-label="First sync"
      >
        <p class="step-title">Run your first sync</p>
        <p class="muted">
          Pull the photo index from the NAS. You can skip this and run it later from the dashboard.
        </p>
        <JobPanel ref="syncPanel" @done="onSyncDone" />
        <div class="wizard-actions">
          <button type="button" class="btn btn-ghost" @click="goto(current - 1)">Back</button>
          <button type="button" class="btn" :disabled="syncBusy" @click="runSync">Run sync</button>
          <div class="spacer"></div>
          <button type="button" class="btn btn-ghost" @click="goto(LAST_STEP)">Skip</button>
          <button
            type="button"
            class="btn btn-action"
            :disabled="!syncNext"
            @click="goto(LAST_STEP)"
          >
            Continue
          </button>
        </div>
      </section>

      <!-- Step 5: done -->
      <section
        class="wizard-step"
        :class="{ active: current === 5 }"
        aria-label="Setup complete"
      >
        <p class="step-title">You&rsquo;re all set</p>
        <p class="muted">
          Synopticon is configured. Head to the dashboard to run the pipeline and review results.
        </p>
        <button type="button" class="btn btn-action btn-block" @click="router.push('/')">
          Go to dashboard
        </button>
      </section>
    </div>
  </main>
</template>

<style scoped>
/* Wizard chrome — page-scoped, kept out of the shared app.css per the plan. */
.wizard {
  max-width: 560px;
}
.wizard-steps {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-5);
  list-style: none;
  padding: 0;
  flex-wrap: wrap;
}
.wizard-steps li {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--text-3);
}
.wizard-dot {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  background: var(--bg-raised);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-2);
  flex: 0 0 auto;
}
.wizard-steps li[data-state='current'] {
  color: var(--text);
}
.wizard-steps li[data-state='current'] .wizard-dot {
  border-color: var(--action);
  color: var(--action);
  box-shadow: 0 0 0 3px var(--sel-tint);
}
.wizard-steps li[data-state='done'] .wizard-dot {
  background: var(--action);
  border-color: var(--action);
  color: #fff;
}
.wizard-step {
  display: none;
}
.wizard-step.active {
  display: block;
}
.step-title {
  font-size: var(--fs-lg);
  font-weight: 600;
  margin: 0 0 var(--sp-1);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: var(--sp-3);
}
.field label {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.field input[type='text'],
.field input[type='password'],
.field input[type='number'] {
  font: inherit;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-raised);
  color: var(--text);
}
.field-inline {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.field-inline label {
  color: var(--text);
}
.field-row {
  display: flex;
  gap: var(--sp-3);
}
.field-row .field {
  flex: 1;
}
.wizard-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
}
.wizard-actions .spacer {
  flex: 1;
}
.probe-steps {
  list-style: none;
  padding: 0;
  margin: var(--sp-3) 0 0;
}
.probe-steps li {
  display: flex;
  gap: var(--sp-2);
  align-items: baseline;
  padding: var(--sp-1) 0;
  font-size: var(--fs-base);
}
.probe-mark {
  font-weight: 700;
  flex: 0 0 auto;
}
.probe-ok .probe-mark {
  color: var(--ok);
}
.probe-fail .probe-mark {
  color: var(--danger);
}
.probe-name {
  font-weight: 600;
  min-width: 5.5em;
}
.probe-detail {
  color: var(--text-2);
}
.storage-result {
  margin-top: var(--sp-3);
  font-size: var(--fs-base);
}
.storage-result .ok {
  color: var(--ok);
}
.storage-result .bad {
  color: var(--danger);
}
.models-missing {
  margin-top: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--warn);
  border-radius: var(--radius);
  background: var(--bg-sunken);
  font-size: var(--fs-base);
}
.models-missing-title {
  font-weight: 600;
  margin: 0 0 var(--sp-1);
  color: var(--warn);
}
.models-missing p {
  margin: 0 0 var(--sp-2);
}
.models-missing ul {
  margin: 0;
  padding-left: var(--sp-4);
}
.models-missing code {
  font-family: ui-monospace, monospace;
}
</style>
