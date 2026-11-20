<script setup lang="ts">
// Scoped tokens for AI agents on /mcp. Like API keys, the secret comes back
// exactly once; the scope catalogue (labels, descriptions, requirements) is the
// server's, so a new scope needs no change here.
import { computed, onMounted, ref } from 'vue'
import { getJSON, postJSON, ApiError } from '../../../api/client'
import type { McpScope, McpStatus, McpToken } from '../../../api/types'
import { toast } from '../../../stores/toasts'
import { confirm } from '../../../composables/useConfirm'

const status = ref<McpStatus | null>(null)
const tokens = ref<McpToken[]>([])
const tokenName = ref('')
const picked = ref<Set<string>>(new Set(['read']))
const revealed = ref<{ token: string; name: string } | null>(null)

const scopes = computed<McpScope[]>(() => status.value?.scopes ?? [])
const scopeById = computed(() => new Map(scopes.value.map((s) => [s.id, s])))
const endpointUrl = computed(() => window.location.origin + (status.value?.endpoint || '/mcp'))

const cliSnippet = computed(() =>
  revealed.value
    ? `claude mcp add --transport http synopticon ${endpointUrl.value} --header "Authorization: Bearer ${revealed.value.token}"`
    : '',
)
const jsonSnippet = computed(() =>
  revealed.value
    ? JSON.stringify(
        {
          mcpServers: {
            synopticon: {
              type: 'http',
              url: endpointUrl.value,
              headers: { Authorization: `Bearer ${revealed.value.token}` },
            },
          },
        },
        null,
        2,
      )
    : '',
)

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

function isNas(id: string): boolean {
  return scopeById.value.get(id)?.danger === 'nas'
}

function toggle(scope: McpScope, on: boolean): void {
  if (scope.id === 'read') return
  const next = new Set(picked.value)
  if (on) {
    next.add(scope.id)
    for (const r of scope.requires) next.add(r)
  } else {
    next.delete(scope.id)
    // Unchecking a scope unchecks everything that needs it, transitively.
    let changed = true
    while (changed) {
      changed = false
      for (const s of scopes.value) {
        if (next.has(s.id) && s.requires.some((r) => !next.has(r))) {
          next.delete(s.id)
          changed = true
        }
      }
    }
  }
  picked.value = next
}

async function load(): Promise<void> {
  try {
    const [st, tk] = await Promise.all([
      getJSON<McpStatus>('/api/mcp/status'),
      getJSON<{ tokens: McpToken[] }>('/api/mcp/tokens'),
    ])
    status.value = st
    tokens.value = tk.tokens || []
  } catch {
    /* transient — leave the current state */
  }
}

async function createToken(): Promise<void> {
  const name = tokenName.value.trim()
  if (!name) return
  try {
    const res = await postJSON<{ token: string; name: string }>('/api/mcp/tokens', {
      name,
      scopes: [...picked.value],
    })
    tokenName.value = ''
    picked.value = new Set(['read'])
    revealed.value = { token: res.token, name: res.name }
    void load()
  } catch (err) {
    toast(errMsg(err, 'Could not create token'), 'error')
  }
}

