<script setup lang="ts">
// Apply page: pick kinds + scope, run a free dry-run preview, then a gated
// apply-to-NAS. Ports apply.html.j2 + apply.js. Consent (plan §6) is mirrored
// into the job params exactly as the server's validate_consent expects:
//   reassign → apply_reassigns=true  ·  merge → apply_merges=true
//   merge_named → confirm_phrase == "merge named people" (typed-phrase dialog)
// Apply-to-NAS stays disabled until a preview for the exact current kind-set has
// succeeded this session. The phrase text lives only client-side for the user's
// typing; the server re-validates every request (a missing gate → 428).
import { ref, reactive, computed, onMounted } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import { getJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'
import type { JobEvent, AuditEntry } from '../api/types'
import type { DoneDetail } from '../composables/useJobStream'

const MERGE_NAMED_PHRASE = 'merge named people'

const SPACES: [string, string][] = [
  ['', 'both (default)'],
  ['personal', 'personal'],
  ['shared', 'shared'],
]

interface KindDef {
  k: string
  label: string
  danger: boolean
}
const KINDS: KindDef[] = [
  { k: 'assign', label: 'assign — routine label assignments', danger: false },
  { k: 'low_confidence', label: 'low_confidence — low-confidence assignments', danger: false },
  { k: 'new_person', label: 'new_person — create new persons', danger: false },
  { k: 'reassign', label: 'reassign — move a face to another person', danger: false },
  { k: 'merge', label: 'merge — join two people (irreversible)', danger: true },
  {
    k: 'merge_named',
    label: 'merge_named — join two NAMED people (destroys a label)',
    danger: true,
  },
]

const checked = reactive<Record<string, boolean>>(
  Object.fromEntries(KINDS.map((k) => [k.k, false])),
)
const space = ref('')
const personId = ref('')
const ackReassign = ref(false)
const ackMerge = ref(false)
const counts = ref<Record<string, number>>({})

const panel = ref<InstanceType<typeof JobPanel> | null>(null)
const previewedKey = ref<string | null>(null)
let pendingPreviewKey: string | null = null
let mode: 'preview' | 'apply' | null = null

const resultStats = ref<Record<string, unknown> | null>(null)
const auditRows = ref<AuditEntry[]>([])
const showAudit = ref(false)

const selectedKinds = computed(() => KINDS.map((k) => k.k).filter((k) => checked[k]))
function kindsKey(kinds: string[]): string {
  return kinds.slice().sort().join(',')
}

const showReassignConsent = computed(() => checked.reassign)
const showMergeConsent = computed(() => checked.merge)

const applyEnabled = computed(
  () => previewedKey.value !== null && previewedKey.value === kindsKey(selectedKinds.value),
)
const applyHint = computed(() =>
  applyEnabled.value
    ? 'Preview succeeded for this kind-set — Apply is enabled.'
    : 'Run a preview for the selected kinds to enable Apply.',
)

function baseParams(kinds: string[]): Record<string, unknown> {
  const params: Record<string, unknown> = { kinds }
  const s = space.value.trim()
  if (s) params.space = s
  const p = personId.value.trim()
  if (p) params.person_id = p
  return params
}

async function loadCounts(): Promise<void> {
  try {
    const res = await getJSON<{ counts: Record<string, Record<string, number>> }>(
      '/api/review/counts',
    )
    counts.value = res.counts?.approved || {}
  } catch {
    /* transient */
  }
}

function preview(): void {
  const kinds = selectedKinds.value
  const params = baseParams(kinds)
  params.dry_run = true
  mode = 'preview'
  pendingPreviewKey = kindsKey(kinds)
  showAudit.value = false
  panel.value
    ?.start('apply', params)
    .catch((e: Error) => toast(e.message || 'Preview failed', 'error'))
}

function submitApply(confirmPhrase?: string): void {
  const kinds = selectedKinds.value
  const params = baseParams(kinds)
  params.dry_run = false
  if (kinds.includes('reassign')) params.apply_reassigns = true
  if (kinds.includes('merge')) params.apply_merges = true
  mode = 'apply'
  const extra: Record<string, unknown> = { confirm: true }
  if (confirmPhrase) extra.confirm_phrase = confirmPhrase
  panel.value?.start('apply', params, extra).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 428) {
      const req = (e.body?.requirement as string) || ''
      toast('Consent required: ' + req, 'error')
    } else {
      toast((e as Error).message || 'Apply failed', 'error')
    }
  })
}

