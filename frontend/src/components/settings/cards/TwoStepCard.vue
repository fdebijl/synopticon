<script setup lang="ts">
// Two-step sign-in (SEC1): enrol, confirm, regenerate backup codes, disable.
//
// There is no server-side QR (D1): route 11 hands back the otpauth:// URI and
// the base32 secret, and this component draws the symbol itself with `uqr`.
// `renderSVG` returns a plain string, wrapped into a data URI and bound to an
// <img> rather than injected with v-html -- an <img> needs no HTML injection
// surface at all.
import { computed, onMounted, reactive, ref } from 'vue'
import { renderSVG } from 'uqr'
import { getJSON, postJSON, ApiError } from '../../../api/client'
import { toast } from '../../../stores/toasts'

interface TotpStatus {
  enrolled: boolean
  pending: boolean
  confirmed_at: number | null
  recovery_remaining: number
  recovery_generated_at: number | null
  pending_expires_in: number | null
}

interface TotpStartResponse {
  secret: string
  secret_groups: string
  otpauth_uri: string
  pending_expires_in: number
  manual: { issuer: string; account: string; algorithm: string; digits: number; period: number }
}

const status = ref<TotpStatus | null>(null)
const pending = ref<TotpStartResponse | null>(null)
const qrSrc = ref<string | null>(null)
const qrFailed = ref(false)

const startPassword = ref('')
const confirmCode = ref('')
const recoveryCodes = ref<string[] | null>(null)

const showDisableForm = ref(false)
const disableForm = reactive({ password: '', code: '' })

const showRegenForm = ref(false)
const regenForm = reactive({ password: '', code: '' })

const busy = ref(false)

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 429) {
      const retry = (err.body as { retry_after?: number } | null)?.retry_after
      return retry
        ? `Too many attempts — try again in ${retry}s.`
        : err.message || 'Too many attempts — try again shortly.'
    }
    return err.message || fallback
  }
  return (err as Error)?.message || fallback
}

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

const minutesLeft = computed(() => {
  const secs = status.value?.pending_expires_in ?? pending.value?.pending_expires_in
  return secs != null ? Math.max(1, Math.ceil(secs / 60)) : null
})

