<script setup lang="ts">
// The sign-in log (SEC3). Reads GET /api/security/log (route 18) and renders
// every identifier -- event, outcome, reason -- through utils/authlog.ts, per
// CLAUDE.md's translation rule.
//
// Hides the pending/password_ok rows by default: on an enrolled instance that
// row fires on every single completed login (the password step succeeding
// and handing off to the code step), and showing it unconditionally buries
// the events an operator is actually here to look for. "Show every step"
// turns it back on. The address in every row is itself a filter -- clicking
// it sets route 18's &ip= so "what else came from here" is one click away.
import { computed, onMounted, ref, watch } from 'vue'
import { getJSON, ApiError } from '../../../api/client'
import { eventLabel, outcomeLabel, reasonLabel } from '../../../utils/authlog'

interface LogItem {
  id: number
  ts: number
  event: string
  outcome: string
  reason: string | null
  username: string | null
  user_id: number | null
  ip: string | null
  user_agent: string | null
}

interface LogResponse {
  items: LogItem[]
  total: number
  limit: number
  offset: number
  best_effort: boolean
  summary: {
    total: number
    failed: number
    distinct_ips: number
    distinct_usernames: number
    since: number
  }
  retention: { days: number; max_rows: number; enabled: boolean }
}

const PAGE_SIZE = 50
const UA_PREVIEW = 40

const items = ref<LogItem[]>([])
const total = ref(0)
const offset = ref(0)
const retention = ref<LogResponse['retention'] | null>(null)
const bestEffort = ref(false)
const showEveryStep = ref(false)
const ipFilter = ref('')
const loading = ref(false)
const loadError = ref('')
const loaded = ref(false)

function isBookkeeping(item: LogItem): boolean {
  return item.outcome === 'pending' && item.reason === 'password_ok'
}

const visibleItems = computed(() =>
  showEveryStep.value ? items.value : items.value.filter((item) => !isBookkeeping(item))
)

const rangeLabel = computed(() => {
  if (!total.value) return '0 of 0'
  const first = offset.value + 1
  const last = Math.min(offset.value + PAGE_SIZE, total.value)
  return `${first}–${last} of ${total.value}`
})

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}

function shortUserAgent(ua: string | null): string {
  if (!ua) return ''
  return ua.length > UA_PREVIEW ? `${ua.slice(0, UA_PREVIEW)}…` : ua
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset.value),
    })
    if (ipFilter.value) params.set('ip', ipFilter.value)
    const data = await getJSON<LogResponse>(`/api/security/log?${params.toString()}`)
    items.value = data.items
    total.value = data.total
    retention.value = data.retention
    bestEffort.value = data.best_effort
  } catch (err) {
    loadError.value = err instanceof ApiError ? err.message : 'Could not load the sign-in log'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

function filterToAddress(ip: string | null): void {
  if (!ip) return
  offset.value = 0
  ipFilter.value = ip
}

function clearAddressFilter(): void {
  offset.value = 0
  ipFilter.value = ''
}

function nextPage(): void {
  if (offset.value + PAGE_SIZE < total.value) offset.value += PAGE_SIZE
}

function prevPage(): void {
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
}

watch([offset, ipFilter], () => void load())
onMounted(() => void load())
</script>

<template>
  <div class="card sign-in-log">
    <h3>Sign-in log</h3>

    <p v-if="loading && !loaded" class="muted">Loading…</p>
    <p v-else-if="loadError" class="muted error-text">{{ loadError }}</p>

    <template v-else>
      <div v-if="retention && !retention.enabled" class="banner banner-warn">
        New entries are not being recorded. The ones below are kept.
      </div>

      <div class="toolbar">
        <label class="toggle">
          <input v-model="showEveryStep" type="checkbox" />
          Show every step
        </label>
        <span v-if="ipFilter" class="active-filter muted small">
          Only <span class="mono">{{ ipFilter }}</span>
          <button class="btn btn-sm" type="button" @click="clearAddressFilter">Clear</button>
        </span>
      </div>

      <div class="table-scroll">
        <table class="data log-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Event</th>
              <th>Outcome</th>
              <th>Reason</th>
              <th>Username</th>
              <th>Address</th>
              <th>Browser</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in visibleItems" :key="item.id">
              <td class="nowrap">{{ formatTime(item.ts) }}</td>
              <td>{{ eventLabel(item.event) }}</td>
              <td>
                <span class="badge" :class="`outcome-${item.outcome}`">{{
                  outcomeLabel(item.outcome)
                }}</span>
              </td>
              <td>{{ reasonLabel(item.reason) || '—' }}</td>
              <td>{{ item.username || '—' }}</td>
              <td class="mono">
                <button
                  v-if="item.ip"
                  class="link-btn"
                  type="button"
                  :title="`Only this address: ${item.ip}`"
                  @click="filterToAddress(item.ip)"
                >
                  {{ item.ip }}
                </button>
                <span v-else>—</span>
              </td>
              <td :title="item.user_agent || ''">{{ shortUserAgent(item.user_agent) || '—' }}</td>
            </tr>
            <tr v-if="!visibleItems.length">
              <td colspan="7" class="muted">No entries to show.</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <button class="btn btn-sm" type="button" :disabled="offset === 0" @click="prevPage">
          Previous
        </button>
        <span class="muted small">{{ rangeLabel }}</span>
        <button
          class="btn btn-sm"
          type="button"
          :disabled="offset + PAGE_SIZE >= total"
          @click="nextPage"
        >
          Next
        </button>
      </div>

      <p v-if="bestEffort" class="muted small footnote">
        Entries are recorded on a best-effort basis; one can be missing if the
        database was busy.
      </p>
    </template>
  </div>
</template>

<style scoped>
.sign-in-log {
  max-width: 900px;
}
.banner {
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--bg-sunken);
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-3);
  font-size: var(--fs-sm);
  line-height: 1.5;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.active-filter {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
}
.table-scroll {
  overflow-x: auto;
}
.log-table {
  min-width: 720px;
}
.nowrap {
  white-space: nowrap;
}
.link-btn {
  background: none;
  border: none;
  padding: 0;
  color: var(--action);
  cursor: pointer;
  font: inherit;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.link-btn:hover {
  text-decoration: underline;
}
.badge.outcome-success {
  background: rgba(31, 157, 77, 0.14);
  color: var(--ok);
  border-color: transparent;
}
.badge.outcome-failure {
  background: rgba(216, 67, 67, 0.14);
  color: var(--danger);
  border-color: transparent;
}
.badge.outcome-blocked {
  background: rgba(216, 67, 67, 0.14);
  color: var(--danger);
  border-color: transparent;
}
.badge.outcome-pending {
  background: var(--bg-sunken);
  color: var(--text-2);
}
.pager {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-top: var(--sp-3);
}
.small {
  font-size: var(--fs-sm);
}
.footnote {
  margin-top: var(--sp-2);
}
.error-text {
  color: var(--danger);
}
</style>
