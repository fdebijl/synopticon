<script setup lang="ts">
// Full-access API keys for automation. A newly created key's secret comes back
// exactly once (the server only stores its hash), so it stays on screen until
// the next create; revoking gates on a confirm.
import { onMounted, ref } from 'vue'
import { getJSON, postJSON, ApiError } from '../../../api/client'
import type { ApiKey } from '../../../api/types'
import { toast } from '../../../stores/toasts'
import { confirm } from '../../../composables/useConfirm'

const keys = ref<ApiKey[]>([])
const keyName = ref('')
const revealedKey = ref('')

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

async function load(): Promise<void> {
  try {
    const data = await getJSON<{ keys: ApiKey[] }>('/api/auth/keys')
    keys.value = data.keys || []
  } catch {
    /* transient — leave the current list */
  }
}

async function createKey(): Promise<void> {
  const name = keyName.value.trim()
  if (!name) return
  try {
    const res = await postJSON<{ key: string; name: string }>('/api/auth/keys', { name })
    keyName.value = ''
    revealedKey.value = res.key
    void load()
  } catch (err) {
    toast(errMsg(err, 'Could not create key'), 'error')
  }
}

async function revokeKey(id: number, name: string): Promise<void> {
  const ok = await confirm({
    title: 'Revoke API key',
    message: `Revoke "${name}"? Any client using it will stop working.`,
    okLabel: 'Revoke',
  })
  if (!ok) return
  try {
    await postJSON(`/api/auth/keys/${id}/revoke`, {})
    toast('Key revoked', 'ok')
    void load()
  } catch (err) {
    toast(errMsg(err, 'Revoke failed'), 'error')
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="card keys-section">
    <h3>API keys</h3>
    <p class="muted">
      Full-access keys for automation (e.g. a sidecar extension). The secret is shown once.
    </p>
    <form class="key-form" @submit.prevent="createKey">
      <label style="flex: 1"
        >Key name
        <input class="input" type="text" placeholder="my-laptop" required v-model="keyName" />
      </label>
      <button class="btn btn-action" type="submit">Create key</button>
    </form>
    <div v-if="revealedKey" class="key-reveal">
      New key (copy it now — it will not be shown again):<br />{{ revealedKey }}
    </div>
    <table class="data key-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Prefix</th>
          <th>Created</th>
          <th>Last used</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="k in keys" :key="k.id" :class="{ revoked: k.revoked }">
          <td>{{ k.name }}</td>
          <td class="mono">{{ k.key_prefix }}…</td>
          <td>{{ fmtTime(k.created_at) }}</td>
          <td>{{ fmtTime(k.last_used_at) }}</td>
          <td>
            <button
              v-if="!k.revoked"
              class="btn btn-sm btn-danger"
              type="button"
              @click="revokeKey(k.id, k.name)"
            >
              Revoke
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.keys-section {
  max-width: 640px;
}
.key-form {
  display: flex;
  flex-direction: row;
  align-items: flex-end;
  gap: var(--sp-2);
  max-width: 380px;
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.key-table {
  width: 100%;
  margin-top: var(--sp-3);
}
.key-table tr.revoked td {
  opacity: 0.5;
}
.key-reveal {
  margin-top: var(--sp-2);
  font-family: ui-monospace, monospace;
  background: var(--bg-sunken);
  border: 1px solid var(--action);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  word-break: break-all;
}
</style>