function renderQr(uri: string): void {
  try {
    const svg = renderSVG(uri, { ecc: 'M', border: 4, pixelSize: 6 })
    // btoa only accepts Latin1; the URI-encode/unescape round trip is the
    // standard way to base64 a UTF-8 string in the browser.
    qrSrc.value = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(svg)))}`
    qrFailed.value = false
  } catch {
    qrSrc.value = null
    qrFailed.value = true
  }
}

async function loadStatus(): Promise<void> {
  try {
    status.value = await getJSON<TotpStatus>('/api/auth/totp')
  } catch {
    /* transient — leave the current view */
  }
}

async function startEnrolment(fresh: boolean): Promise<void> {
  if (!startPassword.value) {
    toast('Enter your password to continue', 'error')
    return
  }
  busy.value = true
  try {
    const url = fresh ? '/api/auth/totp/start?fresh=1' : '/api/auth/totp/start'
    const res = await postJSON<TotpStartResponse>(url, { password: startPassword.value })
    pending.value = res
    startPassword.value = ''
    confirmCode.value = ''
    renderQr(res.otpauth_uri)
  } catch (err) {
    toast(errMsg(err, 'Could not start setup'), 'error')
  } finally {
    busy.value = false
  }
}

async function confirmEnrolment(): Promise<void> {
  if (!confirmCode.value) return
  busy.value = true
  try {
    const res = await postJSON<{ ok: true; recovery_codes: string[] }>(
      '/api/auth/totp/confirm',
      { code: confirmCode.value },
    )
    recoveryCodes.value = res.recovery_codes
    pending.value = null
    qrSrc.value = null
    confirmCode.value = ''
    toast('Two-step sign-in turned on', 'ok')
    await loadStatus()
  } catch (err) {
    toast(errMsg(err, 'That code was not accepted'), 'error')
  } finally {
    busy.value = false
  }
}

async function submitDisable(): Promise<void> {
  busy.value = true
  try {
    await postJSON('/api/auth/totp/disable', {
      password: disableForm.password,
      code: disableForm.code,
    })
    disableForm.password = ''
    disableForm.code = ''
    showDisableForm.value = false
    recoveryCodes.value = null
    toast('Two-step sign-in turned off', 'ok')
    await loadStatus()
  } catch (err) {
    toast(errMsg(err, 'Password or code was not accepted'), 'error')
  } finally {
    busy.value = false
  }
}

async function submitRegen(): Promise<void> {
  busy.value = true
  try {
    const res = await postJSON<{ codes: string[] }>('/api/auth/totp/recovery-codes', {
      password: regenForm.password,
      code: regenForm.code,
    })
    recoveryCodes.value = res.codes
    regenForm.password = ''
    regenForm.code = ''
    showRegenForm.value = false
    toast('New backup codes generated', 'ok')
    await loadStatus()
  } catch (err) {
    toast(errMsg(err, 'Password or code was not accepted'), 'error')
  } finally {
    busy.value = false
  }
}

async function copySecret(): Promise<void> {
  if (!pending.value) return
  try {
    await navigator.clipboard.writeText(pending.value.secret_groups)
    toast('Copied', 'ok')
  } catch {
    /* clipboard access denied — the text is already selectable */
  }
}

onMounted(async () => {
  await loadStatus()
  // A reload mid-enrolment: the card cannot show the QR again without the
  // password, but it can tell the user there is unfinished setup waiting.
})
</script>

<template>
  <div class="card two-step-card">
    <h3>Two-step sign-in</h3>

    <div v-if="status?.enrolled && !pending" class="two-step-status">
      <p class="muted">
        Turned on{{ status.confirmed_at ? ` on ${fmtTime(status.confirmed_at)}` : '' }}.
      </p>
      <p class="muted">
        Backup codes remaining: <strong>{{ status.recovery_remaining }}</strong>
        <template v-if="status.recovery_generated_at">
          (generated {{ fmtTime(status.recovery_generated_at) }})</template
        >
      </p>
      <p v-if="status.recovery_remaining <= 2" class="warn-line">
        Running low on backup codes — generate a fresh set before you need one.
      </p>

      <div class="two-step-actions">
        <button class="btn" type="button" @click="showRegenForm = !showRegenForm">
          Generate new backup codes
        </button>
        <button class="btn btn-danger" type="button" @click="showDisableForm = !showDisableForm">
          Turn off two-step sign-in
        </button>
      </div>

      <form v-if="showRegenForm" class="reauth-form" @submit.prevent="submitRegen">
        <label
          >Password
          <input class="input" type="password" autocomplete="current-password" required v-model="regenForm.password" />
        </label>
        <label
          >Authenticator code or a backup code
          <input class="input" type="text" autocomplete="one-time-code" required v-model="regenForm.code" />
        </label>
        <button class="btn btn-primary" type="submit" :disabled="busy">Generate codes</button>
      </form>

      <form v-if="showDisableForm" class="reauth-form" @submit.prevent="submitDisable">
        <label
          >Password
          <input class="input" type="password" autocomplete="current-password" required v-model="disableForm.password" />
        </label>
        <label
          >Authenticator code or a backup code
          <input class="input" type="text" autocomplete="one-time-code" required v-model="disableForm.code" />
        </label>
        <button class="btn btn-danger" type="submit" :disabled="busy">Turn off</button>
      </form>
    </div>

    <div v-else-if="!pending" class="two-step-enrol">
      <p class="muted">
        Require a code from an authenticator app (or a backup code) in addition to your password when signing in. Enter your password to get started.
      </p>
      <p v-if="status?.pending" class="muted">
        Setup in progress<template v-if="minutesLeft">, you have {{ minutesLeft }} minute{{ minutesLeft === 1 ? '' : 's' }} left</template>. Enter your password to continue.
      </p>
      <form class="reauth-form" @submit.prevent="startEnrolment(false)">
        <label
          >Password
          <input class="input" type="password" autocomplete="current-password" required v-model="startPassword" />
        </label>
        <div class="two-step-actions" v-if="status?.pending">
          <button class="btn btn-primary" type="submit" :disabled="busy">Show the code again</button>
          <button class="btn" type="button" :disabled="busy" @click="startEnrolment(true)">Start over</button>
        </div>
        <button v-else class="btn btn-primary" type="submit" :disabled="busy">
          Turn on two-step sign-in
        </button>
      </form>
    </div>

    <div v-else class="two-step-enrol">
      <p v-if="minutesLeft" class="muted">You have {{ minutesLeft }} minute{{ minutesLeft === 1 ? '' : 's' }} left to complete the two-step authentication setup.</p>

      <div class="enrol-grid">
        <div class="enrol-qr">
          <img v-if="qrSrc" :src="qrSrc" alt="Two-step sign-in setup code" />
          <p v-else class="muted">
            QR code generation failed, enter the key below by hand instead, or reload the page.
          </p>
        </div>
        <div class="enrol-manual">
          <p class="muted">Can't scan the code? Enter this key manually in your authenticator app:</p>
          <div class="secret-groups" @click="copySecret">{{ pending.secret_groups }}</div>
          <button class="btn btn-sm" type="button" @click="copySecret">Copy</button>
          <p class="muted manual-line">
            Use the following information for manual setup in your authenticator app<br>
            Account: {{ pending.manual.account }}<br>
            Issuer: {{ pending.manual.issuer }}<br>
            Type: time-based<br>
            {{ pending.manual.digits }} digits<br>
            {{ pending.manual.period }} seconds<br>
            {{ pending.manual.algorithm }}<br>
          </p>
        </div>
      </div>

      <form class="reauth-form" @submit.prevent="confirmEnrolment">
        <label>
          Enter the code generated by your authenticator app
          <input
            class="input"
            type="text"
            autocomplete="one-time-code"
            required
            v-model="confirmCode"
          />
        </label>
        <button class="btn btn-primary" type="submit" :disabled="busy">Confirm</button>
      </form>
    </div>

    <div v-if="recoveryCodes" class="recovery-reveal">
      <p><strong>Save these backup codes now, they will not be shown again.</strong></p>
      <p class="muted">Each one signs you in once, if you lose access to your authenticator.</p>
      <ul class="recovery-list">
        <li v-for="c in recoveryCodes" :key="c" class="mono">{{ c }}</li>
      </ul>
      <button class="btn btn-sm" type="button" @click="recoveryCodes = null">Done</button>
    </div>
  </div>
</template>

<style scoped>
.two-step-card {
  max-width: 640px;
}
.two-step-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
}
.reauth-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 380px;
  margin-top: var(--sp-3);
}
.reauth-form label {
  font-size: var(--fs-sm);
  color: var(--text-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.warn-line {
  color: var(--warn);
  font-size: var(--fs-sm);
}
.enrol-grid {
  display: flex;
  gap: var(--sp-4);
  flex-wrap: wrap;
  margin-top: var(--sp-3);
  align-items: flex-start;
}
.enrol-qr img {
  width: 240px;
  height: 240px;
  /* The QR needs fixed black-on-white contrast for scanners in every theme,
     so it gets an explicit light backdrop regardless of the app's own theme. */
  background: #fff;
  padding: var(--sp-2);
  border-radius: var(--radius);
}
.enrol-manual {
  flex: 1;
  min-width: 220px;
}
.secret-groups {
  font-family: ui-monospace, monospace;
  font-size: var(--fs-lg);
  user-select: all;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  margin: var(--sp-2) 0;
  cursor: text;
  word-break: break-all;
}
.manual-line {
  margin-top: var(--sp-2);
}
.recovery-reveal {
  margin-top: var(--sp-3);
  background: var(--bg-sunken);
  border: 1px solid var(--action);
  border-radius: var(--radius);
  padding: var(--sp-3);
}
.recovery-list {
  font-family: ui-monospace, monospace;
  columns: 2;
  margin: var(--sp-2) 0;
  padding-left: var(--sp-4);
}
</style>
