<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { getJSON, postJSON, ApiError } from '../../../api/client'
import { toast } from '../../../stores/toasts'

interface Observed {
  user_agent: string
  device_key: string
  ip: string
  network: string
}

interface PinningInfo {
  mode: string
  modes: string[]
  session_pinned: boolean
  other_sessions: number
  observed: Observed
}

interface Me {
  totp_enabled: boolean | null
}

const MODE_LABELS: Record<string, string> = {
  off: 'Off',
  device: 'This device',
  'device+network': 'This device and network',
}

const info = ref<PinningInfo | null>(null)
const totpEnabled = ref(false)
const selectedMode = ref('off')
const form = reactive({ password: '', code: '' })
const saving = ref(false)
const loadFailed = ref(false)

function modeLabel(mode: string): string {
  return MODE_LABELS[mode] || mode
}

async function load(): Promise<void> {
  try {
    const [pinning, me] = await Promise.all([
      getJSON<PinningInfo>('/api/auth/session-pinning'),
      getJSON<Me>('/api/auth/me'),
    ])
    info.value = pinning
    selectedMode.value = pinning.mode
    totpEnabled.value = !!me.totp_enabled
    loadFailed.value = false
  } catch {
    loadFailed.value = true
  }
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

async function save(): Promise<void> {
  if (!form.password) {
    toast('Enter your password to confirm this change', 'error')
    return
  }
  saving.value = true
  try {
    const body: Record<string, string> = { mode: selectedMode.value, password: form.password }
    if (form.code) body.code = form.code
    const res = await postJSON<{ ok: true; mode: string; signed_out_others: number }>(
      '/api/auth/session-pinning',
      body,
    )
    form.password = ''
    form.code = ''
    const others =
      res.signed_out_others > 0
        ? ` — ${res.signed_out_others} other session(s) signed out`
        : ''
    toast(`Session pinning set to "${modeLabel(res.mode)}"${others}`, 'ok')
    await load()
  } catch (err) {
    toast(errMsg(err, 'Could not update session pinning'), 'error')
  } finally {
    saving.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="card pinning-section">
    <h3>Session pinning</h3>
    <p class="muted">
      Lock down your current session to the browser (and, optionally, the network) that requested it, so a copied/stolen cookie stops working anywhere else.<br><br>
      Evaluate if session hijacking is part of your threat model - for must users, two-factor authentication will provide ample protection.
      When first enabled, session pinning will sign out all other sessions for your account.
    </p>

    <template v-if="info">
      <dl class="observed">
        <dt>This browser</dt>
        <dd>{{ info.observed.user_agent || '—' }} <span class="mono">({{ info.observed.device_key }})</span></dd>
        <dt>This address</dt>
        <dd>{{ info.observed.ip }} <span class="mono">({{ info.observed.network }})</span></dd>
        <dt>Other signed-in sessions</dt>
        <dd>{{ info.other_sessions }}</dd>
      </dl>

      <form class="pinning-form" @submit.prevent="save">
        <label>
          Pin sessions to
          <select class="input" v-model="selectedMode">
            <option v-for="m in info.modes" :key="m" :value="m">{{ modeLabel(m) }}</option>
          </select>
        </label>
        <p v-if="selectedMode === 'device+network'" class="warning">
          Not recommended on mobile or IPv4+IPv6 connections, you will be signed out whenever the network changes.
        </p>

        <label>
          Password
          <input class="input" type="password" autocomplete="current-password" v-model="form.password" required />
        </label>
        <label v-if="totpEnabled">
          Two-step code (or a backup code)
          <input class="input" type="text" inputmode="numeric" autocomplete="one-time-code" v-model="form.code" />
        </label>

        <button class="btn btn-primary" type="submit" :disabled="saving">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
      </form>

      <p class="muted recovery">
        Signed out unexpectedly because your browser or network changed too often? Run
        <code>synopticon session-pin off</code> on the server to turn pinning back off for your
        account without needing to sign in first.
      </p>
    </template>
    <p v-else-if="loadFailed" class="muted">Could not load session pinning settings.</p>
  </div>
</template>

<style scoped>
.pinning-section {
  max-width: 640px;
}
.observed {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--sp-1) var(--sp-3);
  font-size: var(--fs-sm);
  margin: var(--sp-3) 0;
}
.observed dt {
  color: var(--text-2);
}
.observed dd {
  margin: 0;
}
.pinning-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 380px;
  margin-top: var(--sp-3);
}
.pinning-form label {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.warning {
  color: var(--danger);
  font-size: var(--fs-sm);
  margin: 0;
}
.recovery {
  margin-top: var(--sp-3);
}
.recovery code {
  font-family: ui-monospace, monospace;
}
</style>
