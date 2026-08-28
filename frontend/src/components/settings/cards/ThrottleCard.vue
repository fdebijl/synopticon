<script setup lang="ts">
// Login throttling (SEC5): a live readout of route 19's limiter.snapshot(),
// with a per-row "Clear" wired to route 20. Two tiers only, both always
// armed on every address -- there is no global tier and no exempt address
// (D5, D6), which is why the card says so in its own copy rather than
// letting an operator infer a carve-out that does not exist.
import { onMounted, ref } from 'vue'
import { getJSON, postJSON, ApiError } from '../../../api/client'
import { toast } from '../../../stores/toasts'

interface ThrottlePairRow {
  scope: string
  ip: string
  username: string
  failures: number
  attempts: number
  locked_for: number
  forget_in: number
}

interface ThrottleAddressRow {
  ip: string
  failures_in_window: number
  blocked_for: number
}

interface ThrottleSnapshot {
  pairs: ThrottlePairRow[]
  ips: ThrottleAddressRow[]
  tracked: number
  max_tracked: number
  thresholds: { pair_max_attempts: number; ip_max_failures: number }
}

const SCOPE_LABELS: Record<string, string> = {
  password: 'Password',
  totp: 'Two-step code',
  change_password: 'Change password',
  reauth: 'Re-authentication',
}

const snap = ref<ThrottleSnapshot | null>(null)
const loadFailed = ref(false)
const clearing = ref('') // "" | "pair:<scope>:<ip>:<username>" | "ip:<ip>"

function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope] || scope
}

function fmtDuration(seconds: number): string {
  if (seconds <= 0) return '—'
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return s ? `${m}m ${s}s` : `${m}m`
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

async function load(): Promise<void> {
  try {
    snap.value = await getJSON<ThrottleSnapshot>('/api/security/throttles')
    loadFailed.value = false
  } catch {
    loadFailed.value = true
  }
}

async function clear(body: { ip: string } | { username: string }, token: string): Promise<void> {
  clearing.value = token
  try {
    const res = await postJSON<{ ok: true; cleared: number }>('/api/security/throttles/clear', body)
    toast(res.cleared > 0 ? `Cleared ${res.cleared} throttle entr${res.cleared === 1 ? 'y' : 'ies'}` : 'Nothing to clear', 'ok')
    await load()
  } catch (err) {
    toast(errMsg(err, 'Could not clear the throttle'), 'error')
  } finally {
    clearing.value = ''
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="card throttle-section">
    <h3>Sign-in throttling</h3>
    <p class="muted">
      Failed and half-finished sign-ins are throttled per account and per address. This applies to
      every address that reaches Synopticon, including this machine — there is no exempt address
      and no way to turn it off short of the limits below.
    </p>

    <template v-if="snap">
      <p class="muted counts">
        Tracking {{ snap.tracked }} of {{ snap.max_tracked }} entries · account limit
        {{ snap.thresholds.pair_max_attempts }} attempts · address limit
        {{ snap.thresholds.ip_max_failures }} failures
      </p>

      <h4>Blocked accounts</h4>
      <p v-if="!snap.pairs.length" class="muted">No account is currently blocked.</p>
      <div v-else class="table-scroll">
        <table class="data">
          <thead>
            <tr>
              <th>Scope</th>
              <th>Address</th>
              <th>Username</th>
              <th>Failures</th>
              <th>Locked for</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in snap.pairs" :key="`${p.scope}:${p.ip}:${p.username}`">
              <td>{{ scopeLabel(p.scope) }}</td>
              <td class="mono">{{ p.ip }}</td>
              <td>{{ p.username }}</td>
              <td>{{ p.failures }} / {{ p.attempts }}</td>
              <td>{{ fmtDuration(p.locked_for) }}</td>
              <td>
                <button
                  class="btn btn-sm btn-ghost"
                  type="button"
                  :disabled="clearing !== ''"
                  @click="clear({ username: p.username }, `pair:${p.username}`)"
                >
                  {{ clearing === `pair:${p.username}` ? 'Clearing…' : 'Clear account' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4>Blocked addresses</h4>
      <p v-if="!snap.ips.length" class="muted">No address is currently blocked.</p>
      <div v-else class="table-scroll">
        <table class="data">
          <thead>
            <tr>
              <th>Address</th>
              <th>Failures in window</th>
              <th>Blocked for</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in snap.ips" :key="row.ip">
              <td class="mono">{{ row.ip }}</td>
              <td>{{ row.failures_in_window }}</td>
              <td>{{ fmtDuration(row.blocked_for) }}</td>
              <td>
                <button
                  class="btn btn-sm btn-ghost"
                  type="button"
                  :disabled="clearing !== ''"
                  @click="clear({ ip: row.ip }, `ip:${row.ip}`)"
                >
                  {{ clearing === `ip:${row.ip}` ? 'Clearing…' : 'Clear address' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="muted recovery">
        Locked out of your own address? Set <code>max_failures_per_address = 0</code> under
        <code>[security]</code> in config.toml and restart Synopticon, or clear the entry above.
      </p>
    </template>
    <p v-else-if="loadFailed" class="muted">Could not load sign-in throttle status.</p>
  </div>
</template>

<style scoped>
.throttle-section {
  max-width: 720px;
}
.throttle-section h4 {
  margin: var(--sp-4) 0 var(--sp-1);
  font-size: var(--fs-base);
}
.counts {
  margin-top: var(--sp-2);
}
.table-scroll {
  overflow-x: auto;
}
.recovery {
  margin-top: var(--sp-3);
}
.recovery code {
  font-family: ui-monospace, monospace;
}
</style>