async function apply(): Promise<void> {
  const kinds = selectedKinds.value
  if (kinds.includes('reassign') && !ackReassign.value) {
    toast('Acknowledge the reassign warning first.', 'error')
    return
  }
  if (kinds.includes('merge') && !ackMerge.value) {
    toast('Acknowledge the merge warning first.', 'error')
    return
  }
  if (kinds.includes('merge_named')) {
    // Fetch the affected named↔named pairs so the confirmation spells out which
    // human labels get destroyed, then gate on the typed phrase.
    let pairsText = ''
    try {
      const res = await getJSON<{ pairs: { label_a: string; label_b: string }[] }>(
        '/api/review/named-merge-pairs',
      )
      const pairs = res.pairs || []
      pairsText = pairs.length
        ? pairs.map((p) => `${p.label_a} ↔ ${p.label_b}`).join(' · ')
        : 'No approved named↔named merges are queued.'
    } catch {
      /* still allow typed confirm even if the pair list is unavailable */
    }
    const ok = await confirm({
      title: 'Confirm named ↔ named merges',
      message:
        'The following already-named people will be merged, permanently destroying one human label per pair: ' +
        pairsText,
      phrase: MERGE_NAMED_PHRASE,
      okLabel: 'Apply named merges',
    })
    if (ok) submitApply(MERGE_NAMED_PHRASE)
    return
  }
  submitApply()
}

async function loadAudit(): Promise<void> {
  try {
    const res = await getJSON<{ items: AuditEntry[] }>('/api/audit?limit=50')
    auditRows.value = res.items || []
    showAudit.value = true
  } catch {
    /* audit optional */
  }
}

function onDone(detail: DoneDetail): void {
  if ('event' in detail && detail.event === 'result') {
    resultStats.value = ((detail as JobEvent).stats as Record<string, unknown>) || {}
    return
  }
  if ('state' in detail && detail.state === 'succeeded') {
    if (mode === 'preview') {
      previewedKey.value = pendingPreviewKey
    } else if (mode === 'apply') {
      void loadAudit()
      void loadCounts()
    }
  }
}

onMounted(() => {
  void loadCounts()
})
</script>

<template>
  <div class="page">
    <div class="card">
      <h2>Apply corrections</h2>
      <p class="muted">
        Preview is a free dry run. Applying to the NAS is gated per correction kind; merges are
        irreversible.
      </p>

      <h3 style="margin-top: var(--sp-3)">Kinds</h3>
      <div class="kinds">
        <div v-for="kd in KINDS" :key="kd.k" class="kind-row">
          <label :class="{ 'kind-danger': kd.danger }">
            <input type="checkbox" v-model="checked[kd.k]" /> {{ kd.label }}
          </label>
          <span class="kind-count badge">{{ counts[kd.k] || 0 }} approved</span>
        </div>
      </div>

      <div class="scope-row">
        <span
          ><label for="apply-space">Space</label>
          <select class="select" id="apply-space" v-model="space">
            <option v-for="[v, l] in SPACES" :key="v" :value="v">{{ l }}</option>
          </select></span
        >
        <span
          ><label for="apply-person">Person id</label>
          <input
            class="input input-sm"
            id="apply-person"
            type="number"
            min="1"
            placeholder="all"
            v-model="personId"
        /></span>
      </div>

      <div v-if="showReassignConsent" class="consent-box">
        <label
          ><input type="checkbox" v-model="ackReassign" /> I understand this moves face labels a
          human can see in Photos (reassign).</label
        >
      </div>
      <div v-if="showMergeConsent" class="consent-box">
        <label
          ><input type="checkbox" v-model="ackMerge" /> I understand merges are irreversible
          (merge).</label
        >
      </div>

      <div class="apply-actions">
        <button type="button" class="btn" @click="preview">Preview (dry run)</button>
        <button type="button" class="btn btn-danger" :disabled="!applyEnabled" @click="apply">
          Apply to NAS
        </button>
      </div>
      <p class="muted">{{ applyHint }}</p>
    </div>

    <JobPanel ref="panel" @done="onDone" />

    <div v-if="resultStats" class="card result-stats">
      <h3>Last result</h3>
      <dl>
        <template v-for="(v, k) in resultStats" :key="k">
          <dt>{{ k }}</dt>
          <dd>{{ v }}</dd>
        </template>
      </dl>
    </div>

    <div v-if="showAudit" class="card" style="margin-top: var(--sp-4)">
      <h3>Applied to NAS</h3>
      <table class="data">
        <thead>
          <tr>
            <th></th>
            <th>Action</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in auditRows" :key="i">
            <td :class="r.success ? 'audit-ok' : 'audit-fail'">{{ r.success ? '✓' : '✗' }}</td>
            <td>{{ r.action || '' }}</td>
            <td class="mono">{{ r.api || '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.kinds {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: var(--sp-2) 0;
}
.kind-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.kind-row label {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 1;
}
.kind-count {
  font-size: var(--fs-sm);
}
.kind-danger {
  color: var(--danger);
  font-weight: 600;
}
.scope-row {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  flex-wrap: wrap;
  margin: var(--sp-2) 0;
}
.scope-row label {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.consent-box {
  border: 1px solid var(--warn);
  background: rgba(224, 133, 15, 0.08);
  border-radius: var(--radius);
  padding: var(--sp-2) var(--sp-3);
  margin-top: var(--sp-2);
}
.consent-box label {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-base);
}
.apply-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}
.result-stats {
  margin-top: var(--sp-3);
}
.result-stats dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px var(--sp-3);
  margin: 0;
}
.result-stats dt {
  color: var(--text-2);
  font-size: var(--fs-sm);
}
.result-stats dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}
.audit-ok {
  color: var(--ok);
}
.audit-fail {
  color: var(--danger);
}
</style>
