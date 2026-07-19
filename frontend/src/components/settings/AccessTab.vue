<script setup lang="ts">
// Access tab: change the signed-in user's password and manage full-access API
// keys. Ports settings.js's Access panel. A newly created key's secret is shown
// exactly once (the server only stores its hash); revoking gates on a confirm.
import { onMounted, reactive, ref } from 'vue'
import { getJSON, postJSON, ApiError } from '../../api/client'
import type { ApiKey } from '../../api/types'
import { toast } from '../../stores/toasts'
import { confirm } from '../../composables/useConfirm'

const pw = reactive({ current: '', next: '', confirm: '' })
const keys = ref<ApiKey[]>([])
const keyName = ref('')
const revealedKey = ref('')

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

async function loadKeys(): Promise<void> {
  try {
    const data = await getJSON<{ keys: ApiKey[] }>('/api/auth/keys')
    keys.value = data.keys || []
  } catch {
    /* transient — leave the current list */
  }
}

async function changePassword(): Promise<void> {
  if (pw.next !== pw.confirm) {
    toast('New passwords do not match', 'error')
    return
  }
  try {
    await postJSON('/api/auth/change-password', {
      current_password: pw.current,
      new_password: pw.next,
    })
    pw.current = pw.next = pw.confirm = ''
    toast('Password updated', 'ok')
  } catch (err) {
    toast(errMsg(err, 'Could not change password'), 'error')
  }
}

async function createKey(): Promise<void> {
  const name = keyName.value.trim()
  if (!name) return
  try {
    const res = await postJSON<{ key: string; name: string }>('/api/auth/keys', { name })
    keyName.value = ''
    revealedKey.value = res.key
    void loadKeys()
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
    void loadKeys()
  } catch (err) {
    toast(errMsg(err, 'Revoke failed'), 'error')
  }
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

onMounted(() => void loadKeys())
</script>

<template>
  <div class="settings-panel">
    <div class="card access-section">
      <h3>Change password</h3>
      <form class="access-form" @submit.prevent="changePassword">
        <label
          >Current password
          <input class="input" type="password" autocomplete="current-password" required v-model="pw.current" />
        </label>
        <label
          >New password
          <input class="input" type="password" autocomplete="new-password" required v-model="pw.next" />
        </label>
        <label
          >Confirm new password
          <input class="input" type="password" autocomplete="new-password" required v-model="pw.confirm" />
        </label>
        <button class="btn btn-primary" type="submit">Update password</button>
      </form>
    </div>

    <div class="card access-section">
      <h3>API keys</h3>
      <p class="muted">
        Full-access keys for automation (e.g. a sidecar extension). The secret is shown once.
      </p>
      <form class="access-form key-form" @submit.prevent="createKey">
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
  </div>
</template>

<style scoped>
.access-section {
  margin-bottom: var(--sp-5);
  max-width: 640px;
}
.access-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 380px;
  margin-top: var(--sp-3);
}
.access-form label {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.access-form.key-form {
  flex-direction: row;
  align-items: flex-end;
  gap: var(--sp-2);
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
