<script setup lang="ts">
// The primary UI for network access (SEC2). The Security tab's `allow_from` /
// `allow_private_networks` / `trusted_proxies` fields (rendered by SchemaForm)
// are the raw escape hatch; this card is the diagnostic that tells an operator
// whether flipping them is even safe from where they are sitting right now.
//
// Reads GET /api/security/access (route 17, in-memory + one-scope, no
// connection) for "this browser" and the proxy-trust hazard readout, and GET
// /api/config for the saved (on-disk) security values so the "not in effect
// until restart" line can compare them against what the running middleware is
// actually using. Nothing here decides anything -- every field is a diagnostic
// (clientip.py RULE 2) and the banners below are ordered by how bad the
// misconfiguration they describe actually is, not by when they were added.
import { computed, onMounted, ref } from 'vue'
import { getJSON, ApiError } from '../../../api/client'

interface AllowlistInfo {
  active: boolean
  entries: string[]
  allow_private: boolean
  loopback_always: boolean
  adds_nothing: boolean
}

interface AccessInfo {
  client_ip: string
  effective_source: 'forwarded' | 'socket_peer'
  peer: string
  allowed: boolean
  allowlist: AllowlistInfo
  effective: { summary: string; list_adds_nothing: boolean }
  proxy: {
    trusted_proxies: string[]
    peer_is_trusted_proxy: boolean
    forwarded_for_present: boolean
    forwarded_for_raw: string
    trusts_loopback: boolean
    shared_bucket: boolean
  }
  in_effect: {
    allow_from: string[]
    allow_private_networks: boolean
    trusted_proxies: string[]
  }
}

interface SecuritySaved {
  allow_from?: string[]
  allow_private_networks?: boolean
  trusted_proxies?: string[]
}

const access = ref<AccessInfo | null>(null)
const saved = ref<SecuritySaved | null>(null)
const loadError = ref('')
const loading = ref(false)

function sameList(a: string[] | undefined, b: string[] | undefined): boolean {
  const x = a || []
  const y = b || []
  return x.length === y.length && x.every((v, i) => v === y[i])
}

const savedDiffersFromRunning = computed(() => {
  if (!access.value || !saved.value) return false
  const s = saved.value
  const eff = access.value.in_effect
  return (
    !sameList(s.allow_from, eff.allow_from) ||
    !!s.allow_private_networks !== eff.allow_private_networks ||
    !sameList(s.trusted_proxies, eff.trusted_proxies)
  )
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [a, cfg] = await Promise.all([
      getJSON<AccessInfo>('/api/security/access'),
      getJSON<{ values: { security?: SecuritySaved } }>('/api/config'),
    ])
    access.value = a
    saved.value = cfg.values?.security || null
  } catch (err) {
    loadError.value = err instanceof ApiError ? err.message : 'Could not load access status'
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="card access-section network-access">
    <h3>Network access</h3>

    <p v-if="loading && !access" class="muted">Checking…</p>
    <p v-else-if="loadError" class="muted error-text">{{ loadError }}</p>

    <template v-else-if="access">
      <div v-if="access.proxy.trusts_loopback" class="banner banner-danger">
        Synopticon trusts the X-Forwarded-For header on connections from this
        machine. Your reverse proxy MUST overwrite that header —
        <code>proxy_set_header X-Forwarded-For $remote_addr;</code> for nginx
        (Caddy and Traefik do it by default). A proxy that passes the
        visitor's own header through instead — which is what a bare
        <code>proxy_pass</code> does — lets any visitor claim any address,
        which switches off the address list and the per-address sign-in
        limits for whoever asks. Synopticon cannot tell the difference from
        here; check your proxy config.
      </div>

      <div
        v-else-if="
          access.proxy.forwarded_for_present &&
          !access.proxy.peer_is_trusted_proxy &&
          access.allowlist.active
        "
        class="banner banner-danger"
      >
        This request reached Synopticon through a proxy that is not listed in
        trusted_proxies, so every visitor looks like the same address and the
        list below is not restricting anyone.
      </div>

      <div v-else-if="access.proxy.shared_bucket" class="banner banner-warn">
        Your reverse proxy runs on this machine and is not sending a visitor
        address, so everyone — including strangers — arrives as 127.0.0.1. The
        address list cannot restrict anyone and per-address sign-in limits
        count all your visitors as one. Configure the proxy to overwrite
        X-Forwarded-For.
      </div>

      <div v-if="access.effective.list_adds_nothing" class="banner banner-warn">
        Every address in your list is already covered by "Always allow local
        networks" — this list is not restricting anything yet.
      </div>

      <div v-if="savedDiffersFromRunning" class="banner banner-info">
        Saved. Not in effect until the web process restarts — Settings shows
        what is on disk, this panel shows what is running.
      </div>

      <p class="summary">{{ access.effective.summary }}</p>

      <table class="data browser-table">
        <tbody>
          <tr>
            <th>This browser</th>
            <td class="mono">{{ access.client_ip }}</td>
          </tr>
          <tr>
            <th>Reached through</th>
            <td>
              {{ access.effective_source === 'forwarded' ? 'a trusted proxy' : 'a direct connection' }}
            </td>
          </tr>
          <tr>
            <th>Connection peer</th>
            <td class="mono">{{ access.peer }}</td>
          </tr>
          <tr v-if="access.proxy.forwarded_for_present">
            <th>X-Forwarded-For seen</th>
            <td class="mono">{{ access.proxy.forwarded_for_raw }}</td>
          </tr>
        </tbody>
      </table>

      <p class="muted small">
        Change the address list and "Always allow local networks" on the
        Security tab, and <code>trusted_proxies</code> there too if you run a
        reverse proxy. Locked out? Run
        <code>synopticon web-access --clear</code> on the server, then restart
        Synopticon.
      </p>

      <button class="btn btn-sm" type="button" :disabled="loading" @click="load">
        Refresh
      </button>
    </template>
  </div>
</template>

<style scoped>
.network-access {
  max-width: 640px;
}
.summary {
  margin: var(--sp-2) 0;
}
.banner {
  border-left: 3px solid var(--action);
  border-radius: var(--radius);
  background: var(--bg-sunken);
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1.5;
}
.banner-danger {
  border-left-color: var(--danger);
}
.banner-warn {
  border-left-color: var(--warn);
}
.banner-info {
  border-left-color: var(--action);
}
.banner code {
  font-family: ui-monospace, monospace;
  font-size: 0.9em;
}
.browser-table {
  margin: var(--sp-3) 0;
  width: auto;
}
.browser-table th {
  text-align: left;
  padding: var(--sp-1) var(--sp-3) var(--sp-1) 0;
  color: var(--text-2);
  font-weight: normal;
  white-space: nowrap;
}
.browser-table td {
  padding: var(--sp-1) 0;
}
.small {
  font-size: var(--fs-sm);
}
.error-text {
  color: var(--danger);
}
</style>