async function revokeToken(id: number, name: string): Promise<void> {
  const ok = await confirm({
    title: 'Revoke MCP token',
    message: `Revoke "${name}"? Any agent using it will stop working.`,
    okLabel: 'Revoke',
  })
  if (!ok) return
  try {
    await postJSON(`/api/mcp/tokens/${id}/revoke`, {})
    toast('Token revoked', 'ok')
    void load()
  } catch (err) {
    toast(errMsg(err, 'Revoke failed'), 'error')
  }
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    toast('Copied', 'ok')
  } catch {
    /* clipboard access denied — the text is already selectable */
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="card mcp-section">
    <h3>MCP tokens</h3>
    <p class="muted">
      Scoped tokens that let an AI agent tune settings, debug detection and grouping, and run steps
      through Synopticon's MCP endpoint. They work on the MCP endpoint only. The secret is shown
      once.
    </p>
    <p v-if="status && status.enabled" class="mcp-state">
      <span class="badge state-running">Enabled</span>
      Endpoint: <span class="mono">{{ endpointUrl }}</span>
    </p>
    <p v-else-if="status" class="mcp-state">
      <span class="badge">Off</span>
      Turn on Settings → MCP → Enabled and restart Synopticon. You can create tokens in the
      meantime.
    </p>

    <form class="mcp-form" @submit.prevent="createToken">
      <label
        >Token name
        <input
          class="input"
          type="text"
          placeholder="claude-code-laptop"
          required
          v-model="tokenName"
        />
      </label>
      <fieldset class="scope-list">
        <legend>Scopes</legend>
        <label v-for="s in scopes" :key="s.id" class="scope-row" :class="{ nas: s.danger === 'nas' }">
          <input
            type="checkbox"
            :checked="picked.has(s.id)"
            :disabled="s.id === 'read'"
            @change="toggle(s, ($event.target as HTMLInputElement).checked)"
          />
          <span>
            <strong>{{ s.label }}</strong>
            <span v-if="s.danger === 'nas'" class="badge danger-badge">Writes to NAS</span>
            <span class="muted scope-desc">{{ s.description }}</span>
          </span>
        </label>
      </fieldset>
      <p class="muted">
        Agents can never merge two named people, delete duplicate photos or run a full reset; those
        stay in the web GUI.
      </p>
      <div>
        <button class="btn btn-action" type="submit">Create token</button>
      </div>
    </form>

    <div v-if="revealed" class="mcp-reveal">
      <div>
        New token for "{{ revealed.name }}" (copy it now — it will not be shown again):
        <div class="snippet">{{ revealed.token }}</div>
        <button class="btn btn-sm" type="button" @click="copy(revealed.token)">Copy token</button>
      </div>
      <div>
        Claude Code:
        <pre class="snippet">{{ cliSnippet }}</pre>
        <button class="btn btn-sm" type="button" @click="copy(cliSnippet)">Copy command</button>
      </div>
      <div>
        Other clients (streamable HTTP):
        <pre class="snippet">{{ jsonSnippet }}</pre>
        <button class="btn btn-sm" type="button" @click="copy(jsonSnippet)">Copy JSON</button>
      </div>
    </div>

    <table class="data mcp-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Prefix</th>
          <th>Scopes</th>
          <th>Created</th>
          <th>Last used</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in tokens" :key="t.id" :class="{ revoked: t.revoked }">
          <td>{{ t.name }}</td>
          <td class="mono">{{ t.token_prefix }}…</td>
          <td>
            <span
              v-for="sc in t.scopes"
              :key="sc"
              class="badge scope-chip"
              :class="{ 'danger-badge': isNas(sc) }"
              >{{ sc }}</span
            >
          </td>
          <td>{{ fmtTime(t.created_at) }}</td>
          <td>{{ fmtTime(t.last_used_at) }}</td>
          <td>
            <button
              v-if="!t.revoked"
              class="btn btn-sm btn-danger"
              type="button"
              @click="revokeToken(t.id, t.name)"
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
.mcp-section {
  max-width: 760px;
}
.mcp-state {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.mcp-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  max-width: 560px;
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.scope-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
}
.scope-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.scope-desc {
  display: block;
}
.danger-badge {
  background: rgba(216, 67, 67, 0.14);
  color: var(--danger);
  border-color: transparent;
  margin-left: var(--sp-2);
}
.scope-chip {
  margin: 0 4px 4px 0;
}
.scope-chip.danger-badge {
  margin-left: 0;
}
.mcp-table {
  width: 100%;
  margin-top: var(--sp-3);
}
.mcp-table tr.revoked td {
  opacity: 0.5;
}
.mcp-reveal {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-3);
  border: 1px solid var(--action);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
}
.snippet {
  font-family: ui-monospace, monospace;
  background: var(--bg-sunken);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  margin: var(--sp-1) 0;
  word-break: break-all;
  white-space: pre-wrap;
}
</style>
